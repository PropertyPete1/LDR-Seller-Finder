"""County parcel/owner data — TxGIO StratMap Land Parcels bulk download.

Why TxGIO instead of scraping bcad.org / comalad.org:
  * Neither CAD publishes a self-serve bulk export on their website.
  * TxGIO (Texas Geographic Information Office, a division of TWDB) collects
    each CAD's parcel data, normalizes it into one schema, and publishes it
    as free CC0-licensed county downloads — this IS the CAD's data, one hop
    downstream, refreshed roughly annually.
  * The download is a FileGDB inside a zip; we read only the attribute table
    (no geometry) via GDAL/pyogrio, which keeps memory sane.

Fields available: Prop_ID, OWNER_NAME, SITUS_* (property address),
MAIL_* (owner mailing address), MKT_VALUE, TAX_YEAR, YEAR_BUILT.
NOT available here: homestead exemption (see exemptions.py) and deed date
(see README — requires the BCAD FTP appraisal export via open records).
"""
import logging
import re
import shutil
import zipfile
from pathlib import Path

import requests

from .. import config
from ..state import now_iso

LOGGER = logging.getLogger("sources.parcels")

TXGIO_API = "https://api.tnris.org/api/v1"
# CloudFront in front of TxGIO downloads rejects the default python UA.
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 LDR-Seller-Finder"}

# Owner names that indicate non-individual owners we don't want to mail/call.
INSTITUTIONAL_RE = re.compile(
    r"\b(LLC|L L C|INC|CORP|LTD|LP\b|L P\b|TRUSTEE|CHURCH|CITY OF|COUNTY OF|"
    r"STATE OF|UNITED STATES|ISD\b|SCHOOL|AUTHORITY|HOMEOWNERS|HOA\b|ASSN|"
    r"ASSOCIATION|PARTNERS|HOLDINGS|PROPERTIES|INVESTMENTS|BANK\b|MORTGAGE|"
    r"HOUSING|DEVELOPMENT|VENTURES|GROUP\b|FUND\b|REIT\b|SAN ANTONIO WATER)\b",
    re.IGNORECASE,
)


def _newest_parcel_collection_id(session: requests.Session) -> str:
    """Find the most recent TxGIO 'Land Parcels' collection id."""
    resp = session.get(
        f"{TXGIO_API}/collections",
        params={"search": config.SETTINGS.get("parcel_sources", {}).get("txgio_collection_search", "land parcels")},
        headers=UA,
        timeout=60,
    )
    resp.raise_for_status()
    results = [c for c in resp.json().get("results", []) if c.get("name") == "Land Parcels"]
    if not results:
        raise RuntimeError("TxGIO API returned no 'Land Parcels' collections")
    results.sort(key=lambda c: c.get("acquisition_date") or "", reverse=True)
    return results[0]["collection_id"]


def download_county_gdb(county: str, dest_dir: Path) -> Path:
    """Download and extract the county parcel FileGDB. Returns the .gdb path."""
    cfg = config.SETTINGS.get("parcel_sources", {}).get(county)
    if not cfg:
        raise ValueError(f"No parcel_sources config for county '{county}'")

    session = requests.Session()
    collection_id = _newest_parcel_collection_id(session)
    resp = session.get(
        f"{TXGIO_API}/resources",
        params={"collection_id": collection_id, "area_type_name": cfg["area_type_name"]},
        headers=UA,
        timeout=60,
    )
    resp.raise_for_status()
    resources = resp.json().get("results", [])
    if not resources:
        raise RuntimeError(f"TxGIO has no parcel resource for {county}")
    url = resources[0]["resource"]

    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / f"{county}_parcels.zip"
    LOGGER.info("Downloading %s parcels: %s", county, url)
    with session.get(url, headers=UA, stream=True, timeout=1800) as dl:
        dl.raise_for_status()
        with open(zip_path, "wb") as f:
            shutil.copyfileobj(dl.raw, f)

    extract_dir = dest_dir / f"{county}_parcels"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)
    zip_path.unlink()  # keep the runner disk lean

    gdbs = list(extract_dir.rglob("*.gdb"))
    if not gdbs:
        raise RuntimeError(f"No .gdb found in {county} parcel download")
    LOGGER.info("Extracted %s", gdbs[0])
    return gdbs[0]


def _clean(val) -> str:
    s = str(val or "").strip()
    return "" if s.upper() in ("NULL", "NONE", "NAN") else " ".join(s.split())


def normalize_addr(addr: str) -> str:
    """Normalize an address string for comparison (absentee detection)."""
    s = _clean(addr).upper()
    s = re.sub(r"[.,#]", " ", s)
    replacements = {
        r"\bSTREET\b": "ST", r"\bDRIVE\b": "DR", r"\bAVENUE\b": "AVE",
        r"\bLANE\b": "LN", r"\bROAD\b": "RD", r"\bCOURT\b": "CT",
        r"\bBOULEVARD\b": "BLVD", r"\bPLACE\b": "PL", r"\bCIRCLE\b": "CIR",
        r"\bTRAIL\b": "TRL", r"\bPARKWAY\b": "PKWY", r"\bHIGHWAY\b": "HWY",
        r"\bNORTH\b": "N", r"\bSOUTH\b": "S", r"\bEAST\b": "E", r"\bWEST\b": "W",
        r"\bAPARTMENT\b": "APT", r"\bSUITE\b": "STE", r"\bUNIT\b": "",
    }
    for pat, rep in replacements.items():
        s = re.sub(pat, rep, s)
    return " ".join(s.split())


def is_absentee(situs_addr: str, situs_zip: str, mail_addr: str, mail_zip: str) -> bool:
    """Absentee = mailing address does not match the property address."""
    situs = normalize_addr(situs_addr)
    mail = normalize_addr(mail_addr)
    if not situs or not mail:
        return False
    # Compare street-number + first street token; zips are a strong tiebreak.
    if situs_zip and mail_zip and situs_zip[:5] != mail_zip[:5]:
        return True
    situs_head = " ".join(situs.split()[:2])
    mail_head = " ".join(mail.split()[:2])
    return situs_head != mail_head


def is_individual_owner(owner_name: str) -> bool:
    name = _clean(owner_name)
    return bool(name) and not INSTITUTIONAL_RE.search(name)


def read_gdb_rows(gdb_path: Path):
    """Yield normalized parcel dicts from a StratMap FileGDB (no geometry)."""
    import pyogrio  # heavyweight import kept local

    layers = pyogrio.list_layers(str(gdb_path))
    layer = layers[0][0]
    meta, _, _, field_data = pyogrio.raw.read(str(gdb_path), layer=layer, read_geometry=False)
    fields = [f.upper() for f in meta["fields"]]
    idx = {f: i for i, f in enumerate(fields)}

    def col(row_i, name):
        i = idx.get(name)
        return field_data[i][row_i] if i is not None else None

    n = len(field_data[0]) if len(field_data) else 0
    for r in range(n):
        yield {
            "prop_id": _clean(col(r, "PROP_ID")),
            "owner_name": _clean(col(r, "OWNER_NAME")),
            "situs_addr": _clean(col(r, "SITUS_ADDR")),
            "situs_city": _clean(col(r, "SITUS_CITY")),
            "situs_zip": _clean(col(r, "SITUS_ZIP")),
            "mail_addr": _clean(col(r, "MAIL_ADDR")),
            "mail_city": _clean(col(r, "MAIL_CITY")),
            "mail_state": _clean(col(r, "MAIL_STAT")),
            "mail_zip": _clean(col(r, "MAIL_ZIP")),
            "mkt_value": float(col(r, "MKT_VALUE") or 0),
            "tax_year": int(col(r, "TAX_YEAR") or 0),
        }


def sync_county(conn, county: str, gdb_path: Path | None = None) -> dict:
    """Sync one county's parcels into the state DB.

    Records owner changes in owner_history (our proxy for resale/deed date
    going forward) and flags absentee owners. Returns stats.
    """
    if gdb_path is None:
        gdb_path = download_county_gdb(county, config.DATA_DIR / "downloads")

    min_val = float(config.SETTINGS.get("min_market_value", 40000))
    max_val = float(config.SETTINGS.get("max_market_value", 2500000))
    ts = now_iso()
    stats = {"county": county, "rows": 0, "kept": 0, "absentee": 0, "owner_changes": 0, "new": 0}

    existing = {
        row["prop_id"]: row["owner_name"]
        for row in conn.execute("SELECT prop_id, owner_name FROM parcels WHERE county=?", (county,))
    }

    batch = []
    for p in read_gdb_rows(gdb_path):
        stats["rows"] += 1
        if not p["prop_id"] or not p["owner_name"] or not p["situs_addr"]:
            continue
        if not (min_val <= p["mkt_value"] <= max_val):
            continue
        if not is_individual_owner(p["owner_name"]):
            continue
        absentee = is_absentee(p["situs_addr"], p["situs_zip"], p["mail_addr"], p["mail_zip"])
        stats["kept"] += 1
        stats["absentee"] += int(absentee)

        prev_owner = existing.get(p["prop_id"])
        if prev_owner is None:
            stats["new"] += 1
        elif prev_owner != p["owner_name"]:
            stats["owner_changes"] += 1
            conn.execute(
                "INSERT INTO owner_history (county, prop_id, owner_name, observed_at) VALUES (?,?,?,?)",
                (county, p["prop_id"], p["owner_name"], ts),
            )

        batch.append((
            county, p["prop_id"], p["owner_name"], p["situs_addr"], p["situs_city"],
            p["situs_zip"], p["mail_addr"], p["mail_city"], p["mail_state"], p["mail_zip"],
            p["mkt_value"], p["tax_year"], int(absentee), ts, ts,
        ))
        if len(batch) >= 5000:
            _upsert_parcels(conn, batch)
            batch = []
    if batch:
        _upsert_parcels(conn, batch)
    conn.commit()
    LOGGER.info("Parcel sync %s: %s", county, stats)
    return stats


def _upsert_parcels(conn, batch):
    conn.executemany(
        """
        INSERT INTO parcels (county, prop_id, owner_name, situs_addr, situs_city,
                             situs_zip, mail_addr, mail_city, mail_state, mail_zip,
                             mkt_value, tax_year, is_absentee, first_seen_at, last_seen_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT (county, prop_id) DO UPDATE SET
            owner_name=excluded.owner_name, situs_addr=excluded.situs_addr,
            situs_city=excluded.situs_city, situs_zip=excluded.situs_zip,
            mail_addr=excluded.mail_addr, mail_city=excluded.mail_city,
            mail_state=excluded.mail_state, mail_zip=excluded.mail_zip,
            mkt_value=excluded.mkt_value, tax_year=excluded.tax_year,
            is_absentee=excluded.is_absentee, last_seen_at=excluded.last_seen_at
        """,
        batch,
    )

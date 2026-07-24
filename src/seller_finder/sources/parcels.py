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

Download strategy (why a mirror exists):
  CloudFront in front of data.geographic.texas.gov blocks GitHub Actions
  datacenter IPs at the IP level (403 regardless of UA/retries — confirmed
  in live Actions runs). So the PRIMARY source is a mirror of the same CC0
  zips attached to this repo's GitHub Release `parcel-data-2025`, which is
  always reachable from Actions. TxGIO direct download is kept as FALLBACK
  (works from residential IPs, and in case the mirror asset is missing).
  Refresh process: README → "Parcel data mirror — quarterly refresh".
"""
import os
import subprocess
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
MIRROR_REPO = "PropertyPete1/LDR-Seller-Finder"
MIRROR_TAG = "parcel-data-2025"
GITHUB_API = "https://api.github.com"
# CloudFront in front of TxGIO downloads rejects non-browser user agents
# (verified: the default python UA and custom-suffixed UAs get 403).
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

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


def _github_token() -> str:
    """Find a GitHub token usable for release-asset downloads.

    Order: GITHUB_TOKEN / GH_TOKEN env vars, then the auth header that
    actions/checkout persists in .git/config (default persist-credentials:
    true) — so this works on Actions WITHOUT any workflow file changes.
    """
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        tok = (os.environ.get(var) or "").strip()
        if tok:
            return tok
    try:
        header = subprocess.run(
            ["git", "config", "--get", "http.https://github.com/.extraheader"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        # Format: "AUTHORIZATION: basic base64(x-access-token:TOKEN)"
        if header.lower().startswith("authorization: basic "):
            import base64
            decoded = base64.b64decode(header.split()[-1]).decode()
            if ":" in decoded:
                return decoded.split(":", 1)[1]
    except Exception:  # noqa: BLE001
        pass
    return ""


def _download_from_mirror(county: str, zip_path: Path) -> bool:
    """Download {county}_parcels.zip from the repo's parcel-data release.

    Returns True on success. Works on Actions runners (github.com is always
    reachable there); the repo is private so an auth token is required.
    """
    token = _github_token()
    if not token:
        LOGGER.warning("Mirror: no GitHub token available — skipping mirror source")
        return False
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    try:
        rel = requests.get(
            f"{GITHUB_API}/repos/{MIRROR_REPO}/releases/tags/{MIRROR_TAG}",
            headers=headers, timeout=60)
        rel.raise_for_status()
        assets = rel.json().get("assets", [])
        asset = next((a for a in assets if a["name"] == f"{county}_parcels.zip"), None)
        if not asset:
            LOGGER.warning("Mirror: release %s has no asset %s_parcels.zip", MIRROR_TAG, county)
            return False
        LOGGER.info("Downloading %s parcels from mirror release %s (%.0f MB)",
                    county, MIRROR_TAG, asset["size"] / 1e6)
        dl_headers = {**headers, "Accept": "application/octet-stream"}
        with requests.get(asset["url"], headers=dl_headers, stream=True,
                          timeout=1800, allow_redirects=True) as dl:
            dl.raise_for_status()
            with open(zip_path, "wb") as f:
                shutil.copyfileobj(dl.raw, f)
        if zip_path.stat().st_size != asset["size"]:
            LOGGER.warning("Mirror: size mismatch for %s (%d != %d)",
                           county, zip_path.stat().st_size, asset["size"])
            zip_path.unlink(missing_ok=True)
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Mirror download failed for %s: %s", county, exc)
        zip_path.unlink(missing_ok=True)
        return False


def _download_from_txgio(county: str, cfg: dict, zip_path: Path) -> bool:
    """Download directly from TxGIO (fallback — CloudFront blocks datacenter
    IPs, so this typically only works from residential/office networks)."""
    try:
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
            LOGGER.warning("TxGIO has no parcel resource for %s", county)
            return False
        url = resources[0]["resource"]
        LOGGER.info("Downloading %s parcels from TxGIO: %s", county, url)
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                with session.get(url, headers=UA, stream=True, timeout=1800) as dl:
                    dl.raise_for_status()
                    with open(zip_path, "wb") as f:
                        shutil.copyfileobj(dl.raw, f)
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                LOGGER.warning("TxGIO attempt %d/3 failed for %s: %s", attempt, county, exc)
                import time
                time.sleep(10 * attempt)
        if last_exc is not None:
            raise last_exc
        return True
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("TxGIO download failed for %s: %s", county, exc)
        zip_path.unlink(missing_ok=True)
        return False


def download_county_gdb(county: str, dest_dir: Path) -> Path:
    """Download and extract the county parcel FileGDB. Returns the .gdb path.

    Source order: (1) GitHub Release mirror `parcel-data-2025` — reachable
    from Actions runners; (2) TxGIO direct — fallback, blocked from
    datacenter IPs by CloudFront but fine from residential networks.
    """
    cfg = config.SETTINGS.get("parcel_sources", {}).get(county)
    if not cfg:
        raise ValueError(f"No parcel_sources config for county '{county}'")

    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / f"{county}_parcels.zip"

    ok = _download_from_mirror(county, zip_path)
    if not ok:
        LOGGER.info("Mirror unavailable for %s — trying TxGIO direct", county)
        ok = _download_from_txgio(county, cfg, zip_path)
    if not ok:
        raise RuntimeError(
            f"All parcel download sources failed for {county} "
            f"(mirror release '{MIRROR_TAG}' and TxGIO direct). "
            "See README 'Parcel data mirror' to refresh the mirror.")

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


def _street_only(addr: str) -> str:
    """Some county files (e.g. Bexar) embed ', CITY, TX ZIP' inside the address
    fields. Return just the street portion so addresses render/dedupe cleanly."""
    s = _clean(addr)
    if "," in s:
        head = s.split(",")[0].strip()
        if head:
            return head
    return s


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
        situs_city = _clean(col(r, "SITUS_CITY"))
        yield {
            "prop_id": _clean(col(r, "PROP_ID")),
            "owner_name": _clean(col(r, "OWNER_NAME")),
            "situs_addr": _street_only(col(r, "SITUS_ADDR")),
            "situs_city": situs_city,
            "situs_zip": _clean(col(r, "SITUS_ZIP")),
            "mail_addr": _street_only(col(r, "MAIL_ADDR")),
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

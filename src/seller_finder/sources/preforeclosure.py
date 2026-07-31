"""Pre-foreclosure / Notice of Trustee Sale — per-county sources.

Bexar: the County Clerk publishes the current month's foreclosure-sale
notices through a public ArcGIS service (the data behind
maps.bexar.org/foreclosures). Layer 0 = Mortgage, layer 1 = Tax.
Fields: ADDRESS, DOC_NUMBER, YEAR, MONTH, TYPE, CITY, ZIP. No owner name in
the feed, so the pipeline matches ADDRESS against parcel situs addresses.

Counties WITHOUT a clean feed (Travis — tccsearch.org is CAPTCHA/login
gated; Comal — courthouse kiosk only) use the CSV INBOX instead: drop
data/inbox/foreclosures_<county>_*.csv with columns
  address, zip, doc_number[, city, sale_date]
and commit — the next run ingests and matches it exactly like a live feed.
Exactly-once ingestion is enforced by the `ingested_files` ledger in the
committed state DB (keyed by file name + content hash), so a file that stays
in the repo is never re-processed, and a corrected re-upload of the same name
IS processed. (An earlier version renamed the file to *.done on the runner —
that marker was thrown away with the runner, so it never actually worked.)
See README "County foreclosure sources" for where to export each county's
notices manually and the paid-API options for full automation.

Failure discipline: a live feed that errors raises FeedUnavailable rather than
returning an empty list. An empty list means "this county genuinely has no
notices right now" and must never be produced by a network failure.
"""
import csv
import logging

from .. import config
from ..arcgis import ArcGISError
from ..arcgis import query as arcgis_query
from ..state import already_ingested, mark_ingested
from ..sources.parcels import normalize_addr

LOGGER = logging.getLogger("sources.preforeclosure")


class FeedUnavailable(RuntimeError):
    """Every configured foreclosure feed for a county failed (network/HTTP).

    Distinct from "no notices": callers must record this as a run error, not
    as a clean zero, so a blocked feed can never look like a quiet week.
    """


def fetch(county: str, conn=None) -> list[dict]:
    """Return current-month notices: [{address, doc_number, year, month, kind, city, zip}].

    conn : committed-state connection, required for the CSV-inbox path so
           ingestion can be recorded in the exactly-once ledger. When omitted,
           inbox files are read but NOT marked ingested (used by ad-hoc scripts).
    Raises FeedUnavailable if every configured live feed for the county errored.
    """
    src = config.SETTINGS.get("foreclosure_sources", {}).get(county)
    if not src:
        LOGGER.info("No foreclosure source configured for %s — skipping", county)
        return []
    if not src.get("mortgage_url") and not src.get("tax_url"):
        # No live feed — CSV inbox fallback (Travis et al).
        return _fetch_from_inbox(county, conn)

    notices = []
    attempted = 0
    failed = 0
    for kind, key in (("mortgage", "mortgage_url"), ("tax", "tax_url")):
        url = src.get(key)
        if not url:
            continue
        attempted += 1
        params = {
            "where": "1=1",
            "outFields": "ADDRESS,DOC_NUMBER,YEAR,MONTH,TYPE,CITY,ZIP",
            "returnGeometry": "false",
            "f": "json",
        }
        try:
            # arcgis_query raises on transport errors AND on the HTTP-200
            # bodies ArcGIS uses to report query failures — see arcgis.py.
            # Without that check a broken layer reads as "no notices".
            data = arcgis_query(None, url, params, timeout=120, attempts=3)
        except ArcGISError as exc:
            LOGGER.error("Foreclosure fetch failed (%s %s): %s", county, kind, exc)
            failed += 1
            continue
        for f in data.get("features", []):
            a = f.get("attributes", {})
            if not a.get("ADDRESS"):
                continue
            notices.append({
                "address": str(a["ADDRESS"]).strip(),
                "doc_number": str(a.get("DOC_NUMBER") or ""),
                "year": a.get("YEAR"),
                "month": a.get("MONTH"),
                "kind": kind,
                "city": str(a.get("CITY") or "").strip(),
                "zip": str(a.get("ZIP") or "").strip(),
            })
    if attempted and failed == attempted:
        # Every feed for this county errored. Returning [] here would look
        # identical to "no foreclosures this month" and silently drop the
        # signal, so make it a hard error the run records instead.
        raise FeedUnavailable(
            f"all {attempted} foreclosure feed(s) for {county} failed "
            "(see preceding errors) — treating as UNKNOWN, not as zero notices")
    LOGGER.info("Foreclosure notices for %s: %d", county, len(notices))
    return notices


def _fetch_from_inbox(county: str, conn=None) -> list[dict]:
    """Ingest manually-exported notice CSVs from data/inbox/ (see module doc).

    Files already recorded in the `ingested_files` ledger are skipped, so the
    CSV can stay committed in the repo without being re-processed every run.
    """
    inbox = config.DATA_DIR / "inbox"
    notices: list[dict] = []
    if not inbox.exists():
        LOGGER.info("%s: no live foreclosure feed and no inbox dir — 0 notices", county)
        return notices
    for path in sorted(inbox.glob(f"foreclosures_{county}_*.csv")):
        try:
            if conn is not None and already_ingested(conn, "foreclosure", path):
                LOGGER.info("%s: inbox file %s already ingested — skipping", county, path.name)
                continue
            file_notices: list[dict] = []
            with open(path, newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
                    if not row.get("address"):
                        continue
                    file_notices.append({
                        "address": row["address"],
                        "doc_number": row.get("doc_number", ""),
                        "year": None,
                        "month": None,
                        "kind": "mortgage",
                        "city": row.get("city", ""),
                        "zip": row.get("zip", ""),
                        "sale_date": row.get("sale_date", ""),
                    })
            notices.extend(file_notices)
            if conn is not None:
                mark_ingested(conn, "foreclosure", path, len(file_notices))
            LOGGER.info("%s: ingested foreclosure inbox file %s (%d notices)",
                        county, path.name, len(file_notices))
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("%s: failed to ingest %s: %s", county, path.name, exc)
    LOGGER.info("Foreclosure notices for %s (inbox): %d", county, len(notices))
    return notices


def match_to_parcels(conn, county: str, notices: list[dict]) -> list[dict]:
    """Attach prop_id/owner to each notice by matching the property address.

    Match strategy: normalized address head (number + street) + zip.
    """
    if not notices:
        return []

    # Build lookup of parcel situs addresses for the county.
    lookup: dict[str, dict] = {}
    for row in conn.execute(
        "SELECT prop_id, owner_name, situs_addr, situs_zip, mail_addr FROM pc.parcels WHERE county=?",
        (county,),
    ):
        key = _addr_key(row["situs_addr"], row["situs_zip"])
        if key:
            lookup[key] = dict(row)

    matched = []
    for n in notices:
        key = _addr_key(n["address"], n["zip"])
        hit = lookup.get(key)
        if hit:
            matched.append({**n, "prop_id": hit["prop_id"], "owner_name": hit["owner_name"]})
    LOGGER.info("Foreclosure notices matched to parcels: %d/%d", len(matched), len(notices))
    return matched


def _addr_key(addr: str, zip_code: str) -> str:
    norm = normalize_addr(addr or "")
    if not norm:
        return ""
    head = " ".join(norm.split()[:3])
    return f"{head}|{(zip_code or '')[:5]}"

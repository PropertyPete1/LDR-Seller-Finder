"""Deed / purchase dates — unlocks the "owned 10+ years" (+20) signal.

Data-source reality (researched Jul 2026, see README "Data Source Status"):
  * No free bulk source with deed dates is directly downloadable for Bexar
    or Comal. TxGIO parcels have no deed date; the Bexar ArcGIS parcel layer
    has no deed/sale fields; the Comptroller's EARS/EPTS rolls are not public;
    the County Clerk's deed search is an interactive site (new transfers only).
  * The unlock is BCAD's appraisal data export: email openrecords@bcad.org and
    request the current *appraisal export* (free, delivered via their FTP).
    Comal AD: open-records request via comalad.org.

How this module works — fully automatic once the file exists:
  1. Drop a CSV named  deeds_*.csv  (or  deed_dates*.csv ) into  data/inbox/ .
     Required columns (case-insensitive, extras ignored):
         county, prop_id, deed_date
     county may be omitted -> defaults to 'bexar'.
     deed_date accepts  YYYY-MM-DD, MM/DD/YYYY, YYYYMMDD, or bare YYYY.
  2. Every weekly run ingests inbox files into the encrypted state DB
     (deed_dates table) BEFORE scoring. Exactly-once processing is enforced by
     the `ingested_files` ledger in the committed state DB (file name + content
     hash) — NOT by renaming the file, because the runner never commits its
     working tree back to `main`, so a rename there is thrown away.
  3. scoring._owned_ten_plus_years() reads deed_dates first, and falls back
     to the owner_history proxy (unchanged owner across runs).

scripts/import_deed_dates.py remains available for one-off local imports.
"""
import csv
import datetime as dt
import logging
import re
from pathlib import Path

from .. import config
from ..state import already_ingested, mark_ingested, now_iso

LOGGER = logging.getLogger("sources.deeds")

INBOX_PATTERNS = ("deeds_*.csv", "deed_dates*.csv")


def parse_deed_date(raw: str) -> str | None:
    """Normalize assorted deed-date formats to ISO YYYY-MM-DD (or None)."""
    s = (raw or "").strip()
    if not s:
        return None
    # already ISO-ish
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
    else:
        m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", s)  # MM/DD/YYYY
        if m:
            mo, d, y = int(m[1]), int(m[2]), int(m[3])
        else:
            m = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)  # YYYYMMDD
            if m:
                y, mo, d = int(m[1]), int(m[2]), int(m[3])
            else:
                m = re.match(r"^(\d{4})$", s)  # bare year
                if m:
                    y, mo, d = int(m[1]), 7, 1  # mid-year to be conservative
                else:
                    return None
    try:
        return dt.date(y, mo, d).isoformat()
    except ValueError:
        return None


def ingest_file(conn, path: Path, default_county: str = "bexar") -> int:
    """Import one deed-date CSV into the deed_dates table. Returns row count."""
    ts = now_iso()
    n = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            row = {(k or "").lower().strip(): (v or "").strip() for k, v in row.items()}
            prop_id = row.get("prop_id") or row.get("property_id") or row.get("propid")
            deed = parse_deed_date(row.get("deed_date") or row.get("deed_dt")
                                   or row.get("sale_date") or "")
            if not prop_id or not deed:
                continue
            county = (row.get("county") or default_county).lower()
            conn.execute(
                "INSERT OR REPLACE INTO deed_dates (county, prop_id, deed_date, source, imported_at) "
                "VALUES (?,?,?,?,?)",
                (county, prop_id, deed, path.name, ts),
            )
            n += 1
    conn.commit()
    return n


def ingest_inbox(conn) -> dict:
    """Ingest all deed CSVs waiting in data/inbox/, exactly once per file
    content (see the `ingested_files` ledger in state.py)."""
    inbox = config.DATA_DIR / "inbox"
    stats: dict = {"files": 0, "rows": 0, "file_errors": []}
    if not inbox.exists():
        return stats
    seen = set()
    for pattern in INBOX_PATTERNS:
        for path in sorted(inbox.glob(pattern)):
            if path in seen:
                continue
            seen.add(path)
            try:
                if already_ingested(conn, "deeds", path):
                    LOGGER.info("Deed inbox file %s already imported — skipping", path.name)
                    continue
                rows = ingest_file(conn, path)
                mark_ingested(conn, "deeds", path, rows)
            except Exception as exc:  # noqa: BLE001 — a bad file must not kill the run
                # Recorded, not just logged: this file is a manual export Peter
                # committed on purpose, so it silently doing nothing is a
                # failure. collect_stage_errors reads file_errors.
                LOGGER.error("Deed import failed for %s: %s", path.name, exc)
                stats["file_errors"].append(f"{path.name}: {exc}")
                continue
            stats["files"] += 1
            stats["rows"] += rows
            LOGGER.info("Imported %d deed dates from %s", rows, path.name)
    total = conn.execute("SELECT COUNT(*) AS c FROM deed_dates").fetchone()["c"]
    stats["total_deed_dates"] = total
    if stats["files"] == 0:
        LOGGER.info("No deed CSVs in inbox; deed_dates table has %d rows "
                    "(tenure signal %s)", total, "ACTIVE" if total else "dormant — "
                    "email openrecords@bcad.org for the free appraisal export")
    return stats

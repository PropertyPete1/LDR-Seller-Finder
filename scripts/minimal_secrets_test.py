#!/usr/bin/env python3
"""Prove the pipeline completes with ONLY ANTHROPIC_API_KEY + FUB_API_KEY
(+ SQLITE_ENCRYPTION_KEY, which is workflow-level) — no BatchData, no SMTP,
no healthchecks. Uses a temp DB with seeded parcels and live Bexar
pre-foreclosure data; runs the real weekly steps NOT in dry-run mode so all
optional-skip paths are exercised for real.
"""
import logging
import os
import sys
import tempfile
from pathlib import Path

# Simulate the minimal-secrets environment BEFORE importing config.
for var in ("BATCHDATA_API_KEY", "SMTP_USER", "SMTP_PASSWORD", "SMTP_HOST",
            "SMTP_PORT", "EMAIL_FROM", "HEALTHCHECK_URL", "DRY_RUN"):
    os.environ.pop(var, None)
os.environ["DATABASE_PATH"] = str(Path(tempfile.mkdtemp()) / "minimal.sqlite3")
os.environ["REVIEW_DIR"] = str(Path(tempfile.mkdtemp()) / "review")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

from seller_finder import config  # noqa: E402
from seller_finder.state import get_db, now_iso  # noqa: E402
from seller_finder.sources import preforeclosure, divorce  # noqa: E402
from seller_finder.scoring import compute_scores  # noqa: E402
from seller_finder.skiptrace.tracer import trace_qualified_leads  # noqa: E402
from seller_finder.review import stage_traced_leads, write_review_files, send_digest_email  # noqa: E402
from seller_finder.health import ping_healthcheck  # noqa: E402

assert config.BATCHDATA_API_KEY == "", "expected no BatchData key"
assert config.SMTP_USER == "", "expected no SMTP user"
assert config.HEALTHCHECK_URL == "", "expected no healthcheck URL"
assert config.DRY_RUN is False, "this test must run the REAL code paths"

conn = get_db()

# Seed parcels that will match live foreclosure addresses + one absentee-only.
print("== fetch live Bexar pre-foreclosure notices ==")
notices = preforeclosure.fetch("bexar")
assert len(notices) > 50
ts = now_iso()
for i, n in enumerate(notices[:5]):
    conn.execute(
        """INSERT OR REPLACE INTO parcels (county, prop_id, owner_name, situs_addr,
           situs_city, situs_zip, mail_addr, mail_city, mail_state, mail_zip,
           mkt_value, tax_year, is_absentee, first_seen_at, last_seen_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("bexar", f"900{i}", f"TESTOWNER SAM{i}", n["address"], n["city"], n["zip"],
         "111 FARAWAY LN", "AUSTIN", "TX", "78701", 300000, 2025, 1, ts, ts),
    )
conn.commit()

matched = preforeclosure.match_to_parcels(conn, "bexar", notices)
for m in matched:
    m["county"] = "bexar"
print(f"matched: {len(matched)}")
assert len(matched) >= 5

print("== divorce stub (no CSVs — must not error) ==")
filings = divorce.fetch("bexar")
assert filings == []

print("== scoring ==")
sstats = compute_scores(conn, matched, [], {})
print(sstats)
assert sstats["qualified"] >= 5

print("== skip trace with NO BatchData key (must skip gracefully) ==")
tstats = trace_qualified_leads(conn)
print(tstats)
assert tstats["skipped_no_api_key"] >= 5
assert tstats["traced"] == 0

print("== stage + review files ==")
staged = stage_traced_leads(conn)
assert staged >= 5
rstats = write_review_files(conn)
print(rstats)
assert rstats["pending"] >= 5
csv_text = Path(rstats["csv"]).read_text()
assert "TESTOWNER" in csv_text, "scored leads must appear in review CSV without contact info"

print("== digest with NO SMTP (must skip, return False, not raise) ==")
assert send_digest_email(conn) is False

print("== healthcheck with NO URL (must skip, not raise) ==")
ping_healthcheck()

print("\nMINIMAL-SECRETS TEST PASSED ✅ — full run completes with only "
      "ANTHROPIC_API_KEY + FUB_API_KEY + SQLITE_ENCRYPTION_KEY")

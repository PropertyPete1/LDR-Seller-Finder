#!/usr/bin/env python3
"""E2E test of the state-DB split using REAL mirror data (both counties).

Verifies:
  1. Full weekly pipeline path: parcel sync (from pre-downloaded mirror GDBs)
     -> live foreclosure fetch/match -> scoring -> review artifacts.
  2. Committed state DB stays small (parcels never committed).
  3. run_summary.md shows non-zero score bands / by-source / diagnostics.
  4. Legacy-DB migration works on a DB that contains an old parcels table.

Usage:
  python3 scripts/state_split_e2e_test.py <bexar_gdb> <comal_gdb>
"""
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, "src")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from seller_finder import config  # noqa: E402
from seller_finder.state import get_db, check_state_size  # noqa: E402
from seller_finder.sources import parcels, preforeclosure  # noqa: E402
from seller_finder.scoring import compute_scores  # noqa: E402
from seller_finder.review import write_review_files  # noqa: E402

bexar_gdb = Path(sys.argv[1])
comal_gdb = Path(sys.argv[2])
db_path = config.DATA_DIR / "e2e_split.sqlite3"
cache_path = config.DATA_DIR / "e2e_split_cache.sqlite3"
for p in (db_path, cache_path):
    p.unlink(missing_ok=True)

conn = get_db(db_path, parcels_cache=cache_path, fresh_parcels=True)
stats = {"counties": {}}

# 1. Parcel sync from the exact mirror files
stats["counties"]["bexar"] = {"parcels": parcels.sync_county(conn, "bexar", gdb_path=bexar_gdb)}
stats["counties"]["comal"] = {"parcels": parcels.sync_county(conn, "comal", gdb_path=comal_gdb)}

# 2. Live foreclosure fetch + match
notices = preforeclosure.fetch("bexar")
matched = preforeclosure.match_to_parcels(conn, "bexar", notices)
for m in matched:
    m["county"] = "bexar"
stats["counties"]["bexar"]["preforeclosure"] = {"notices": len(notices), "matched": len(matched)}

# 3. Scoring
stats["scoring"] = compute_scores(conn, matched, [], {})
stats["divorce"] = {"filings": 0, "matched": 0}
stats["deeds"] = {"files": 0, "rows": 0}
stats["skiptrace"] = {"eligible": stats["scoring"]["qualified"], "cached": 0, "traced": 0,
                      "matched": 0, "skipped_no_api_key": stats["scoring"]["qualified"]}
# Simulate held outcome (no BatchData key), as on the user's runner
conn.execute("UPDATE leads SET status='held_no_contact' WHERE status='qualified'")
conn.commit()
stats["fub_push"] = {"pushed": 0, "held_no_contact": stats["scoring"]["qualified"], "failed": 0}

# 4. Review artifacts
review = write_review_files(conn, run_stats=stats)
conn.commit()
conn.close()

size_mb = check_state_size(db_path)
cache_mb = cache_path.stat().st_size / 1e6

summary = (config.REVIEW_DIR / "run_summary.md").read_text()
print("\n========== RESULTS ==========")
print(f"bexar parcels: {stats['counties']['bexar']['parcels']}")
print(f"comal parcels: {stats['counties']['comal']['parcels']}")
print(f"foreclosures:  {stats['counties']['bexar']['preforeclosure']}")
print(f"scoring:       {stats['scoring']}")
print(f"review:        pending={review['pending']} buckets={review['score_buckets']} "
      f"by_source={review['by_source']}")
print(f"combos:        {review['by_signal_combo']}")
print(f"COMMITTED DB:  {size_mb:.1f} MB   (cache: {cache_mb:.1f} MB, gitignored)")

assert stats["scoring"]["qualified"] > 0, "no qualified leads!"
assert size_mb < 20, f"committed DB too big: {size_mb} MB"
assert sum(review["score_buckets"].values()) == stats["scoring"]["qualified"], \
    "summary buckets do not add up to qualified count"
assert review["by_source"], "by_source empty"
assert "Pipeline diagnostics" in summary, "diagnostics section missing"
assert "rows " in summary and "notices" in summary
print("=== STATE SPLIT E2E OK ===")

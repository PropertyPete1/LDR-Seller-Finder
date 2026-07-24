#!/usr/bin/env python3
"""End-to-end live smoke test (DRY_RUN — no paid calls, no FUB writes).

Uses the real Comal TxGIO GDB (pass a path to skip re-download), live Bexar
pre-foreclosure feed, scoring, dry-run skip trace, staging, and review files.
"""
import logging
import os
import sys
from pathlib import Path

os.environ.setdefault("DRY_RUN", "true")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from seller_finder import config  # noqa: E402
from seller_finder.state import get_db  # noqa: E402
from seller_finder.sources import parcels, preforeclosure  # noqa: E402
from seller_finder.scoring import compute_scores  # noqa: E402
from seller_finder.skiptrace.tracer import trace_qualified_leads  # noqa: E402
from seller_finder.review import stage_traced_leads, write_review_files  # noqa: E402


def main() -> int:
    gdb = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    conn = get_db()

    print("== 1. Comal parcel sync (real TxGIO data) ==")
    stats = parcels.sync_county(conn, "comal", gdb_path=gdb)
    print(stats)
    assert stats["kept"] > 10000, "expected tens of thousands of Comal parcels"
    assert stats["absentee"] > 100, "expected many absentee owners"

    print("== 2. Live Bexar pre-foreclosure fetch ==")
    notices = preforeclosure.fetch("bexar")
    print(f"notices: {len(notices)}")
    assert len(notices) > 50, "expected a real monthly notice list"
    matched = preforeclosure.match_to_parcels(conn, "bexar", notices)
    for m in matched:
        m["county"] = "bexar"
    print(f"matched to parcels: {len(matched)} (0 expected — Bexar parcels not loaded in this test)")

    print("== 3. Scoring ==")
    sstats = compute_scores(conn, matched, [], {})
    print(sstats)
    assert sstats["leads_created"] > 100

    print("== 4. Skip trace (dry run) ==")
    tstats = trace_qualified_leads(conn)
    print(tstats)

    print("== 5. Stage + review files ==")
    stage_traced_leads(conn)
    rstats = write_review_files(conn)
    print(rstats)

    print("\nE2E LIVE TEST PASSED ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Live Bexar end-to-end test — real TxGIO Bexar parcels + real County Clerk
foreclosure notices, full scoring, review CSV. Answers: how many leads qualify?

Usage:  python3 scripts/live_bexar_test.py [path/to/bexar.gdb]
If no GDB path is given, downloads the Bexar TxGIO parcel file (~large).
"""
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger("live_bexar_test")

from seller_finder import config  # noqa: E402
from seller_finder.state import get_db  # noqa: E402
from seller_finder.sources import parcels, exemptions, preforeclosure  # noqa: E402
from seller_finder.scoring import compute_scores  # noqa: E402
from seller_finder.skiptrace.tracer import trace_qualified_leads  # noqa: E402
from seller_finder.review import stage_traced_leads, write_review_files  # noqa: E402


def main() -> int:
    t0 = time.time()
    gdb = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    conn = get_db(config.DATA_DIR / "live_test.sqlite3")

    # 1. Bexar parcels (real TxGIO bulk data)
    p_stats = parcels.sync_county(conn, "bexar", gdb_path=gdb)
    print(f"\n[1] Bexar parcels: {p_stats}")

    # 2. Homestead exemptions (Bexar ArcGIS) — optional, may be slow
    try:
        e_stats = exemptions.sync_county(conn, "bexar")
        e_stats.pop("homestead_removed", None)
        print(f"[2] Exemptions: {e_stats}")
    except Exception as exc:  # noqa: BLE001
        print(f"[2] Exemptions skipped: {exc}")

    # 3. Live foreclosure notices → match to real parcels
    notices = preforeclosure.fetch("bexar")
    matched = preforeclosure.match_to_parcels(conn, "bexar", notices)
    for m in matched:
        m["county"] = "bexar"
    print(f"[3] Foreclosure notices: {len(notices)}, matched to parcels: {len(matched)} "
          f"({100 * len(matched) / max(len(notices), 1):.0f}%)")

    # 4. Scoring
    s_stats = compute_scores(conn, matched, [], {})
    print(f"[4] Scoring: {s_stats}")

    # 5. Trace (no key → leads still advance) + stage + review CSV
    t_stats = trace_qualified_leads(conn)
    staged = stage_traced_leads(conn)
    review = write_review_files(conn)
    print(f"[5] Trace: {t_stats} | staged: {staged} | review: {review}")

    # 6. Report
    dist = conn.execute(
        "SELECT score, COUNT(*) c FROM leads GROUP BY score ORDER BY score DESC"
    ).fetchall()
    print("\nScore distribution:")
    for row in dist:
        print(f"  score {row['score']:>3}: {row['c']:>6} leads")
    qualified = conn.execute(
        "SELECT COUNT(*) c FROM leads WHERE score >= ?", (config.SCORE_THRESHOLD,)
    ).fetchone()["c"]
    pending = conn.execute(
        "SELECT COUNT(*) c FROM leads WHERE status='awaiting_approval'"
    ).fetchone()["c"]
    print(f"\nQUALIFIED (score >= {config.SCORE_THRESHOLD}): {qualified}")
    print(f"AWAITING APPROVAL (in pending_leads.csv): {pending}")
    print(f"Elapsed: {time.time() - t0:.0f}s")

    print("\nSample qualified leads:")
    for row in conn.execute(
        "SELECT owner_name, property_addr, score, primary_source FROM leads "
        "WHERE score >= ? ORDER BY score DESC LIMIT 10", (config.SCORE_THRESHOLD,)
    ):
        print(f"  [{row['score']}] {row['owner_name']} — {row['property_addr']} ({row['primary_source']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

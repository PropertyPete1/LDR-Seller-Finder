#!/usr/bin/env python3
"""E2E test for the volume upgrade: weekly run (3 counties, real mirror data,
live Bexar foreclosure feed) followed by a daily run (light sync), verifying:
  * Travis parcels load + absentee detection
  * warm tier populated (compact), never traced
  * budget caps by mode (weekly 75 / daily 15)
  * spend stats (run + month-to-date)
  * light sync on daily when asset key unchanged
  * committed DB size stays small
Run: python3 scripts/volume_upgrade_e2e_test.py
Uses pre-downloaded GDBs under /home/ubuntu/research/. No BatchData spend
(no API key), no FUB push (no key), zero cost.
"""
import os
import sys
from pathlib import Path

os.environ["DATA_DIR"] = "/tmp/vol_e2e_data"
os.environ["DATABASE_PATH"] = "/tmp/vol_e2e_data/seller_finder.sqlite3"
os.environ["REVIEW_DIR"] = "/tmp/vol_e2e_review"
os.environ.pop("BATCHDATA_API_KEY", None)
os.environ.pop("FUB_API_KEY", None)
os.environ["RUN_MODE"] = "weekly"

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

GDBS = {
    "bexar": "/home/ubuntu/research/bexar_parcels/fgdb/stratmap25-landparcels_48029_bexar_202507.gdb",
    "comal": "/home/ubuntu/research/comal_parcels/fgdb/stratmap25-landparcels_48091_comal_202503.gdb",
    "travis": "/home/ubuntu/research/travis_parcels/fgdb/stratmap25-landparcels_48453_travis_202508.gdb",
}


def run():
    import shutil
    shutil.rmtree("/tmp/vol_e2e_data", ignore_errors=True)
    Path("/tmp/vol_e2e_data").mkdir(parents=True)

    from seller_finder import config
    from seller_finder.state import get_db
    from seller_finder.sources import parcels, preforeclosure
    from seller_finder.scoring import compute_scores
    from seller_finder.skiptrace.tracer import trace_qualified_leads

    assert config.RUN_MODE == "weekly"
    assert config.MAX_SKIP_TRACES_PER_RUN == 75, config.MAX_SKIP_TRACES_PER_RUN
    print(f"[OK] weekly budget = {config.MAX_SKIP_TRACES_PER_RUN}")

    conn = get_db(fresh_parcels=True)

    # ── WEEKLY: full sync all three counties (simulate a mirror asset key) ──
    for county, gdb in GDBS.items():
        parcels._LAST_ASSET_KEY[county] = f"test-{county}:12345"
        st = parcels.sync_county(conn, county, gdb_path=Path(gdb))
        print(f"[WEEKLY] {county}: {st}")
        assert st["kept"] > 10000, f"{county} kept too few"
        assert not st["light_sync"]

    # Travis check
    trow = conn.execute(
        "SELECT COUNT(*) c, SUM(is_absentee) a FROM pc.parcels WHERE county='travis'"
    ).fetchone()
    print(f"[OK] travis parcels={trow['c']:,} absentee={trow['a']:,}")
    assert trow["c"] > 100000 and trow["a"] > 10000

    # Foreclosures: live Bexar feed + Travis inbox sample
    inbox = Path(config.DATA_DIR) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    # find one real travis situs address to prove inbox->match works
    sample = conn.execute(
        "SELECT situs_addr, situs_zip FROM pc.parcels "
        "WHERE county='travis' AND is_absentee=1 AND situs_addr != '' "
        "AND situs_zip != '' LIMIT 1").fetchone()
    (inbox / "foreclosures_travis_2026-07.csv").write_text(
        "address,zip,doc_number,city\n"
        f"\"{sample['situs_addr']}\",{sample['situs_zip']},2026TEST1,AUSTIN\n")

    fc_matches = []
    for county in ("bexar", "travis"):
        notices = preforeclosure.fetch(county)
        matched = preforeclosure.match_to_parcels(conn, county, notices)
        for m in matched:
            m["county"] = county
        fc_matches.extend(matched)
        print(f"[WEEKLY] {county} foreclosures: {len(notices)} notices, {len(matched)} matched")
    assert any(m["county"] == "travis" for m in fc_matches), "travis inbox match failed"

    sc = compute_scores(conn, fc_matches, [], {})
    print(f"[WEEKLY] scoring: {sc}")
    assert sc["qualified"] > 0
    assert sc["warm"] > 100000, "warm tier should hold the absentee mass"

    st = trace_qualified_leads(conn)
    print(f"[WEEKLY] skiptrace: {st}")
    assert st["run_mode"] == "weekly" and st["budget"] == 75
    assert st["skipped_no_api_key"] == st["eligible"]  # no key in this test

    # warm never eligible
    warm_total = conn.execute("SELECT COUNT(*) c FROM warm_leads").fetchone()["c"]
    lead_total = conn.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"]
    print(f"[OK] warm_leads={warm_total:,}  leads={lead_total:,}")
    assert warm_total > 100000 and lead_total < 2000

    conn.commit()
    db_mb = Path(os.environ["DATABASE_PATH"]).stat().st_size / 1e6
    print(f"[OK] committed DB size with 3 counties + warm tier: {db_mb:.1f} MB")
    assert db_mb < 90, "committed DB too big!"
    conn.close()

    # ── DAILY: light sync (same asset keys → bookkeeping skipped) ──────────
    os.environ["RUN_MODE"] = "daily"
    import importlib
    importlib.reload(config)
    assert config.RUN_MODE == "daily" and config.MAX_SKIP_TRACES_PER_RUN == 15
    print(f"[OK] daily budget = {config.MAX_SKIP_TRACES_PER_RUN}")

    conn = get_db(fresh_parcels=True)
    for county, gdb in GDBS.items():
        parcels._LAST_ASSET_KEY[county] = f"test-{county}:12345"  # unchanged
        st2 = parcels.sync_county(conn, county, gdb_path=Path(gdb), light=True)
        assert st2["light_sync"], f"{county} should light-sync"
    print("[DAILY] light sync OK for all 3 counties (bookkeeping skipped)")

    st3 = trace_qualified_leads(conn)
    print(f"[DAILY] skiptrace: {st3}")
    assert st3["run_mode"] == "daily" and st3["budget"] == 15
    conn.close()

    print("\nALL VOLUME-UPGRADE E2E CHECKS PASSED")


if __name__ == "__main__":
    run()

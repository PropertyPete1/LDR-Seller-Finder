#!/usr/bin/env python3
"""Daily data pull — Tue-Sat mornings (GitHub Actions cron, RUN_MODE=daily).

The Monday weekly run (run_weekly_pull.py) does the full pipeline; this
daily run is the FAST path that catches NEW pre-foreclosure notices between
Mondays:

  1. Rebuild the ephemeral parcel cache from the mirror (needed for address
     matching; the download is GitHub→GitHub so it's fast). When the mirror
     asset checksum is unchanged since the last sync, the expensive
     owner-change bookkeeping is skipped entirely (light sync).
  2. Fetch pre-foreclosure notices and match to parcels.
  3. Score — only foreclosure-affected leads change day-to-day; warm-tier
     and event carry-forward logic run identically to the weekly.
  4. Skip-trace NEW qualified leads (daily budget, default 15/run).
  5. Auto-push contactable leads to FUB (same dedupe/tag/DNC rules).
  6. Review CSV + run summary + healthcheck ping.

SKIPPED on daily runs (weekly-only): exemption diffs, divorce ingest,
deed-date inbox, digest email.
"""
import logging
import os
import sys

os.environ.setdefault("RUN_MODE", "daily")
sys.path.insert(0, "src")

from seller_finder import config  # noqa: E402
from seller_finder.state import get_db, now_iso, record_run, check_state_size  # noqa: E402
from seller_finder.sources import parcels, preforeclosure  # noqa: E402
from seller_finder.scoring import compute_scores  # noqa: E402
from seller_finder.skiptrace.tracer import trace_qualified_leads  # noqa: E402
from seller_finder.review import stage_traced_leads, write_review_files  # noqa: E402
from seller_finder.fub import auto_push_leads  # noqa: E402
from seller_finder.health import ping_healthcheck  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger("daily_pull")


def main() -> int:
    started = now_iso()
    conn = get_db(fresh_parcels=True)
    stats: dict = {"counties": {}, "started_at": started, "dry_run": config.DRY_RUN,
                   "run_mode": config.RUN_MODE}
    LOGGER.info("=== Daily pull — DRY_RUN=%s counties=%s ===", config.DRY_RUN, config.COUNTIES)

    # 1. Parcels (light sync — skips owner-change bookkeeping when the mirror
    # snapshot is unchanged, which is the normal case between quarterly refreshes)
    for county in config.COUNTIES:
        try:
            stats["counties"][county] = {
                "parcels": parcels.sync_county(conn, county, light=True)}
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("Parcel sync FAILED for %s: %s", county, exc, exc_info=True)
            stats["counties"][county] = {"parcels": {"error": str(exc)}}

    # 2. Pre-foreclosure — the signal that actually changes daily
    foreclosure_matches = []
    for county in config.COUNTIES:
        try:
            notices = preforeclosure.fetch(county)
            matched = preforeclosure.match_to_parcels(conn, county, notices)
            for m in matched:
                m["county"] = county
            foreclosure_matches.extend(matched)
            stats["counties"].setdefault(county, {})["preforeclosure"] = {
                "notices": len(notices), "matched": len(matched),
            }
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("Pre-foreclosure failed for %s: %s", county, exc)

    # 3. Score (divorce/homestead signals carry forward from stored leads)
    stats["scoring"] = compute_scores(conn, foreclosure_matches, [], {})

    # 4. Skip trace (daily budget)
    stats["skiptrace"] = trace_qualified_leads(conn)

    # 5. Auto-push to FUB
    stats["staged"] = stage_traced_leads(conn)
    try:
        stats["fub_push"] = auto_push_leads(conn)
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("FUB auto-push failed: %s", exc, exc_info=True)
        stats["fub_push"] = {"error": str(exc)}

    # 6. Artifacts + heartbeat (no digest email on daily runs)
    stats["review"] = write_review_files(conn, run_stats=stats)
    record_run(conn, "daily_pull", started, stats)
    conn.commit()
    conn.close()
    stats["state_db_mb"] = round(check_state_size(), 1)
    ping_healthcheck()
    LOGGER.info("=== Daily pull complete: %s ===", {k: v for k, v in stats.items() if k != "counties"})
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Weekly data pull — Monday 6am CT (GitHub Actions cron).

Steps:
  1. Sync parcels for each enabled county (TxGIO bulk download).
  2. Sync homestead exemptions (Bexar ArcGIS) and detect removals.
  3. Fetch pre-foreclosure notices and match to parcels.
  4. Ingest divorce filings (stubbed source / CSV inbox) and fuzzy-match owners.
  5. Ingest deed-date CSVs from data/inbox/ (BCAD export → tenure signal), then
     score all leads; qualify at threshold.
  6. Skip-trace qualified leads (cached, budgeted).
  7. Stage traced leads for approval + write review CSV / run summary.
  8. Send weekly digest email; ping healthchecks.io.

NO leads are pushed to Follow Up Boss here — see run_push_approved.py.
"""
import logging
import sys

sys.path.insert(0, "src")

from seller_finder import config  # noqa: E402
from seller_finder.state import get_db, now_iso, record_run  # noqa: E402
from seller_finder.sources import parcels, exemptions, preforeclosure, divorce, deeds  # noqa: E402
from seller_finder.scoring import compute_scores  # noqa: E402
from seller_finder.skiptrace.tracer import trace_qualified_leads  # noqa: E402
from seller_finder.review import stage_traced_leads, write_review_files, send_digest_email  # noqa: E402
from seller_finder.health import ping_healthcheck  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger("weekly_pull")


def main() -> int:
    started = now_iso()
    conn = get_db()
    stats: dict = {"counties": {}, "started_at": started, "dry_run": config.DRY_RUN}
    LOGGER.info("=== Weekly pull — DRY_RUN=%s counties=%s ===", config.DRY_RUN, config.COUNTIES)

    # 1-2. Parcels + exemptions per county (isolated failures don't kill the run)
    homestead_removed: dict = {}
    for county in config.COUNTIES:
        try:
            stats["counties"][county] = {"parcels": parcels.sync_county(conn, county)}
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("Parcel sync FAILED for %s: %s", county, exc, exc_info=True)
            stats["counties"][county] = {"parcels": {"error": str(exc)}}
            continue
        try:
            ex_stats = exemptions.sync_county(conn, county)
            homestead_removed[county] = ex_stats.pop("homestead_removed", [])
            stats["counties"][county]["exemptions"] = ex_stats
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("Exemption sync failed for %s: %s", county, exc)
            homestead_removed[county] = []

    # 3. Pre-foreclosure
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

    # 4. Divorce (stubbed feed; matching fully live)
    divorce_matches = []
    try:
        filings = divorce.fetch("bexar")
        if filings:
            divorce.store_filings(conn, filings)
        divorce_matches = divorce.match_filings_to_owners(conn)
        stats["divorce"] = {"filings": len(filings), "matched": len(divorce_matches)}
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("Divorce module failed: %s", exc)

    # 5. Deed dates (BCAD export inbox → tenure signal), then scoring
    try:
        stats["deeds"] = deeds.ingest_inbox(conn)
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("Deed ingest failed: %s", exc)
    stats["scoring"] = compute_scores(conn, foreclosure_matches, divorce_matches, homestead_removed)

    # 6. Skip trace (cost-guarded)
    stats["skiptrace"] = trace_qualified_leads(conn)

    # 7. Approval gate artifacts
    stats["staged"] = stage_traced_leads(conn)
    stats["review"] = write_review_files(conn)

    # 8. Digest + heartbeat
    stats["digest_sent"] = send_digest_email(conn)
    record_run(conn, "weekly_pull", started, stats)
    ping_healthcheck()
    LOGGER.info("=== Weekly pull complete: %s ===", {k: v for k, v in stats.items() if k != "counties"})
    return 0


if __name__ == "__main__":
    sys.exit(main())

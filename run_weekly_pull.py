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
  7. AUTO-PUSH qualified, skip-traced, contactable leads to Follow Up Boss
     (dedupe by email/phone/address; "Seller Lead" + source tags; DNC tag for
     flagged owners; leads with no email AND no phone are held, not pushed).
  8. Write review CSV (permanent push record) + run summary.
  9. Send weekly digest email; ping healthchecks.io.

The push-approved workflow (run_push_approved.py) remains a manual fallback
for retrying failed/held pushes.
"""
import logging
import sys

sys.path.insert(0, "src")

from seller_finder import config  # noqa: E402
from seller_finder.state import get_db, now_iso, record_run, check_state_size  # noqa: E402
from seller_finder.sources import parcels, exemptions, preforeclosure, divorce, deeds  # noqa: E402
from seller_finder.scoring import compute_scores  # noqa: E402
from seller_finder.skiptrace.tracer import trace_qualified_leads  # noqa: E402
from seller_finder.review import stage_traced_leads, write_review_files, send_digest_email  # noqa: E402
from seller_finder.fub import auto_push_leads  # noqa: E402
from seller_finder.health import collect_stage_errors, ping_healthcheck  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger("weekly_pull")


def main() -> int:
    started = now_iso()
    # fresh_parcels: the parcel snapshot is ephemeral — rebuilt from the
    # mirror download every run, never committed (GitHub 100MB file limit).
    conn = get_db(fresh_parcels=True)
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
            stats["counties"][county]["exemptions"] = {"error": str(exc)}

    # 3. Pre-foreclosure
    foreclosure_matches = []
    for county in config.COUNTIES:
        try:
            notices = preforeclosure.fetch(county, conn=conn)
            matched = preforeclosure.match_to_parcels(conn, county, notices)
            for m in matched:
                m["county"] = county
            foreclosure_matches.extend(matched)
            stats["counties"].setdefault(county, {})["preforeclosure"] = {
                "notices": len(notices), "matched": len(matched),
            }
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("Pre-foreclosure failed for %s: %s", county, exc)
            stats["counties"].setdefault(county, {})["preforeclosure"] = {"error": str(exc)}

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
        stats["divorce"] = {"error": str(exc)}

    # 5. Deed dates (BCAD export inbox → tenure signal), then scoring
    try:
        stats["deeds"] = deeds.ingest_inbox(conn)
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("Deed ingest failed: %s", exc)
        stats["deeds"] = {"error": str(exc)}
    stats["scoring"] = compute_scores(conn, foreclosure_matches, divorce_matches, homestead_removed)

    # 6. Skip trace (cost-guarded)
    stats["skiptrace"] = trace_qualified_leads(conn)

    # 7. Auto-push to Follow Up Boss (contactable leads only; DNC tagged)
    stats["staged"] = stage_traced_leads(conn)
    try:
        stats["fub_push"] = auto_push_leads(conn)
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("FUB auto-push failed: %s", exc, exc_info=True)
        stats["fub_push"] = {"error": str(exc)}

    # 8. Run-record artifacts (CSV documents what was pushed/held)
    stats["review"] = write_review_files(conn, run_stats=stats)

    # 9. Digest + heartbeat + committed-DB size guard
    stats["digest_sent"] = send_digest_email(conn, run_stats=stats)
    # See run_daily_pull: per-stage try/except would otherwise let a totally
    # failed run exit 0 and ping the dead-man's switch green.
    failures = collect_stage_errors(stats)
    stats["stage_failures"] = failures
    record_run(conn, "weekly_pull", started, stats)
    conn.commit()
    conn.close()
    stats["state_db_mb"] = round(check_state_size(), 1)
    ping_healthcheck(failed=bool(failures))
    LOGGER.info("=== Weekly pull complete: %s ===", {k: v for k, v in stats.items() if k != "counties"})
    if failures:
        LOGGER.error("Weekly pull finished with %d failed stage(s): %s",
                     len(failures), ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

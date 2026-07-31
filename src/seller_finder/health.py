"""healthchecks.io dead-man's switch + the run-level health verdict.

A run that swallowed stage errors must NOT ping success: the whole point of the
switch is to page when the pipeline stops working, and every stage in the
runners is wrapped in try/except so the process exits 0 even when nothing was
downloaded, scored, or pushed. Pass failed=True to hit the /fail endpoint
instead (healthchecks.io convention), which flips the check to DOWN immediately.

`collect_stage_errors` is what turns swallowed exceptions back into that
verdict. It deliberately looks past raised exceptions: the failures that
actually cost this system money are the ones where every stage "succeeded" and
produced nothing — a 403'd token erroring every trace, a FUB outage failing
every push, a blocked mirror yielding zero parcel rows. Those all used to exit 0
and ping the switch green.
"""
import logging

import requests

from . import config

LOGGER = logging.getLogger("health")


def ping_healthcheck(failed: bool = False, url: str | None = None) -> None:
    """GET the HEALTHCHECK_URL (or its /fail variant). Never crashes the run.

    url : override the target. The two scheduled runs share one check; a
          hand-triggered workflow must pass its own URL (or None) so a manual
          retry can never reset the dead-man's switch for a cron that has
          silently stopped firing.
    """
    target_base = config.HEALTHCHECK_URL if url is None else url
    if not target_base:
        LOGGER.info("HEALTHCHECK_URL not set — heartbeat ping SKIPPED (optional)")
        return
    if config.DRY_RUN:
        LOGGER.info("[DRY-RUN] Skipping healthchecks.io ping (failed=%s)", failed)
        return
    target = f"{target_base.rstrip('/')}/fail" if failed else target_base
    try:
        resp = requests.get(target, timeout=10)
        LOGGER.info("healthchecks.io pinged (%s): %s",
                    "FAIL" if failed else "ok", resp.status_code)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("healthchecks.io ping failed: %s", exc)


def collect_stage_errors(stats: dict) -> list[str]:
    """Names of pipeline stages that failed this run. Empty list = healthy.

    Two kinds of failure are detected:

      1. RECORDED — a stage caught an exception and stored {"error": ...}.
      2. SILENT — a stage finished without raising but produced a result that
         cannot be correct. These are the expensive ones, and every check below
         is an incident this pipeline can actually have:

         * every skip-trace call errored (revoked token / empty PayGo wallet).
           This is precisely the 403 incident that poisoned 64 cache entries;
           it exited 0 and pinged the switch green.
         * every FUB push failed (FUB outage / bad key) — leads pile up in
           awaiting_approval and nobody is told.
         * a parcel sync kept zero rows (blocked mirror, empty release asset,
           changed GDB schema). The README calls this out as the canonical
           silent failure, and the entire candidate universe comes from it.
         * an exemption pull tripped the truncation guard — the snapshot is
           stale and homestead-removed detection did not run at all.
         * an inbox file failed to parse — a county export committed by hand
           was silently ignored.
    """
    failures: list[str] = []

    for county, sections in (stats.get("counties") or {}).items():
        for stage, result in (sections or {}).items():
            if not isinstance(result, dict):
                continue
            if result.get("error"):
                failures.append(f"{stage}:{county}")
                continue
            if stage == "parcels" and not result.get("kept", 0):
                # kept==0 with rows>0 means every row was filtered out (schema
                # or field-name change); rows==0 means the download was empty.
                failures.append(f"parcels-empty:{county}")
            if stage == "exemptions" and result.get("truncated_feed"):
                failures.append(f"exemptions-truncated:{county}")

    for stage in ("fub_push", "scoring", "skiptrace", "divorce", "deeds", "review"):
        result = stats.get(stage)
        if isinstance(result, dict) and result.get("error"):
            failures.append(stage)

    st = stats.get("skiptrace")
    if isinstance(st, dict) and st.get("errors") and not st.get("traced"):
        # Nothing got through. A partial error rate is normal and stays a
        # warning in the diagnostics table; a total wipeout is an incident.
        failures.append("skiptrace-all-errors")

    fp = stats.get("fub_push")
    if isinstance(fp, dict) and fp.get("failed") and not fp.get("pushed"):
        failures.append("fub_push-all-failed")

    for stage in ("deeds", "divorce"):
        result = stats.get(stage)
        if isinstance(result, dict) and result.get("file_errors"):
            failures.append(f"{stage}-inbox-unreadable")

    return failures

"""healthchecks.io dead-man's switch — pinged at the END of a run.

A run that swallowed stage errors must NOT ping success: the whole point of the
switch is to page when the pipeline stops working, and every stage in the
runners is wrapped in try/except so the process exits 0 even when nothing was
downloaded, scored, or pushed. Pass failed=True to hit the /fail endpoint
instead (healthchecks.io convention), which flips the check to DOWN immediately.
"""
import logging

import requests

from . import config

LOGGER = logging.getLogger("health")


def ping_healthcheck(failed: bool = False) -> None:
    """GET the HEALTHCHECK_URL (or its /fail variant). Never crashes the run."""
    url = config.HEALTHCHECK_URL
    if not url:
        LOGGER.info("HEALTHCHECK_URL not set — heartbeat ping SKIPPED (optional)")
        return
    if config.DRY_RUN:
        LOGGER.info("[DRY-RUN] Skipping healthchecks.io ping (failed=%s)", failed)
        return
    target = f"{url.rstrip('/')}/fail" if failed else url
    try:
        resp = requests.get(target, timeout=10)
        LOGGER.info("healthchecks.io pinged (%s): %s",
                    "FAIL" if failed else "ok", resp.status_code)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("healthchecks.io ping failed: %s", exc)


def collect_stage_errors(stats: dict) -> list[str]:
    """Names of pipeline stages that recorded an error in this run's stats.

    The runners catch per-stage exceptions so one bad county can't kill the
    whole run; this is what turns those swallowed errors back into a run-level
    verdict for the exit code and the dead-man's switch.
    """
    failures: list[str] = []
    for county, sections in (stats.get("counties") or {}).items():
        for stage, result in (sections or {}).items():
            if isinstance(result, dict) and result.get("error"):
                failures.append(f"{stage}:{county}")
    for stage in ("fub_push", "scoring", "skiptrace", "divorce", "deeds", "review"):
        result = stats.get(stage)
        if isinstance(result, dict) and result.get("error"):
            failures.append(stage)
    return failures

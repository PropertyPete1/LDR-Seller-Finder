"""healthchecks.io dead-man's switch — pinged at the END of a successful run."""
import logging

import requests

from . import config

LOGGER = logging.getLogger("health")


def ping_healthcheck() -> None:
    """GET the HEALTHCHECK_URL. Failures are logged, never crash the run."""
    url = config.HEALTHCHECK_URL
    if not url:
        LOGGER.warning("HEALTHCHECK_URL not set — dead-man's switch not configured")
        return
    if config.DRY_RUN:
        LOGGER.info("[DRY-RUN] Skipping healthchecks.io ping")
        return
    try:
        resp = requests.get(url, timeout=10)
        LOGGER.info("healthchecks.io pinged: %s", resp.status_code)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("healthchecks.io ping failed: %s", exc)

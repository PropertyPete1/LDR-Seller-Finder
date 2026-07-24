#!/usr/bin/env python3
"""Push approved leads to Follow Up Boss.

ONLY runs when the push-approved workflow is manually triggered
(workflow_dispatch) — this is the approval gate. It pushes every lead in
'awaiting_approval'. To exclude specific leads before pushing, run the
exclude script first (scripts/exclude_leads.py) or pass lead IDs via the
workflow's exclude_ids input.
"""
import argparse
import logging
import sys

sys.path.insert(0, "src")

from seller_finder import config  # noqa: E402
from seller_finder.state import get_db, now_iso, record_run  # noqa: E402
from seller_finder.fub import push_approved_leads  # noqa: E402
from seller_finder.health import ping_healthcheck  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger("push_approved")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exclude-ids", default="",
                        help="Comma-separated lead IDs to skip (marked 'skipped')")
    args = parser.parse_args()

    started = now_iso()
    conn = get_db()

    if args.exclude_ids.strip():
        ids = [int(x) for x in args.exclude_ids.split(",") if x.strip().isdigit()]
        if ids:
            q = ",".join("?" * len(ids))
            cur = conn.execute(
                f"UPDATE leads SET status='skipped', updated_at=? "
                f"WHERE id IN ({q}) AND status='awaiting_approval'",
                [now_iso(), *ids],
            )
            conn.commit()
            LOGGER.info("Excluded %d leads: %s", cur.rowcount, ids)

    LOGGER.info("=== Push approved — DRY_RUN=%s ===", config.DRY_RUN)
    stats = push_approved_leads(conn)
    record_run(conn, "push_approved", started, stats)
    ping_healthcheck()
    LOGGER.info("=== Push complete: %s ===", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())

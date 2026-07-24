"""Skip-trace orchestration: qualified leads → cached, budgeted tracing.

Cost controls:
  * Only leads with score >= threshold are traced.
  * skip_traces table caches results by owner_key (name + mail zip) — an
    owner is NEVER paid for twice, even across counties/properties.
  * MAX_SKIP_TRACES_PER_RUN caps spend per weekly run.
"""
import json
import logging
import re

from .. import config
from ..state import now_iso, owner_key
from . import get_provider
from .base import SkipTraceRequest

LOGGER = logging.getLogger("skiptrace.tracer")


def _split_owner_name(owner_name: str) -> tuple[str, str]:
    """Parcel owner names are 'LAST FIRST MIDDLE' or 'LAST FIRST & SPOUSE'."""
    name = re.split(r"[&,]", owner_name or "")[0].strip()
    parts = name.split()
    if len(parts) >= 2:
        return parts[1], parts[0]  # first, last
    return "", name


def _split_situs(property_addr: str) -> tuple[str, str, str, str]:
    """'123 MAIN ST, SAN ANTONIO TX 78201' → (street, city, state, zip)."""
    m = re.match(r"^(.*?),\s*(.*?)\s+TX\s+(\d{5})", property_addr or "")
    if m:
        return m.group(1).strip(), m.group(2).strip(), "TX", m.group(3)
    return (property_addr or "").strip(), "", "TX", ""


def trace_qualified_leads(conn, provider_name: str = "batchdata") -> dict:
    """Trace all qualified, untraced leads (budget/cached). Returns stats.

    OPTIONAL SECRET: if BATCHDATA_API_KEY is not configured, skip tracing is
    skipped entirely and qualified leads advance to the review stage without
    contact info (stats["skipped_no_api_key"] reports how many).
    """
    stats = {"eligible": 0, "cached": 0, "traced": 0, "matched": 0,
             "budget_skipped": 0, "skipped_no_api_key": 0}
    # 'qualified' = new this run; 'held_no_contact' without a real trace = leads
    # that advanced before BATCHDATA_API_KEY existed — retrace them once the key
    # is added so they can be pushed (the cache still prevents double billing).
    leads = conn.execute(
        "SELECT id, county, prop_id, owner_name, property_addr, mail_addr "
        "FROM leads WHERE (status='qualified' OR "
        "(status='held_no_contact' AND skip_trace_id IS NULL)) "
        "AND score>=? ORDER BY score DESC",
        (config.SCORE_THRESHOLD,),
    ).fetchall()
    stats["eligible"] = len(leads)
    if not leads:
        return stats

    if not config.BATCHDATA_API_KEY:
        # No skip-trace provider configured — advance leads without contact info.
        for lead in leads:
            conn.execute(
                "UPDATE leads SET status='traced', updated_at=? WHERE id=?",
                (now_iso(), lead["id"]),
            )
        conn.commit()
        stats["skipped_no_api_key"] = len(leads)
        LOGGER.warning(
            "BATCHDATA_API_KEY not set — skip tracing SKIPPED for %d qualified "
            "leads; they will appear in the review CSV without contact info. "
            "Add the secret to unlock phones/emails.", len(leads),
        )
        return stats

    provider = get_provider(provider_name)
    to_trace: list[tuple[dict, SkipTraceRequest, str]] = []
    queued_keys: dict[str, list[int]] = {}  # within-run dedupe: same owner, many parcels
    budget = config.MAX_SKIP_TRACES_PER_RUN

    for lead in leads:
        lead = dict(lead)
        okey = owner_key(lead["owner_name"], _mail_zip(lead["mail_addr"]))
        if okey in queued_keys:
            queued_keys[okey].append(lead["id"])
            stats["cached"] += 1
            continue
        cached = conn.execute(
            "SELECT id FROM skip_traces WHERE owner_key=?", (okey,)
        ).fetchone()
        if cached:
            conn.execute(
                "UPDATE leads SET skip_trace_id=?, status='traced', updated_at=? WHERE id=?",
                (cached["id"], now_iso(), lead["id"]),
            )
            stats["cached"] += 1
            continue
        if len(to_trace) >= budget:
            stats["budget_skipped"] += 1
            continue
        street, city, state, zip_code = _split_situs(lead["property_addr"])
        first, last = _split_owner_name(lead["owner_name"])
        to_trace.append((lead, SkipTraceRequest(
            street=street, city=city, state=state, zip=zip_code,
            owner_first=first, owner_last=last,
        ), okey))
        queued_keys[okey] = []

    if config.DRY_RUN:
        LOGGER.info("[DRY-RUN] Would skip-trace %d owners", len(to_trace))
        conn.commit()
        stats["traced"] = 0
        return stats

    if to_trace:
        results = provider.trace_batch([req for _, req, _ in to_trace])
        for (lead, _req, okey), result in zip(to_trace, results):
            cur = conn.execute(
                """INSERT OR IGNORE INTO skip_traces
                   (owner_key, provider, matched, emails, phones, dnc, litigator, raw, traced_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (okey, result.provider, int(result.matched), json.dumps(result.emails),
                 json.dumps(result.phones), int(result.dnc), int(result.litigator),
                 json.dumps(result.raw)[:100000], now_iso()),
            )
            trace_id = cur.lastrowid if cur.rowcount else conn.execute(
                "SELECT id FROM skip_traces WHERE owner_key=?", (okey,)
            ).fetchone()["id"]
            conn.execute(
                "UPDATE leads SET skip_trace_id=?, status='traced', updated_at=? WHERE id=?",
                (trace_id, now_iso(), lead["id"]),
            )
            # Attach the same trace to other leads for this owner (within-run dupes)
            for dup_lead_id in queued_keys.get(okey, []):
                conn.execute(
                    "UPDATE leads SET skip_trace_id=?, status='traced', updated_at=? WHERE id=?",
                    (trace_id, now_iso(), dup_lead_id),
                )
            stats["traced"] += 1
            stats["matched"] += int(result.matched)

    conn.commit()
    LOGGER.info("Skip tracing: %s", stats)
    return stats


def _mail_zip(mail_addr: str) -> str:
    m = re.search(r"(\d{5})(?:-\d{4})?\s*$", mail_addr or "")
    return m.group(1) if m else ""

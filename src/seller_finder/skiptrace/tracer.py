"""Skip-trace orchestration: qualified leads → cached, budgeted tracing.

Cost controls:
  * Only leads with score >= threshold are traced.
  * skip_traces table caches results by owner_key (name + mail zip) — an
    owner is NEVER paid for twice, even across counties/properties.
  * MAX_SKIP_TRACES_PER_RUN caps spend per run, resolved by RUN_MODE
    (settings.skip_trace_budget: weekly 75, daily 15). Scheduled worst case is
    5 weekly + 22 daily runs = 705 traces ≈ $105.75/month at $0.15. This is a
    PER-RUN cap: mtd_traces below is reporting only, not a monthly ceiling.
  * Month-to-date trace count + estimated spend are reported in every run's
    diagnostics and the Monday digest (skip_trace_cost_usd, default $0.15).

Error vs no-match discipline:
  * API ERRORS (provider returns result.error) are never cached and never
    marked traced — the lead stays 'qualified' and is retried next run.
  * Genuine NO-MATCH results are cached, but expire after
    no_match_retrace_days (settings.yaml, default 90) so owners can be
    re-traced later; MATCHED results are cached forever (already paid for).
"""
import datetime as _dt
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
             "no_match": 0, "errors": 0, "top_error": "",
             "budget_skipped": 0, "skipped_no_api_key": 0,
             "budget": config.MAX_SKIP_TRACES_PER_RUN, "run_mode": config.RUN_MODE,
             "run_cost_usd": 0.0, "mtd_traces": 0, "mtd_cost_usd": 0.0}
    # Expire stale no-match cache entries FIRST so their leads re-enter the
    # eligibility query below in this same run.
    if config.BATCHDATA_API_KEY:
        _expire_stale_no_matches(conn)
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
        _add_spend_stats(conn, stats)
        return stats

    if not config.BATCHDATA_API_KEY:
        # No skip-trace provider configured — advance leads without contact info.
        for lead in leads:
            conn.execute(
                "UPDATE leads SET status='traced', updated_at=? WHERE id=?",
                (now_iso(), lead["id"]),
            )
        conn.commit()
        _add_spend_stats(conn, stats)
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
        LOGGER.info("[DRY-RUN] Would skip-trace %d owners (budget %d, %s run)",
                    len(to_trace), budget, config.RUN_MODE)
        conn.commit()
        stats["traced"] = 0
        stats["would_trace"] = len(to_trace)
        # Still report month-to-date spend: a dry run is how you check the
        # budget before a real one, and this early return used to skip it,
        # leaving the diagnostics table showing $0.00 MTD.
        _add_spend_stats(conn, stats)
        return stats

    if to_trace:
        error_counts: dict[str, int] = {}
        results = provider.trace_batch([req for _, req, _ in to_trace])
        for (lead, _req, okey), result in zip(to_trace, results):
            if result.error:
                # API failure — do NOT cache, do NOT mark traced. The lead
                # keeps its current status ('qualified'/'held_no_contact')
                # and is retried automatically on the next run.
                stats["errors"] += 1
                error_counts[result.error] = error_counts.get(result.error, 0) + 1
                continue
            cur = conn.execute(
                """INSERT OR IGNORE INTO skip_traces
                   (owner_key, provider, matched, emails, phones, dnc, litigator, raw, traced_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (okey, result.provider, int(result.matched), json.dumps(result.emails),
                 json.dumps(result.phones), int(result.dnc), int(result.litigator),
                 json.dumps(_provenance(result)), now_iso()),
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
            stats["no_match"] += int(not result.matched)
        if error_counts:
            stats["top_error"] = max(error_counts, key=error_counts.get)[:300]
            LOGGER.error(
                "Skip trace: %d API errors this run (NOT cached, will retry next "
                "run). Top error: %s", stats["errors"], stats["top_error"])

    conn.commit()
    _add_spend_stats(conn, stats)
    LOGGER.info("Skip tracing: %s", stats)
    return stats


def _provenance(result) -> dict:
    """What we keep from a provider response, beyond the extracted columns.

    We used to store the entire BatchData person record (truncated at 100 KB).
    Nothing ever read it — the pipeline only uses emails/phones/dnc/litigator,
    which have their own columns — while it carried the full consumer profile
    the provider returns: relatives, address history, age/DOB, associated
    identities. Two problems with that:

      * Data minimisation. This is homeowner PII in a repo whose whole
        retention story is "encrypted state branch"; keeping identity data we
        never look at is pure liability.
      * Size. The committed DB has a hard 100 MB ceiling (GitHub rejects
        larger blobs). At up to 100 KB per owner, ~900 traced owners could
        blow the cap on this column alone.

    Keep only non-identifying provenance: enough to audit what happened and
    which provider/shape produced it.
    """
    raw = result.raw if isinstance(result.raw, dict) else {}
    return {
        "provider": result.provider,
        "matched": bool(result.matched),
        "email_count": len(result.emails or []),
        "phone_count": len(result.phones or []),
        "dnc": bool(result.dnc),
        "litigator": bool(result.litigator),
        # Field names only — never their values.
        "response_fields": sorted(raw.keys())[:40],
    }


def _add_spend_stats(conn, stats: dict) -> None:
    """Attach cost estimates: this run + month-to-date (from skip_traces)."""
    cost = config.SKIP_TRACE_COST_USD
    stats["run_cost_usd"] = round(stats["traced"] * cost, 2)
    month_prefix = _dt.datetime.now(config.CT).strftime("%Y-%m")
    row = conn.execute(
        "SELECT COUNT(*) c FROM skip_traces WHERE traced_at LIKE ?",
        (f"{month_prefix}%",)).fetchone()
    stats["mtd_traces"] = row["c"]
    stats["mtd_cost_usd"] = round(row["c"] * cost, 2)


def _expire_stale_no_matches(conn) -> int:
    """Delete cached NO-MATCH entries older than no_match_retrace_days.

    Matched results are kept forever (already paid for, data rarely improves
    week-to-week); no-matches expire so owners get re-traced periodically as
    providers refresh their data. Leads pointing at an expired trace are
    reset to 'qualified' so they re-enter the tracing queue.
    """
    days = int(config.SETTINGS.get("no_match_retrace_days", 90))
    cutoff = _dt.datetime.now(config.CT) - _dt.timedelta(days=days)
    stale = [r["id"] for r in conn.execute(
        "SELECT id FROM skip_traces WHERE matched=0 AND traced_at < ?",
        (cutoff.isoformat(timespec="seconds"),))]
    if not stale:
        return 0
    ph = ",".join("?" * len(stale))
    conn.execute(
        f"UPDATE leads SET status='qualified', skip_trace_id=NULL, updated_at=? "
        f"WHERE skip_trace_id IN ({ph}) AND status IN ('traced','held_no_contact')",
        (now_iso(), *stale))
    conn.execute(f"DELETE FROM skip_traces WHERE id IN ({ph})", stale)
    conn.commit()
    LOGGER.info("Expired %d stale no-match skip-trace cache entries (>%d days) — "
                "affected leads re-queued for tracing", len(stale), days)
    return len(stale)


def _mail_zip(mail_addr: str) -> str:
    m = re.search(r"(\d{5})(?:-\d{4})?\s*$", mail_addr or "")
    return m.group(1) if m else ""

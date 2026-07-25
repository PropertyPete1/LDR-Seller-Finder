"""Run-record artifacts: review CSV + run summary + weekly digest email.

The weekly run auto-pushes traced leads to FUB, then produces:
  * review/pending_leads.csv        — record of every lead handled this run
                                      (pushed / held_no_contact / awaiting retry),
                                      uploaded as an Actions artifact
  * review/run_summary.md           — written to the Actions job summary
The push-approved workflow remains as a manual fallback for leads that were
not auto-pushed (e.g. FUB errors, or runs before FUB_API_KEY was configured).
"""
import csv
import datetime as dt
import json
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from . import config
from .state import now_iso

LOGGER = logging.getLogger("review")


def stage_traced_leads(conn) -> int:
    """Move traced leads to awaiting_approval."""
    cur = conn.execute(
        "UPDATE leads SET status='awaiting_approval', updated_at=? WHERE status='traced'",
        (now_iso(),),
    )
    conn.commit()
    LOGGER.info("Staged %d leads for approval", cur.rowcount)
    return cur.rowcount


def write_review_files(conn, run_stats: dict | None = None) -> dict:
    """Write pending_leads.csv (push record) and run_summary.md. Returns stats.

    The CSV records every lead touched in the last 24h push cycle plus anything
    still awaiting: status column shows pushed / held_no_contact /
    awaiting_approval so there is a permanent artifact of what was sent to FUB."""
    config.REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = (dt.datetime.now(config.CT) - dt.timedelta(hours=24)).isoformat()
    rows = conn.execute(
        """SELECT l.id, l.county, l.prop_id, l.owner_name, l.property_addr, l.mail_addr,
                  l.score, l.signals, l.primary_source, l.status, l.fub_person_id,
                  st.matched AS trace_matched, st.emails, st.phones, st.dnc, st.litigator
           FROM leads l LEFT JOIN skip_traces st ON st.id = l.skip_trace_id
           WHERE l.status='awaiting_approval'
              OR (l.status IN ('pushed','held_no_contact') AND l.updated_at>=?)
           ORDER BY l.status, l.score DESC""", (cutoff,)
    ).fetchall()

    csv_path = config.REVIEW_DIR / "pending_leads.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["lead_id", "county", "prop_id", "owner_name", "property_address",
                    "mailing_address", "score", "primary_source", "signals", "status",
                    "fub_person_id", "trace_matched", "emails", "phones", "dnc", "litigator"])
        for r in rows:
            signals = "; ".join(
                f"{s['signal']}(+{s['points']})" for s in json.loads(r["signals"] or "[]")
            )
            phones = "; ".join(
                p.get("number", "") for p in json.loads(r["phones"] or "[]")
            ) if r["phones"] else ""
            emails = "; ".join(json.loads(r["emails"] or "[]")) if r["emails"] else ""
            w.writerow([r["id"], r["county"], r["prop_id"], r["owner_name"],
                        r["property_addr"], r["mail_addr"], r["score"],
                        r["primary_source"], signals, r["status"], r["fub_person_id"],
                        r["trace_matched"], emails, phones, r["dnc"], r["litigator"]])

    stats = _summary_stats(conn)
    md_path = config.REVIEW_DIR / "run_summary.md"
    with open(md_path, "w") as f:
        f.write(render_summary_md(stats, len(rows), run_stats=run_stats))
    LOGGER.info("Review files written: %s (%d pending leads)", csv_path, len(rows))
    return {"pending": len(rows), "csv": str(csv_path), "summary": str(md_path), **stats}


def _summary_stats(conn) -> dict:
    def one(sql, *args):
        row = conn.execute(sql, args).fetchone()
        return row[0] if row and row[0] is not None else 0

    week_ago = (dt.datetime.now(config.CT) - dt.timedelta(days=7)).isoformat()
    day_ago = (dt.datetime.now(config.CT) - dt.timedelta(hours=24)).isoformat()
    # "This run" = every qualified lead handled in the last 24h, whatever its
    # outcome (pushed / held for no contact info / awaiting a push retry).
    # NOTE: held_no_contact MUST be counted — an earlier version omitted it,
    # so runs without a BatchData key reported 0 in every band despite
    # finding qualified leads.
    handled = ("((status IN ('pushed','held_no_contact') AND updated_at>=?) "
               "OR status IN ('awaiting_approval','qualified','traced'))")
    buckets = {}
    for lo, hi in ((40, 54), (55, 69), (70, 84), (85, 100)):
        buckets[f"{lo}-{hi}"] = one(
            f"SELECT COUNT(*) FROM leads WHERE score BETWEEN ? AND ? AND {handled}",
            lo, hi, day_ago,
        )
    return {
        "new_leads_this_week": one("SELECT COUNT(*) FROM leads WHERE created_at>=?", week_ago),
        "pushed_this_run": one(
            "SELECT COUNT(*) FROM leads WHERE status='pushed' AND updated_at>=?", day_ago),
        "held_no_contact": one("SELECT COUNT(*) FROM leads WHERE status='held_no_contact'"),
        "awaiting_approval": one("SELECT COUNT(*) FROM leads WHERE status='awaiting_approval'"),
        "pushed_total": one("SELECT COUNT(*) FROM leads WHERE status='pushed'"),
        "traced_total": one("SELECT COUNT(*) FROM skip_traces"),
        "score_buckets": buckets,
        "by_source": {
            r["primary_source"]: r["n"] for r in conn.execute(
                f"SELECT primary_source, COUNT(*) n FROM leads WHERE {handled} "
                "GROUP BY primary_source", (day_ago,)
            )
        },
        "by_signal_combo": {
            r["combo"]: r["n"] for r in conn.execute(
                f"""SELECT combo, COUNT(*) n FROM (
                      SELECT (SELECT group_concat(je.value ->> '$.signal', ' + ')
                              FROM json_each(l.signals) je) AS combo
                      FROM leads l WHERE {handled}
                   ) GROUP BY combo ORDER BY n DESC""", (day_ago,)
            ) if r["combo"]
        },
    }


def _diagnostics_md(run_stats: dict | None) -> list[str]:
    """Per-source pipeline diagnostics — makes silent failures visible at a
    glance (e.g. a blocked parcel download shows rows=0 right in the summary)."""
    if not run_stats:
        return []
    lines = ["", "## Pipeline diagnostics (this run)", "",
             "| Stage | County | Result |", "|---|---|---|"]
    for county, c in (run_stats.get("counties") or {}).items():
        p = c.get("parcels") or {}
        if "error" in p:
            lines.append(f"| Parcels | {county} | ❌ ERROR: {p['error'][:120]} |")
        else:
            lines.append(
                f"| Parcels | {county} | rows {p.get('rows', 0):,} → kept "
                f"{p.get('kept', 0):,} → absentee {p.get('absentee', 0):,} "
                f"(owner changes {p.get('owner_changes', 0)}) |")
        ex = c.get("exemptions")
        if ex is not None:
            lines.append(
                f"| Exemptions | {county} | {ex.get('updated', 0):,} rows, homestead "
                f"{ex.get('homestead', 0):,} |")
        fc = c.get("preforeclosure")
        if fc is not None:
            lines.append(
                f"| Pre-foreclosure | {county} | {fc.get('notices', 0)} notices → "
                f"{fc.get('matched', 0)} matched to parcels |")
    dv = run_stats.get("divorce") or {}
    lines.append(f"| Divorce | bexar | {dv.get('filings', 0)} filings → "
                 f"{dv.get('matched', 0)} matched (stubbed source) |")
    de = run_stats.get("deeds") or {}
    lines.append(f"| Deed dates | — | {de.get('files', 0)} inbox files, "
                 f"{de.get('rows', 0):,} rows |")
    sc = run_stats.get("scoring") or {}
    lines.append(
        f"| Scoring | all | candidates {sc.get('candidates', 0):,} → qualified "
        f"{sc.get('qualified', 0):,} (below threshold {sc.get('below_threshold', 0):,}) |")
    lines.append(
        f"| Warm tier (never traced/pushed) | all | {sc.get('warm', 0):,} scored this "
        f"run, {sc.get('warm_total', 0):,} total stored, "
        f"{sc.get('warm_promoted', 0)} promoted to qualified |")
    st = run_stats.get("skiptrace") or {}
    lines.append(
        f"| Skip trace | all | eligible {st.get('eligible', 0)}, cached "
        f"{st.get('cached', 0)}, traced {st.get('traced', 0)} "
        f"(budget {st.get('budget', '?')}/{st.get('run_mode', 'weekly')} run, "
        f"budget-deferred {st.get('budget_skipped', 0)}), no-key-skipped "
        f"{st.get('skipped_no_api_key', 0)} |")
    lines.append(
        f"| Skip trace spend | all | this run {st.get('traced', 0)} × "
        f"${config.SKIP_TRACE_COST_USD:.2f} = ${st.get('run_cost_usd', 0):.2f}; "
        f"month-to-date {st.get('mtd_traces', 0)} traces ≈ "
        f"${st.get('mtd_cost_usd', 0):.2f} |")
    if st.get("errors"):
        lines.append(
            f"| Skip trace outcomes | all | ✅ matched {st.get('matched', 0)}, "
            f"no-match {st.get('no_match', 0)}, ❌ **API errors "
            f"{st.get('errors', 0)}** (not cached — will retry next run) |")
        lines.append(
            f"| Skip trace top error | all | `{str(st.get('top_error', ''))[:200]}` |")
    elif st.get("traced") or st.get("matched"):
        lines.append(
            f"| Skip trace outcomes | all | matched {st.get('matched', 0)}, "
            f"no-match {st.get('no_match', 0)}, errors 0 |")
    fp = run_stats.get("fub_push") or {}
    if "error" in fp:
        lines.append(f"| FUB push | all | ❌ ERROR: {str(fp['error'])[:120]} |")
    else:
        lines.append(
            f"| FUB push | all | pushed {fp.get('pushed', 0)}, held (no contact) "
            f"{fp.get('held_no_contact', 0)}, failed {fp.get('failed', 0)} |")
    return lines


def render_summary_md(stats: dict, pending: int, run_stats: dict | None = None) -> str:
    lines = [
        f"# LDR-Seller-Finder — {config.RUN_MODE.title()} Run Summary",
        "",
        f"**Run date:** {dt.datetime.now(config.CT):%Y-%m-%d %H:%M %Z}",
        "",
        f"| Metric | Value |",
        f"|---|---|",
        f"| New leads found this week | {stats['new_leads_this_week']} |",
        f"| Leads pushed to FUB this run | {stats['pushed_this_run']} |",
        f"| Held — no email/phone from skip trace | {stats['held_no_contact']} |",
        f"| Awaiting retry (push failed / no FUB key) | {stats['awaiting_approval']} |",
        f"| Leads pushed to FUB (all time) | {stats['pushed_total']} |",
        f"| Owners skip-traced (all time, cached) | {stats['traced_total']} |",
        "",
        "## Score breakdown (this run)",
        "",
        "| Score band | Leads |",
        "|---|---|",
    ]
    for band, n in stats["score_buckets"].items():
        lines.append(f"| {band} | {n} |")
    lines += ["", "## By source", "", "| Source | Leads |", "|---|---|"]
    for src, n in (stats.get("by_source") or {}).items():
        lines.append(f"| {src} | {n} |")
    if stats.get("by_signal_combo"):
        lines += ["", "## By signal combination", "", "| Signals | Leads |", "|---|---|"]
        for combo, n in stats["by_signal_combo"].items():
            lines.append(f"| {combo} | {n} |")
    lines += _diagnostics_md(run_stats)
    lines += [
        "",
        "Qualified, contactable leads were **auto-pushed to Follow Up Boss** at the "
        "end of this run. The `pending-leads` artifact is the permanent record of "
        "what was pushed and held. If any leads show `awaiting_approval` (push "
        "failures), they will retry next run — or trigger the **push-approved** "
        "workflow to retry immediately.",
    ]
    return "\n".join(lines)


def send_digest_email(conn, run_stats: dict | None = None) -> bool:
    """Weekly digest to Peter: new leads, score breakdown, awaiting approval."""
    stats = _summary_stats(conn)
    st = (run_stats or {}).get("skiptrace") or {}
    sc = (run_stats or {}).get("scoring") or {}
    subject = (
        f"LDR Seller Finder — {stats['new_leads_this_week']} new leads, "
        f"{stats['pushed_this_run']} pushed to FUB"
    )
    html_rows = "".join(
        f"<tr><td>{band}</td><td>{n}</td></tr>" for band, n in stats["score_buckets"].items()
    )
    src_rows = "".join(
        f"<tr><td>{s}</td><td>{n}</td></tr>" for s, n in (stats.get("by_source") or {}).items()
    )
    html = f"""
    <h2>LDR Seller Finder — Weekly Digest</h2>
    <p><b>{stats['new_leads_this_week']}</b> new leads found this week.<br>
    <b>{stats['pushed_this_run']}</b> leads auto-pushed to Follow Up Boss this run.<br>
    <b>{stats['held_no_contact']}</b> held (skip trace found no email/phone).<br>
    <b>{stats['awaiting_approval']}</b> awaiting retry (push failed / no FUB key).<br>
    <b>{stats['pushed_total']}</b> leads pushed to Follow Up Boss all-time.<br>
    <b>{sc.get('warm_total', 0):,}</b> warm-tier leads stored (never traced/pushed — zero spend).<br>
    <b>Skip-trace spend this month: ~${st.get('mtd_cost_usd', 0):.2f}</b>
    ({st.get('mtd_traces', 0)} traces × ${config.SKIP_TRACE_COST_USD:.2f}).</p>
    <h3>Score breakdown (this run)</h3>
    <table border="1" cellpadding="4"><tr><th>Score band</th><th>Leads</th></tr>{html_rows}</table>
    <h3>By source</h3>
    <table border="1" cellpadding="4"><tr><th>Source</th><th>Leads</th></tr>{src_rows}</table>
    <p>The <b>pending-leads</b> artifact on the weekly run is the permanent record of
    what was pushed and held. Leads awaiting retry go out on the next run, or trigger
    the <b>push-approved</b> workflow to retry immediately.</p>
    <p style="color:#888">Sent by LDR-Seller-Finder. This repo never contacts leads directly.</p>
    """

    if config.DRY_RUN:
        LOGGER.info("[DRY-RUN] Would send digest: %s", subject)
        return True
    if not (config.SMTP_USER and config.SMTP_PASSWORD):
        LOGGER.warning(
            "SMTP secrets not configured — digest email SKIPPED (optional). "
            "Review stats are in the Actions job summary and the "
            "'pending-leads' artifact. Add SMTP_USER/SMTP_PASSWORD to enable."
        )
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config.EMAIL_FROM or config.SMTP_USER
    msg["To"] = config.OWNER_EMAIL
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=60) as s:
            s.starttls()
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.sendmail(msg["From"], [config.OWNER_EMAIL], msg.as_string())
        LOGGER.info("Digest email sent to %s", config.OWNER_EMAIL)
        return True
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("Digest email failed: %s", exc)
        return False

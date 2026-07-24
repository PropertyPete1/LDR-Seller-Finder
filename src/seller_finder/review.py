"""Approval gate artifacts: review CSV + run summary + weekly digest email.

The weekly run stages traced leads to 'awaiting_approval' and produces:
  * review/pending_leads.csv        — uploaded as an Actions artifact
  * review/run_summary.md           — written to the Actions job summary
Pushing to FUB only happens when the push-approved workflow is manually
triggered (workflow_dispatch).
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


def write_review_files(conn) -> dict:
    """Write pending_leads.csv and run_summary.md. Returns stats."""
    config.REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    rows = conn.execute(
        """SELECT l.id, l.county, l.prop_id, l.owner_name, l.property_addr, l.mail_addr,
                  l.score, l.signals, l.primary_source, l.status,
                  st.matched AS trace_matched, st.emails, st.phones, st.dnc, st.litigator
           FROM leads l LEFT JOIN skip_traces st ON st.id = l.skip_trace_id
           WHERE l.status='awaiting_approval' ORDER BY l.score DESC"""
    ).fetchall()

    csv_path = config.REVIEW_DIR / "pending_leads.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["lead_id", "county", "prop_id", "owner_name", "property_address",
                    "mailing_address", "score", "primary_source", "signals",
                    "trace_matched", "emails", "phones", "dnc", "litigator"])
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
                        r["primary_source"], signals, r["trace_matched"],
                        emails, phones, r["dnc"], r["litigator"]])

    stats = _summary_stats(conn)
    md_path = config.REVIEW_DIR / "run_summary.md"
    with open(md_path, "w") as f:
        f.write(render_summary_md(stats, len(rows)))
    LOGGER.info("Review files written: %s (%d pending leads)", csv_path, len(rows))
    return {"pending": len(rows), "csv": str(csv_path), "summary": str(md_path), **stats}


def _summary_stats(conn) -> dict:
    def one(sql, *args):
        row = conn.execute(sql, args).fetchone()
        return row[0] if row and row[0] is not None else 0

    week_ago = (dt.datetime.now(config.CT) - dt.timedelta(days=7)).isoformat()
    buckets = {}
    for lo, hi in ((40, 54), (55, 69), (70, 84), (85, 100)):
        buckets[f"{lo}-{hi}"] = one(
            "SELECT COUNT(*) FROM leads WHERE score BETWEEN ? AND ? AND status='awaiting_approval'",
            lo, hi,
        )
    return {
        "new_leads_this_week": one("SELECT COUNT(*) FROM leads WHERE created_at>=?", week_ago),
        "awaiting_approval": one("SELECT COUNT(*) FROM leads WHERE status='awaiting_approval'"),
        "pushed_total": one("SELECT COUNT(*) FROM leads WHERE status='pushed'"),
        "traced_total": one("SELECT COUNT(*) FROM skip_traces"),
        "score_buckets": buckets,
        "by_source": {
            r["primary_source"]: r["n"] for r in conn.execute(
                "SELECT primary_source, COUNT(*) n FROM leads "
                "WHERE status='awaiting_approval' GROUP BY primary_source"
            )
        },
    }


def render_summary_md(stats: dict, pending: int) -> str:
    lines = [
        "# LDR-Seller-Finder — Weekly Run Summary",
        "",
        f"**Run date:** {dt.datetime.now(config.CT):%Y-%m-%d %H:%M %Z}",
        "",
        f"| Metric | Value |",
        f"|---|---|",
        f"| New leads found this week | {stats['new_leads_this_week']} |",
        f"| Leads awaiting approval | {stats['awaiting_approval']} |",
        f"| Leads pushed to FUB (all time) | {stats['pushed_total']} |",
        f"| Owners skip-traced (all time, cached) | {stats['traced_total']} |",
        "",
        "## Score breakdown (awaiting approval)",
        "",
        "| Score band | Leads |",
        "|---|---|",
    ]
    for band, n in stats["score_buckets"].items():
        lines.append(f"| {band} | {n} |")
    lines += ["", "## By source", "", "| Source | Leads |", "|---|---|"]
    for src, n in (stats.get("by_source") or {}).items():
        lines.append(f"| {src} | {n} |")
    lines += [
        "",
        "**Next step:** download the `pending-leads` artifact, review the CSV, then "
        "manually run the **push-approved** workflow to send these leads to Follow Up Boss.",
    ]
    return "\n".join(lines)


def send_digest_email(conn) -> bool:
    """Weekly digest to Peter: new leads, score breakdown, awaiting approval."""
    stats = _summary_stats(conn)
    subject = (
        f"LDR Seller Finder — {stats['new_leads_this_week']} new leads, "
        f"{stats['awaiting_approval']} awaiting approval"
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
    <b>{stats['awaiting_approval']}</b> qualified leads awaiting your approval.<br>
    <b>{stats['pushed_total']}</b> leads pushed to Follow Up Boss all-time.</p>
    <h3>Score breakdown (awaiting approval)</h3>
    <table border="1" cellpadding="4"><tr><th>Score band</th><th>Leads</th></tr>{html_rows}</table>
    <h3>By source</h3>
    <table border="1" cellpadding="4"><tr><th>Source</th><th>Leads</th></tr>{src_rows}</table>
    <p>To push these to FUB: review the <b>pending-leads</b> artifact on the latest
    weekly run, then trigger the <b>push-approved</b> workflow in GitHub Actions.</p>
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

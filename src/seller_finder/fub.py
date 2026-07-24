"""Follow Up Boss push — auto-push, dedupe-safe, notes-rich.

Rules (per project spec, updated):
  * The weekly Monday run AUTO-PUSHES qualified, skip-traced leads to FUB at
    the end of the run — no manual approval step required. The push-approved
    workflow remains available as a manual fallback.
  * Leads with NO email AND NO phone from skip tracing are HELD
    (status='held_no_contact') — uncontactable records are never pushed.
  * Leads whose skip trace carries a DNC or litigator flag get an extra
    "DNC" tag so the nurture system can suppress texting/calling.
  * Every lead gets tags: "Seller Lead" + source tag
    ("County-Absentee" | "Divorce-Filing" | "Pre-Foreclosure").
  * Property address, score, and signals go into the lead note.
  * Duplicate check by email, then phone, then address before creating.
  * This repo NEVER sends emails/texts — outreach stays in LDR-Automation-Clean.
"""
import json
import logging
import time

import requests

from . import config
from .state import now_iso

LOGGER = logging.getLogger("fub")

FUB_BASE = "https://api.followupboss.com/v1"
FUB_SYSTEM_HEADERS = {
    "X-System": "LDR-Seller-Finder",
}


def _auth():
    return (config.FUB_API_KEY, "")


def _lead_contact(conn, lead: dict) -> dict:
    """Pull best email/phone + DNC/litigator flags from the lead's skip trace."""
    contact = {"emails": [], "phones": [], "dnc": False, "litigator": False}
    if lead.get("skip_trace_id"):
        row = conn.execute(
            "SELECT emails, phones, matched, dnc, litigator FROM skip_traces WHERE id=?",
            (lead["skip_trace_id"],),
        ).fetchone()
        if row and row["matched"]:
            contact["emails"] = json.loads(row["emails"] or "[]")
            contact["phones"] = [p.get("number") for p in json.loads(row["phones"] or "[]")
                                 if p.get("number")]
            contact["dnc"] = bool(row["dnc"])
            contact["litigator"] = bool(row["litigator"])
    return contact


def is_contactable(contact: dict) -> bool:
    """A lead is pushable only if skip tracing produced an email OR a phone."""
    return bool(contact["emails"] or contact["phones"])


def find_existing_person(emails: list[str], phones: list[str], address: str) -> str | None:
    """Return FUB person id if a contact already exists (email → phone → address)."""
    session = requests.Session()
    session.auth = _auth()
    session.headers.update(FUB_SYSTEM_HEADERS)

    for email in emails:
        pid = _search_people(session, {"email": email})
        if pid:
            return pid
    for phone in phones:
        pid = _search_people(session, {"phone": phone})
        if pid:
            return pid
    if address:
        street = address.split(",")[0].strip()
        if street:
            pid = _search_people(session, {"q": street})
            if pid:
                return pid
    return None


def _search_people(session: requests.Session, params: dict) -> str | None:
    try:
        resp = session.get(f"{FUB_BASE}/people", params={**params, "limit": 1}, timeout=60)
        if resp.status_code == 429:
            time.sleep(10)
            resp = session.get(f"{FUB_BASE}/people", params={**params, "limit": 1}, timeout=60)
        resp.raise_for_status()
        people = resp.json().get("people", [])
        return str(people[0]["id"]) if people else None
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("FUB people search failed (%s): %s", params, exc)
        return None


def build_note(lead: dict) -> str:
    signals = json.loads(lead.get("signals") or "[]")
    lines = [
        f"Seller lead from LDR-Seller-Finder — score {lead['score']}/100",
        f"Property: {lead['property_addr']} ({lead['county'].title()} County, parcel {lead['prop_id']})",
        f"Owner mailing address: {lead['mail_addr']}",
        "",
        "Signals:",
    ]
    for s in signals:
        lines.append(f"  • [{s['points']:+d}] {s['signal'].replace('_', ' ')}: {s['detail']}")
    if lead.get("notes"):
        lines += ["", lead["notes"]]
    return "\n".join(lines)


def generate_summary_note(lead: dict) -> str:
    """Claude-written one-paragraph summary for the FUB note (best effort)."""
    if not config.ANTHROPIC_API_KEY:
        return ""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        signals = json.loads(lead.get("signals") or "[]")
        resp = client.messages.create(
            model=config.LLM_MODEL,
            max_tokens=250,
            system=(
                "You write brief, factual internal CRM notes for a real estate team "
                "about potential seller leads. 2-3 sentences, no fluff, no outreach "
                "language, just why this owner may be motivated to sell."
            ),
            messages=[{
                "role": "user",
                "content": (
                    f"Owner: {lead['owner_name']}\nProperty: {lead['property_addr']}\n"
                    f"Score: {lead['score']}/100\nSignals: {json.dumps(signals)}"
                ),
            }],
        )
        return resp.content[0].text.strip()
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("LLM summary failed for lead %s: %s", lead.get("id"), exc)
        return ""


def push_lead(conn, lead: dict) -> str | None:
    """Create (or tag) one FUB person. Returns FUB person id or None."""
    contact = _lead_contact(conn, lead)
    source_tag = config.TAG_BY_SOURCE.get(lead["primary_source"], "County-Absentee")
    tags = [config.TAG_SELLER, source_tag]
    if contact["dnc"] or contact["litigator"]:
        # Nurture system suppresses texting/calling anyone tagged DNC.
        tags.append("DNC")

    existing_id = find_existing_person(contact["emails"], contact["phones"], lead["property_addr"])

    lead["notes"] = generate_summary_note(lead)
    note_body = build_note(lead)

    session = requests.Session()
    session.auth = _auth()
    session.headers.update(FUB_SYSTEM_HEADERS)

    if config.DRY_RUN:
        LOGGER.info("[DRY-RUN] Would push lead %s (%s) existing=%s tags=%s",
                    lead["id"], lead["owner_name"], existing_id, tags)
        return None

    try:
        if existing_id:
            # Never create a duplicate: add tags + note to the existing person.
            resp = session.get(f"{FUB_BASE}/people/{existing_id}", timeout=60)
            resp.raise_for_status()
            current_tags = resp.json().get("tags") or []
            merged = sorted(set(current_tags) | set(tags))
            session.put(f"{FUB_BASE}/people/{existing_id}", json={"tags": merged}, timeout=60)
            person_id = existing_id
            LOGGER.info("Lead %s matched existing FUB person %s — tagged, no duplicate created",
                        lead["id"], person_id)
        else:
            first, last = _person_name(lead["owner_name"])
            payload = {
                "firstName": first,
                "lastName": last,
                "source": "LDR-Seller-Finder",
                "tags": tags,
                "emails": [{"value": e} for e in contact["emails"][:3]],
                "phones": [{"value": p} for p in contact["phones"][:3]],
                "addresses": [{"street": lead["property_addr"].split(",")[0],
                               "city": lead["property_addr"].split(",")[-1].rsplit("TX", 1)[0].strip(" ,"),
                               "state": "TX", "type": "property"}],
            }
            resp = session.post(f"{FUB_BASE}/people?deduplicate=true", json=payload, timeout=60)
            resp.raise_for_status()
            person_id = str(resp.json()["id"])
            LOGGER.info("Created FUB person %s for lead %s", person_id, lead["id"])

        session.post(f"{FUB_BASE}/notes", json={
            "personId": int(person_id),
            "subject": f"Seller lead — score {lead['score']}/100",
            "body": note_body,
        }, timeout=60)
        return person_id
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("FUB push failed for lead %s: %s", lead["id"], exc)
        return None


def _push_batch(conn, leads, stats: dict) -> dict:
    """Shared push loop with contactability hold + status bookkeeping."""
    for row in leads:
        lead = dict(row)
        contact = _lead_contact(conn, lead)
        if not is_contactable(contact):
            conn.execute(
                "UPDATE leads SET status='held_no_contact', updated_at=? WHERE id=?",
                (now_iso(), lead["id"]),
            )
            stats["held_no_contact"] += 1
            conn.commit()
            LOGGER.info("Lead %s (%s) HELD — skip trace returned no email and no phone",
                        lead["id"], lead["owner_name"])
            continue
        person_id = push_lead(conn, lead)
        if person_id:
            conn.execute(
                "UPDATE leads SET status='pushed', fub_person_id=?, updated_at=? WHERE id=?",
                (person_id, now_iso(), lead["id"]),
            )
            stats["pushed"] += 1
        elif config.DRY_RUN:
            stats["dry_run_would_push"] = stats.get("dry_run_would_push", 0) + 1
        else:
            stats["failed"] += 1  # stays awaiting_approval → retried next run
        conn.commit()
        time.sleep(0.5)  # stay well under FUB rate limits
    LOGGER.info("FUB push complete: %s", stats)
    return stats


def auto_push_leads(conn) -> dict:
    """Auto-push all awaiting leads at the end of the weekly run."""
    stats = {"pushed": 0, "held_no_contact": 0, "failed": 0, "total": 0}
    if not config.FUB_API_KEY:
        LOGGER.warning("FUB_API_KEY not set — auto-push SKIPPED; leads stay in "
                       "awaiting_approval for the push-approved fallback workflow.")
        return stats
    leads = conn.execute(
        "SELECT * FROM leads WHERE status='awaiting_approval' ORDER BY score DESC"
    ).fetchall()
    stats["total"] = len(leads)
    return _push_batch(conn, leads, stats)


def push_approved_leads(conn) -> dict:
    """Manual fallback: push 'awaiting_approval' leads (push-approved workflow).
    Also retries 'held_no_contact' leads in case contact info has since been
    cached by a re-trace."""
    stats = {"pushed": 0, "held_no_contact": 0, "failed": 0, "total": 0}
    leads = conn.execute(
        "SELECT * FROM leads WHERE status IN ('awaiting_approval','held_no_contact') "
        "ORDER BY score DESC"
    ).fetchall()
    stats["total"] = len(leads)
    return _push_batch(conn, leads, stats)


def _person_name(owner_name: str) -> tuple[str, str]:
    import re
    name = re.split(r"[&,]", owner_name or "")[0].strip()
    parts = name.split()
    if len(parts) >= 2:
        return parts[1].title(), parts[0].title()  # owner format LAST FIRST
    return "", name.title()

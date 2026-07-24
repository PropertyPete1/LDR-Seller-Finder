"""Lead scoring — 0-100, stored per lead.

Weights (config/settings.yaml → scoring):
  +30 absentee owner (mailing address ≠ property address)
  +25 divorce filing match
  +30 pre-foreclosure/NOD match
  +20 owned 10+ years (needs deed data — see README; owners_first_seen proxy)
  +10 homestead exemption recently removed

Only leads scoring >= skip_trace_threshold (40) move to skip-tracing.
"""
import datetime as dt
import json
import logging

from . import config
from .state import now_iso

LOGGER = logging.getLogger("scoring")


def compute_scores(conn, foreclosure_matches: list[dict], divorce_matches: list[dict],
                   homestead_removed: dict[str, list[str]]) -> dict:
    """Build/refresh the leads table from all signals. Returns stats."""
    stats = {"leads_created": 0, "leads_updated": 0, "qualified": 0,
             "candidates": 0, "below_threshold": 0}

    fc_by_key = {(m["county"] if "county" in m else "bexar", m["prop_id"]): m
                 for m in foreclosure_matches if m.get("prop_id")}
    dv_by_key = {(m["county"], m["prop_id"]): m for m in divorce_matches}
    hs_removed = {(county, pid) for county, pids in homestead_removed.items() for pid in pids}

    # Candidate universe: absentee parcels + any parcel hit by a signal.
    # Parcels live in the ephemeral cache (pc.parcels), rebuilt each run.
    keys = set(fc_by_key) | set(dv_by_key) | hs_removed
    candidates = {}
    for row in conn.execute(
        "SELECT county, prop_id, owner_name, situs_addr, situs_city, situs_zip, "
        "mail_addr, mail_city, mail_state, mail_zip, is_absentee "
        "FROM pc.parcels WHERE is_absentee=1"
    ):
        candidates[(row["county"], row["prop_id"])] = dict(row)
    for key in keys:
        if key not in candidates:
            row = conn.execute(
                "SELECT county, prop_id, owner_name, situs_addr, situs_city, situs_zip, "
                "mail_addr, mail_city, mail_state, mail_zip, is_absentee "
                "FROM pc.parcels WHERE county=? AND prop_id=?", key,
            ).fetchone()
            if row:
                candidates[key] = dict(row)

    ts = now_iso()
    event_retention_days = int(config.SETTINGS.get("event_signal_retention_days", 120))
    for key, p in candidates.items():
        county, prop_id = key
        signals = []
        score = 0

        if p["is_absentee"]:
            score += config.SCORE_ABSENTEE
            signals.append({"signal": "absentee_owner", "points": config.SCORE_ABSENTEE,
                            "detail": f"Owner mails to {p['mail_addr']}, {p['mail_city']} {p['mail_state']}"})
        if key in dv_by_key:
            m = dv_by_key[key]
            score += config.SCORE_DIVORCE
            signals.append({"signal": "divorce_filing", "points": config.SCORE_DIVORCE,
                            "detail": f"Case {m['case_number']} (confidence {m['confidence']:.2f})"})
        if key in fc_by_key:
            m = fc_by_key[key]
            score += config.SCORE_PREFORECLOSURE
            signals.append({"signal": "preforeclosure", "points": config.SCORE_PREFORECLOSURE,
                            "detail": f"{m['kind'].title()} foreclosure notice doc #{m['doc_number']} "
                                      f"({m['month']}/{m['year']} sale)"})
        if _owned_ten_plus_years(conn, county, prop_id):
            score += config.SCORE_LONG_OWNERSHIP
            signals.append({"signal": "owned_10_plus_years", "points": config.SCORE_LONG_OWNERSHIP,
                            "detail": "No ownership change in 10+ years (deed-date/owner-history)"})
        if key in hs_removed:
            score += config.SCORE_HOMESTEAD_REMOVED
            signals.append({"signal": "homestead_removed", "points": config.SCORE_HOMESTEAD_REMOVED,
                            "detail": "Homestead exemption present last pull, now removed"})

        if score <= 0:
            continue
        stats["candidates"] += 1

        primary = _primary_source(signals)
        prop_addr = f"{p['situs_addr']}, {p['situs_city']} TX {p['situs_zip']}".strip(", ")
        mail_addr = f"{p['mail_addr']}, {p['mail_city']} {p['mail_state']} {p['mail_zip']}".strip(", ")
        qualified = score >= config.SCORE_THRESHOLD

        existing = conn.execute(
            "SELECT id, status, score, signals, updated_at FROM leads WHERE county=? AND prop_id=?", key
        ).fetchone()
        if existing:
            # Event signals (preforeclosure/divorce) come from weekly feeds that
            # only cover the current window. If this run didn't re-observe the
            # event, carry the stored signal forward for a retention period so
            # a qualified lead isn't silently demoted between runs.
            current_kinds = {s["signal"] for s in signals}
            score, signals = _carry_forward_events(
                existing, signals, score, current_kinds, event_retention_days)
            qualified = score >= config.SCORE_THRESHOLD
            primary = _primary_source(signals)
            # Never demote pushed/approved leads; refresh score + signals otherwise.
            new_status = existing["status"]
            if existing["status"] in ("new", "qualified", "skipped"):
                new_status = "qualified" if qualified else "new"
            conn.execute(
                """UPDATE leads SET owner_name=?, property_addr=?, mail_addr=?, score=?,
                   signals=?, primary_source=?, status=?, updated_at=? WHERE id=?""",
                (p["owner_name"], prop_addr, mail_addr, score, json.dumps(signals),
                 primary, new_status, ts, existing["id"]),
            )
            stats["leads_updated"] += 1
        elif qualified:
            # Only qualified leads are persisted — sub-threshold candidates
            # (e.g. absentee-only at 30) are recomputed from the parcel cache
            # every run, so storing them would only bloat the committed DB
            # (186K+ rows today, millions at 5+ counties vs GitHub's 100MB cap).
            conn.execute(
                """INSERT INTO leads (county, prop_id, owner_name, property_addr, mail_addr,
                   score, signals, primary_source, status, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (county, prop_id, p["owner_name"], prop_addr, mail_addr, score,
                 json.dumps(signals), primary, "qualified", ts, ts),
            )
            stats["leads_created"] += 1
        else:
            stats["below_threshold"] += 1
        stats["qualified"] += int(qualified)

    conn.commit()
    LOGGER.info("Scoring: %s", stats)
    return stats


def _carry_forward_events(existing, signals, score, current_kinds, retention_days):
    """Merge stored event signals into this run's signals (see compute_scores)."""
    import datetime as _dt
    try:
        stored = json.loads(existing["signals"] or "[]")
    except (ValueError, TypeError):
        return score, signals
    try:
        age_days = (_dt.date.today()
                    - _dt.date.fromisoformat((existing["updated_at"] or "")[:10])).days
    except ValueError:
        age_days = 0
    if age_days > retention_days:
        return score, signals
    for s in stored:
        if s.get("signal") in ("preforeclosure", "divorce_filing") \
                and s["signal"] not in current_kinds:
            signals.append(s)
            score += int(s.get("points", 0))
    return score, signals


def _primary_source(signals: list[dict]) -> str:
    """Pick the source tag: strongest non-absentee signal wins, else absentee."""
    kinds = {s["signal"] for s in signals}
    if "preforeclosure" in kinds:
        return "preforeclosure"
    if "divorce_filing" in kinds:
        return "divorce"
    return "absentee"


def _owned_ten_plus_years(conn, county: str, prop_id: str) -> bool:
    """True if we can show 10+ years of continuous ownership.

    Free bulk sources carry no deed date, so this signal activates from:
      (a) an imported deed-date table (data/deed_dates.csv from the BCAD
          appraisal export — see scripts/import_deed_dates.py), or
      (b) the compact owners_first_seen baseline once the system has been
          running long enough (owner unchanged since first observation).
    """
    row = conn.execute(
        "SELECT deed_date FROM deed_dates WHERE county=? AND prop_id=?", (county, prop_id)
    ).fetchone()
    if row and row["deed_date"]:
        try:
            deed = dt.date.fromisoformat(row["deed_date"][:10])
            return (dt.date.today() - deed).days >= 3650
        except ValueError:
            pass

    fs = conn.execute(
        "SELECT first_seen_at FROM owners_first_seen WHERE county=? AND prop_id=?",
        (county, prop_id),
    ).fetchone()
    baseline = fs["first_seen_at"] if fs else None
    if not baseline:
        return False
    try:
        base = dt.date.fromisoformat(baseline[:10])
        return (dt.date.today() - base).days >= 3650
    except ValueError:
        return False


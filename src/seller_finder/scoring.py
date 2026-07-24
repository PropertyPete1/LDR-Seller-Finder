"""Lead scoring — 0-100, stored per lead.

Weights (config/settings.yaml → scoring):
  +30 absentee owner (mailing address ≠ property address)
  +25 divorce filing match
  +30 pre-foreclosure/NOD match
  +20 owned 10+ years (needs deed data — see README; owner_history proxy)
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
    stats = {"leads_created": 0, "leads_updated": 0, "qualified": 0}

    fc_by_key = {(m["county"] if "county" in m else "bexar", m["prop_id"]): m
                 for m in foreclosure_matches if m.get("prop_id")}
    dv_by_key = {(m["county"], m["prop_id"]): m for m in divorce_matches}
    hs_removed = {(county, pid) for county, pids in homestead_removed.items() for pid in pids}

    # Candidate universe: absentee parcels + any parcel hit by a signal.
    keys = set(fc_by_key) | set(dv_by_key) | hs_removed
    candidates = {}
    for row in conn.execute(
        "SELECT county, prop_id, owner_name, situs_addr, situs_city, situs_zip, "
        "mail_addr, mail_city, mail_state, mail_zip, is_absentee, first_seen_at "
        "FROM parcels WHERE is_absentee=1"
    ):
        candidates[(row["county"], row["prop_id"])] = dict(row)
    for key in keys:
        if key not in candidates:
            row = conn.execute(
                "SELECT county, prop_id, owner_name, situs_addr, situs_city, situs_zip, "
                "mail_addr, mail_city, mail_state, mail_zip, is_absentee, first_seen_at "
                "FROM parcels WHERE county=? AND prop_id=?", key,
            ).fetchone()
            if row:
                candidates[key] = dict(row)

    ts = now_iso()
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
        if _owned_ten_plus_years(conn, county, prop_id, p["first_seen_at"]):
            score += config.SCORE_LONG_OWNERSHIP
            signals.append({"signal": "owned_10_plus_years", "points": config.SCORE_LONG_OWNERSHIP,
                            "detail": "No ownership change in 10+ years (deed-date/owner-history)"})
        if key in hs_removed:
            score += config.SCORE_HOMESTEAD_REMOVED
            signals.append({"signal": "homestead_removed", "points": config.SCORE_HOMESTEAD_REMOVED,
                            "detail": "Homestead exemption present last pull, now removed"})

        if score <= 0:
            continue

        primary = _primary_source(signals)
        prop_addr = f"{p['situs_addr']}, {p['situs_city']} TX {p['situs_zip']}".strip(", ")
        mail_addr = f"{p['mail_addr']}, {p['mail_city']} {p['mail_state']} {p['mail_zip']}".strip(", ")
        qualified = score >= config.SCORE_THRESHOLD

        existing = conn.execute(
            "SELECT id, status, score FROM leads WHERE county=? AND prop_id=?", key
        ).fetchone()
        if existing:
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
        else:
            conn.execute(
                """INSERT INTO leads (county, prop_id, owner_name, property_addr, mail_addr,
                   score, signals, primary_source, status, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (county, prop_id, p["owner_name"], prop_addr, mail_addr, score,
                 json.dumps(signals), primary, "qualified" if qualified else "new", ts, ts),
            )
            stats["leads_created"] += 1
        stats["qualified"] += int(qualified)

    conn.commit()
    LOGGER.info("Scoring: %s", stats)
    return stats


def _primary_source(signals: list[dict]) -> str:
    """Pick the source tag: strongest non-absentee signal wins, else absentee."""
    kinds = {s["signal"] for s in signals}
    if "preforeclosure" in kinds:
        return "preforeclosure"
    if "divorce_filing" in kinds:
        return "divorce"
    return "absentee"


def _owned_ten_plus_years(conn, county: str, prop_id: str, first_seen_at: str) -> bool:
    """True if we can show 10+ years of continuous ownership.

    Free bulk sources carry no deed date, so this signal activates from:
      (a) an imported deed-date table (data/deed_dates.csv from the BCAD
          appraisal export — see scripts/import_deed_dates.py), or
      (b) our own owner_history once the system has been running long enough.
    """
    row = conn.execute(
        "SELECT deed_date FROM deed_dates WHERE county=? AND prop_id=?", (county, prop_id)
    ).fetchone() if _deed_table_exists(conn) else None
    if row and row["deed_date"]:
        try:
            deed = dt.date.fromisoformat(row["deed_date"][:10])
            return (dt.date.today() - deed).days >= 3650
        except ValueError:
            pass

    last_change = conn.execute(
        "SELECT MAX(observed_at) AS ts FROM owner_history WHERE county=? AND prop_id=?",
        (county, prop_id),
    ).fetchone()
    baseline = last_change["ts"] or first_seen_at
    if not baseline:
        return False
    try:
        base = dt.date.fromisoformat(baseline[:10])
        return (dt.date.today() - base).days >= 3650
    except ValueError:
        return False


def _deed_table_exists(conn) -> bool:
    return bool(conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='deed_dates'"
    ).fetchone())

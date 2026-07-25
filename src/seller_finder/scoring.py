"""Lead scoring — 0-100, stored per lead.

Weights (config/settings.yaml → scoring):
  +30 absentee owner (mailing address ≠ property address)
  +25 divorce filing match
  +30 pre-foreclosure/NOD match
  +20 owned 10+ years (needs deed data — see README; owners_first_seen proxy)
  +10 homestead exemption recently removed

Only leads scoring >= skip_trace_threshold (40) move to skip-tracing.

Warm tier (warm_tier_min..threshold-1, e.g. absentee-only at 30): scored
and stored in the COMPACT warm_leads table (no names/addresses — joined
from pc.parcels at runtime), NEVER traced or pushed — zero spend. When a
new signal (deed date, divorce, foreclosure) lifts a warm lead over the
threshold on a later run, it auto-promotes into the leads table and flows
to tracing/push like any other qualified lead.
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
             "candidates": 0, "below_threshold": 0,
             "warm": 0, "warm_promoted": 0}
    warm_batch: list[tuple] = []
    warm_existing = {
        (r["county"], r["prop_id"]) for r in conn.execute(
            "SELECT county, prop_id FROM warm_leads")
    }

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
        # Event signals record observed_at (the date the notice/filing was
        # actually seen) so _carry_forward_events can expire them correctly.
        if key in dv_by_key:
            m = dv_by_key[key]
            score += config.SCORE_DIVORCE
            signals.append({"signal": "divorce_filing", "points": config.SCORE_DIVORCE,
                            "observed_at": ts[:10],
                            "detail": f"Case {m['case_number']} (confidence {m['confidence']:.2f})"})
        if key in fc_by_key:
            m = fc_by_key[key]
            score += config.SCORE_PREFORECLOSURE
            signals.append({"signal": "preforeclosure", "points": config.SCORE_PREFORECLOSURE,
                            "observed_at": ts[:10],
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
            conn.execute(
                """INSERT INTO leads (county, prop_id, owner_name, property_addr, mail_addr,
                   score, signals, primary_source, status, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (county, prop_id, p["owner_name"], prop_addr, mail_addr, score,
                 json.dumps(signals), primary, "qualified", ts, ts),
            )
            stats["leads_created"] += 1
            if key in warm_existing:
                stats["warm_promoted"] += 1  # warm lead crossed the threshold
        elif score >= config.WARM_TIER_MIN:
            # Warm tier: compact row only (signal names, no PII bloat) — the
            # full record is joined from pc.parcels at runtime. NEVER traced
            # or pushed. ~40 bytes/row keeps 150K+ warm rows a few MB.
            warm_batch.append(
                (county, prop_id, score,
                 json.dumps([s["signal"] for s in signals]), ts))
            stats["warm"] += 1
            stats["below_threshold"] += 1
        else:
            stats["below_threshold"] += 1
        stats["qualified"] += int(qualified)
        if len(warm_batch) >= 5000:
            _upsert_warm(conn, warm_batch)
            warm_batch = []

    if warm_batch:
        _upsert_warm(conn, warm_batch)
    # Promoted warm leads now live in `leads` — drop their warm rows.
    conn.execute(
        "DELETE FROM warm_leads WHERE EXISTS "
        "(SELECT 1 FROM leads l WHERE l.county=warm_leads.county "
        " AND l.prop_id=warm_leads.prop_id)")
    stats["warm_total"] = conn.execute("SELECT COUNT(*) c FROM warm_leads").fetchone()["c"]
    conn.commit()
    LOGGER.info("Scoring: %s", stats)
    return stats


def _upsert_warm(conn, batch: list[tuple]) -> None:
    conn.executemany(
        "INSERT INTO warm_leads (county, prop_id, score, signals, updated_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT (county, prop_id) DO UPDATE SET "
        "score=excluded.score, signals=excluded.signals, updated_at=excluded.updated_at",
        batch)


def _carry_forward_events(existing, signals, score, current_kinds, retention_days):
    """Merge stored event signals into this run's signals (see compute_scores).

    Each event signal carries its own `observed_at` — the run that actually saw
    the notice/filing — and expires `retention_days` after THAT date.

    It used to expire off the lead's `updated_at`, which compute_scores rewrites
    on every single run. That made age_days ~0 forever, so retention_days never
    fired and a one-off foreclosure notice kept its +30 for the life of the row.
    Signals stored before this change have no observed_at; they are anchored to
    the lead's updated_at once, on first sight, and expire normally after that.
    """
    import datetime as _dt
    try:
        stored = json.loads(existing["signals"] or "[]")
    except (ValueError, TypeError):
        return score, signals
    today = _dt.date.today()
    fallback_anchor = (existing["updated_at"] or "")[:10]
    for s in stored:
        if s.get("signal") not in ("preforeclosure", "divorce_filing"):
            continue
        if s["signal"] in current_kinds:
            continue  # re-observed this run — the fresh signal already counted
        observed = str(s.get("observed_at") or fallback_anchor)[:10]
        try:
            age_days = (today - _dt.date.fromisoformat(observed)).days
        except ValueError:
            age_days = 0
        if age_days > retention_days:
            LOGGER.info("Event signal %s expired (%d days > %d retention) — dropping",
                        s["signal"], age_days, retention_days)
            continue
        carried = dict(s)
        carried.setdefault("observed_at", observed)
        signals.append(carried)
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


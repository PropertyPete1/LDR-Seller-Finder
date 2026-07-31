"""Divorce filings — Bexar County District Clerk. **STUBBED — READ THIS.**

WHAT WE FOUND (July 2026 research):
  * Bexar County court records were consolidated into the Tyler Odyssey
    portal: https://portal-txbexar.tylertech.cloud/Portal/
    (the old search.bexar.org was retired).
  * The portal's Smart Search supports case-type + file-date filters, BUT it
    requires a record number or party NAME to run a search — you cannot list
    "all divorce cases filed last week" anonymously.
  * The portal sits behind a CAPTCHA and has no public API or bulk export.
    Elevated/registered access is reserved for law-enforcement and justice
    partners. Building a scraper here would be fragile and would likely get
    blocked, so per project policy we stub instead.

OPTIONS TO LIGHT THIS UP (decide later, then implement fetch()):
  1. Paid court-records API: UniCourt, ATTOM (pre-divorce leads), or
     PropertyRadar. UniCourt has true API access to TX district court dockets.
  2. Recurring open-records request to the Bexar County District Clerk
     (district clerk records requests: https://www.bexar.org/3500/Public-Records-Requests)
     asking for a weekly list of newly filed family/divorce cases (case
     number, filing date, party names). Many Texas clerks fulfill these as
     CSV/Excel on a standing basis for a small fee. Once received, drop the
     CSV into data/inbox/divorce_YYYY-MM-DD.csv and `fetch_from_csv` ingests it.
  3. Lead vendors (Foreclosures Daily, CourthouseDirect) sell divorce-filing
     lists for Bexar County — same CSV ingest path.

The matching half of this module (match_filings_to_owners) is FULLY BUILT:
it uses Claude (claude-sonnet-4-6) for fuzzy name matching between divorce
party names and parcel owner names, so the moment filings flow in, matching
and scoring work end to end.
"""
import csv
import json
import logging
from pathlib import Path

from .. import config
from ..state import now_iso

LOGGER = logging.getLogger("sources.divorce")

# Matching costs one Claude call per party name. A case whose parties own no
# property in our counties — the common case for a county-wide filing list —
# never matches, so without a cap it is re-sent on every weekly run forever.
# Three attempts covers the real reason to retry: a parcel refresh landing
# between runs, or a deed/owner update changing the candidate set.
MAX_MATCH_ATTEMPTS = 3


def _inbox_dir() -> Path:
    """Resolved at call time, not import time.

    As a module-level constant this ignored both the DATA_DIR env override and
    any later change to config.DATA_DIR, so this module read a different inbox
    than deeds.py and preforeclosure.py, which both resolve per call.
    """
    return Path(config.DATA_DIR) / "inbox"


def fetch(county: str) -> list[dict]:
    """STUB: no automated feed available (see module docstring).

    Falls through to CSV ingestion so a manual/vendor workflow can feed the
    pipeline today: place files matching divorce_*.csv in data/inbox/.
    """
    filings = fetch_from_csv()
    if not filings:
        LOGGER.warning(
            "Divorce source is STUBBED for %s — no API/bulk access to Bexar "
            "District Clerk records (Tyler Odyssey portal requires a party "
            "name + CAPTCHA). See sources/divorce.py and README for paid-API "
            "options. Drop vendor/open-records CSVs into data/inbox/ to "
            "activate this signal.",
            county,
        )
    return filings


def fetch_from_csv() -> list[dict]:
    """Ingest divorce filings from data/inbox/divorce_*.csv.

    Expected columns (case-insensitive): case_number, filed_date,
    petitioner, respondent. Extra columns are ignored.
    """
    filings = []
    inbox = _inbox_dir()
    if not inbox.exists():
        return filings
    for path in sorted(inbox.glob("divorce_*.csv")):
        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    row = {k.lower().strip(): (v or "").strip() for k, v in row.items()}
                    parties = [p for p in (row.get("petitioner"), row.get("respondent")) if p]
                    if not row.get("case_number") or not parties:
                        continue
                    filings.append({
                        "case_number": row["case_number"],
                        "filed_date": row.get("filed_date", ""),
                        "party_names": parties,
                        "county": "bexar",
                    })
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("Failed reading %s: %s", path, exc)
    if filings:
        LOGGER.info("Ingested %d divorce filings from CSV inbox", len(filings))
    return filings


def store_filings(conn, filings: list[dict]) -> int:
    """Insert new filings; returns count of newly stored cases."""
    new = 0
    for f in filings:
        cur = conn.execute(
            """INSERT OR IGNORE INTO divorce_cases
               (case_number, county, filed_date, party_names, created_at)
               VALUES (?,?,?,?,?)""",
            (f["case_number"], f.get("county", "bexar"), f.get("filed_date", ""),
             json.dumps(f["party_names"]), now_iso()),
        )
        new += cur.rowcount
    conn.commit()
    return new


# ── Fuzzy name matching (Claude) ─────────────────────────────────────────

MATCH_SYSTEM_PROMPT = """You match divorce-filing party names to county parcel owner names.
Parcel owner names are usually formatted LAST FIRST MIDDLE or LAST FIRST & SPOUSE.
Given one party name and a list of candidate owner records, return the best match.
Respond ONLY with JSON: {"match_index": <int or null>, "confidence": <0.0-1.0>}.
match_index is the 0-based index into the candidates list, or null if none are
plausibly the same person. Be conservative: common names need corroborating
middle initials or spouse names to score above 0.8."""


def _anthropic_client():
    """Build the Claude client. A named seam so tests can mock the paid
    boundary without reaching into the `anthropic` package internals."""
    import anthropic
    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def _candidate_owners(conn, party_name: str, limit: int = 25) -> list[dict]:
    """Cheap SQL prefilter: parcels whose owner shares the party's surname."""
    tokens = [t for t in party_name.upper().replace(",", " ").split() if len(t) >= 3]
    if not tokens:
        return []
    surname = tokens[-1] if "," not in party_name else tokens[0]
    # Party names are usually "First Last"; owner names "LAST FIRST".
    rows = conn.execute(
        """SELECT county, prop_id, owner_name, situs_addr, situs_city
           FROM pc.parcels WHERE owner_name LIKE ? LIMIT ?""",
        (f"{surname}%", limit),
    ).fetchall()
    return [dict(r) for r in rows]


def match_filings_to_owners(conn, min_confidence: float = 0.8) -> list[dict]:
    """Match unmatched divorce cases to parcel owners via Claude fuzzy matching.

    Returns list of {case_number, county, prop_id, owner_name, confidence}.
    """
    # match_attempts caps the retry budget — see MAX_MATCH_ATTEMPTS. Without
    # it this query returned every never-matched case on every weekly run and
    # re-billed Claude for each one indefinitely.
    unmatched = conn.execute(
        "SELECT id, case_number, party_names FROM divorce_cases "
        "WHERE matched_prop_id IS NULL AND COALESCE(match_attempts, 0) < ? "
        "ORDER BY id", (MAX_MATCH_ATTEMPTS,)
    ).fetchall()
    if not unmatched:
        return []

    if config.DRY_RUN:
        # A dry run is documented as zero spend; matching is a paid API call.
        LOGGER.info("[DRY-RUN] Would attempt Claude matching for %d divorce case(s)",
                    len(unmatched))
        return []

    if not config.ANTHROPIC_API_KEY:
        LOGGER.warning("ANTHROPIC_API_KEY not set — divorce matching SKIPPED for "
                       "%d case(s) (optional secret)", len(unmatched))
        return []

    try:
        client = _anthropic_client()
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("Anthropic client unavailable — skipping divorce matching: %s", exc)
        return []

    matches = []
    for case in unmatched:
        # Count the attempt up front: if this run dies mid-loop the case must
        # not get a free retry on top of its budget.
        conn.execute(
            "UPDATE divorce_cases SET match_attempts=COALESCE(match_attempts,0)+1, "
            "last_attempt_at=? WHERE id=?", (now_iso(), case["id"]))
        conn.commit()
        for party in json.loads(case["party_names"]):
            candidates = _candidate_owners(conn, party)
            if not candidates:
                continue
            cand_text = json.dumps([
                {"i": i, "owner": c["owner_name"], "address": c["situs_addr"], "city": c["situs_city"]}
                for i, c in enumerate(candidates)
            ])
            try:
                resp = client.messages.create(
                    model=config.LLM_MODEL,
                    max_tokens=200,
                    system=MATCH_SYSTEM_PROMPT,
                    messages=[{
                        "role": "user",
                        "content": f"Party name: {party}\nCandidates: {cand_text}",
                    }],
                )
                result = json.loads(resp.content[0].text.strip())
                # Parsing the model's answer belongs INSIDE the try: a
                # non-numeric confidence ("high") raised ValueError out of the
                # whole function and took the entire divorce stage down.
                mi = result.get("match_index")
                conf = float(result.get("confidence") or 0)
            except Exception as exc:  # noqa: BLE001
                LOGGER.error("LLM match failed for %s: %s", party, exc)
                continue
            if mi is not None and conf >= min_confidence and 0 <= mi < len(candidates):
                hit = candidates[mi]
                conn.execute(
                    """UPDATE divorce_cases
                       SET matched_prop_id=?, matched_county=?, match_confidence=?
                       WHERE id=?""",
                    (hit["prop_id"], hit["county"], conf, case["id"]),
                )
                matches.append({
                    "case_number": case["case_number"],
                    "county": hit["county"],
                    "prop_id": hit["prop_id"],
                    "owner_name": hit["owner_name"],
                    "confidence": conf,
                })
                break  # one match per case is enough
    conn.commit()
    LOGGER.info("Divorce matching: %d cases matched to parcels", len(matches))
    return matches

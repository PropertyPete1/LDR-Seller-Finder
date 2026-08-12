"""Per-table reconciliation rules for two lineages of the state DB.

Run with: python3 -m pytest tests/test_state_merge.py -v

tests/test_state_sync.py proves the protocol: a push that finds the branch moved
merges rather than overwrites. These tests are about what that merge is allowed
to decide, table by table, because "no row was lost" is not the same as "the
right row won". The rules that matter here are the ones that cost money or leak
data if they go the wrong way:

  * a paid skip trace is never dropped, and never mixed field-wise with another
    lineage's result (a matched=1 row with no emails is worse than either side);
  * a lead that reached FUB never looks unpushed again;
  * `divorce_cases.match_attempts` never regresses (it is the cap on billable
    Claude calls);
  * `owners_first_seen` never invents tenure across an owner change (+20 points,
    and the trace spend that follows);
  * `exempt_parcels` never resurrects a row the newer pull dropped (that is a
    false homestead-removed signal, +10 points, more spend);
  * `ingested_files` never loses a row (a dropped row re-ingests a county CSV).

Every rule is asserted in BOTH merge directions: the two writers in a race must
converge on the same file whichever of them lands second.
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from seller_finder import state_merge
from seller_finder.state import SCHEMA, get_db, owner_hash

TS_OLD = "2026-08-11T06:00:00-05:00"
TS_MID = "2026-08-11T13:00:00-05:00"
#: The same instant as TS_MID written the way imported data and telemetry write
#: it. A lexical compare would sort this AFTER every offset form.
TS_MID_Z = "2026-08-11T18:00:00Z"
TS_NEW = "2026-08-11T21:00:00-05:00"


@pytest.fixture()
def dbs(tmp_path):
    """Two independent state DBs, both created through the production schema."""
    made = []

    def factory(name: str) -> Path:
        path = tmp_path / f"{name}.sqlite3"
        conn = get_db(path, parcels_cache=tmp_path / f"{name}-cache.sqlite3")
        conn.close()
        made.append(path)
        return path

    return factory


def run(path: Path, sql: str, *params) -> None:
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(sql, params)
    conn.close()


def rows(path: Path, table: str, columns: str = "*") -> list[tuple]:
    conn = sqlite3.connect(path)
    out = conn.execute(f"SELECT {columns} FROM {table}").fetchall()
    conn.close()
    return sorted(out)


def one(path: Path, sql: str, *params):
    conn = sqlite3.connect(path)
    row = conn.execute(sql, params).fetchone()
    conn.close()
    return row


def schema_tables() -> set:
    """Committed tables, from state.SCHEMA itself rather than a hand-kept list."""
    probe = sqlite3.connect(":memory:")
    probe.executescript(SCHEMA)
    names = {r[0] for r in probe.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    probe.close()
    return names


# ── The rule set has to be complete, deliberately ────────────────────────────


def test_every_table_in_the_schema_has_a_merge_rule():
    """A new table with no rule falls back to a union that cannot lose a row but
    cannot honour UPSERT semantics either. Better to fail here, while someone is
    adding the table, than to discover it in a run log."""
    missing = sorted(t for t in schema_tables() if state_merge.rule_for(t) is None)
    assert not missing, (
        f"tables with no rule in state_merge: {missing} — add a Ledger, KeyedRow "
        f"or Snapshot entry for each")


def test_no_rule_names_a_table_that_no_longer_exists():
    """The other direction: a rule for a dropped table is dead weight that reads
    like a guarantee."""
    named = {r.name for r in
             state_merge.LEDGERS + state_merge.KEYED_ROWS + state_merge.SNAPSHOTS}
    assert not named - schema_tables()


def test_skip_traces_is_merged_before_the_table_that_references_it():
    """leads.skip_trace_id can only be translated once skip_traces has a remap."""
    order = state_merge._merge_order(schema_tables())
    assert order.index("skip_traces") < order.index("leads")


def test_the_parcel_cache_is_not_a_committed_table():
    """pc.parcels is ephemeral by design — it must not appear in the rule set,
    because a merge of 100K+ raw parcel rows is what the 100 MB ceiling is
    about."""
    assert state_merge.rule_for("parcels") is None
    assert "parcels" not in schema_tables()


# ── Ledgers ──────────────────────────────────────────────────────────────────


def _record_run(path: Path, marker: int, finished: str = TS_MID) -> None:
    run(path, "INSERT INTO runs (run_type, started_at, finished_at, stats) VALUES (?,?,?,?)",
        "weekly_pull", finished, finished, json.dumps({"marker": marker}))


def test_runs_union_keeps_both_lineages_and_ignores_the_autoincrement_id(dbs):
    """Identity is the payload, never the id: the same logical row gets
    different ids in two lineages, and keying on the id would duplicate every
    row on every merge."""
    ours, theirs = dbs("ours"), dbs("theirs")
    _record_run(ours, 1)
    _record_run(ours, 2)
    _record_run(theirs, 3)                     # id 1 in their lineage, id 3 here
    state_merge.merge_databases(str(ours), str(theirs))
    assert {json.loads(r[0])["marker"] for r in rows(ours, "runs", "stats")} == {1, 2, 3}


def test_merging_the_same_pair_twice_inserts_nothing(dbs):
    ours, theirs = dbs("ours"), dbs("theirs")
    _record_run(ours, 1)
    _record_run(theirs, 2)
    state_merge.merge_databases(str(ours), str(theirs))
    first = rows(ours, "runs", "run_type, started_at, stats")
    assert state_merge.merge_databases(str(ours), str(theirs)) == {}
    assert rows(ours, "runs", "run_type, started_at, stats") == first


def test_a_legitimately_duplicated_ledger_row_keeps_both_copies(dbs):
    """The union is multiset-aware: two byte-identical runs really can happen
    (a rerun of the same minute), and collapsing them would understate the day."""
    ours, theirs = dbs("ours"), dbs("theirs")
    _record_run(theirs, 7)
    _record_run(theirs, 7)
    state_merge.merge_databases(str(ours), str(theirs))
    assert len(rows(ours, "runs")) == 2


def test_owner_history_is_an_append_only_audit_trail(dbs):
    ours, theirs = dbs("ours"), dbs("theirs")
    run(ours, "INSERT INTO owner_history (county, prop_id, owner_name, observed_at) "
              "VALUES ('bexar','1001','FIRST OWNER',?)", TS_OLD)
    run(theirs, "INSERT INTO owner_history (county, prop_id, owner_name, observed_at) "
                "VALUES ('bexar','1001','SECOND OWNER',?)", TS_NEW)
    state_merge.merge_databases(str(ours), str(theirs))
    assert rows(ours, "owner_history", "owner_name") == [("FIRST OWNER",), ("SECOND OWNER",)]


def test_an_ingest_ledger_row_is_never_lost_and_keeps_the_earlier_stamp(dbs):
    """ingested_files is what stops a hand-committed county CSV being ingested
    twice. A dropped row means duplicate deed dates; a later stamp is harmless
    but the earlier one is the truth."""
    ours, theirs = dbs("ours"), dbs("theirs")
    run(ours, "INSERT INTO ingested_files (kind, file_name, content_sha256, rows, ingested_at) "
              "VALUES ('deeds','bcad.csv','abc',10,?)", TS_NEW)
    run(theirs, "INSERT INTO ingested_files (kind, file_name, content_sha256, rows, ingested_at) "
                "VALUES ('deeds','bcad.csv','abc',10,?)", TS_OLD)
    run(theirs, "INSERT INTO ingested_files (kind, file_name, content_sha256, rows, ingested_at) "
                "VALUES ('foreclosure','travis.csv','def',3,?)", TS_MID)
    state_merge.merge_databases(str(ours), str(theirs))
    assert len(rows(ours, "ingested_files")) == 2
    assert one(ours, "SELECT ingested_at FROM ingested_files WHERE file_name='bcad.csv'")[0] \
        == TS_OLD


# ── Leads: money and CRM state ───────────────────────────────────────────────


def _lead(path: Path, prop_id: str, **overrides) -> None:
    row = {"county": "bexar", "prop_id": prop_id, "owner_name": "SMITH JOHN",
           "property_addr": f"{prop_id} MAIN ST", "mail_addr": "500 OTHER RD",
           "score": 45, "signals": "[]", "primary_source": "absentee",
           "status": "awaiting_approval", "skip_trace_id": None, "fub_person_id": None,
           "created_at": TS_OLD, "updated_at": TS_OLD}
    row.update(overrides)
    cols = ", ".join(row)
    run(path, f"INSERT INTO leads ({cols}) VALUES ({', '.join('?' * len(row))})",
        *row.values())


@pytest.mark.parametrize("swap", [False, True])
def test_a_lead_that_reached_fub_never_looks_unpushed_again(dbs, swap):
    """A lineage that never saw the push would hand the lead back to
    auto_push_leads, which re-runs the Claude note call and adds a second note to
    the same person. FUB's dedupe catches the person, not the spend."""
    ours, theirs = dbs("ours"), dbs("theirs")
    _lead(ours, "1001", status="pushed", fub_person_id="99", updated_at=TS_MID)
    # The other lineage rescored the same lead later and never saw the push.
    _lead(theirs, "1001", status="qualified", score=60, updated_at=TS_NEW)
    if swap:
        ours, theirs = theirs, ours
    state_merge.merge_databases(str(ours), str(theirs))
    lead = one(ours, "SELECT status, fub_person_id, score FROM leads WHERE prop_id='1001'")
    assert lead[0] == "pushed"
    assert lead[1] == "99", "a known value is never replaced by NULL"
    assert lead[2] == 60, "the newer scoring still wins the descriptive columns"


def test_a_lead_only_one_side_has_is_carried_over(dbs):
    ours, theirs = dbs("ours"), dbs("theirs")
    _lead(ours, "1001")
    _lead(theirs, "2002", owner_name="DOE JANE")
    state_merge.merge_databases(str(ours), str(theirs))
    assert rows(ours, "leads", "prop_id") == [("1001",), ("2002",)]


def test_the_earlier_created_at_wins_and_the_later_updated_at_wins(dbs):
    ours, theirs = dbs("ours"), dbs("theirs")
    _lead(ours, "1001", created_at=TS_MID, updated_at=TS_NEW)
    _lead(theirs, "1001", created_at=TS_OLD, updated_at=TS_MID)
    state_merge.merge_databases(str(ours), str(theirs))
    assert one(ours, "SELECT created_at, updated_at FROM leads") == (TS_OLD, TS_NEW)


def test_timestamps_are_compared_as_instants_not_as_strings(dbs):
    """This repo writes CT-local stamps with an offset; imported data and
    telemetry write "…Z". A lexical compare sorts every Z form last, which would
    make a stale row look like the newest one."""
    ours, theirs = dbs("ours"), dbs("theirs")
    _lead(ours, "1001", status="pushed", fub_person_id="99", updated_at=TS_NEW)
    _lead(theirs, "1001", status="qualified", updated_at=TS_MID_Z)  # same as TS_MID
    state_merge.merge_databases(str(ours), str(theirs))
    assert one(ours, "SELECT updated_at FROM leads")[0] == TS_NEW


# ── Skip traces: never pay twice, never mix two owners ───────────────────────


def _trace(path: Path, owner_key: str, matched: int, emails: list, traced_at: str) -> int:
    conn = sqlite3.connect(path)
    with conn:
        cur = conn.execute(
            "INSERT INTO skip_traces (owner_key, provider, matched, emails, phones, "
            "dnc, litigator, traced_at) VALUES (?,'batchdata',?,?,'[]',0,0,?)",
            (owner_key, matched, json.dumps(emails), traced_at))
        trace_id = cur.lastrowid
    conn.close()
    return trace_id


def test_neither_lineages_paid_trace_is_dropped(dbs):
    ours, theirs = dbs("ours"), dbs("theirs")
    _trace(ours, "OWNER-A|78201", 1, ["a@example.com"], TS_MID)
    _trace(theirs, "OWNER-B|78202", 1, ["b@example.com"], TS_MID)
    state_merge.merge_databases(str(ours), str(theirs))
    assert rows(ours, "skip_traces", "owner_key") == \
        [("OWNER-A|78201",), ("OWNER-B|78202",)]


@pytest.mark.parametrize("swap", [False, True])
def test_a_trace_result_is_taken_whole_never_field_by_field(dbs, swap):
    """(matched, emails, phones, dnc, litigator) is one provider result. Mixing a
    matched=1 from one side with an empty emails from the other produces a lead
    that reads as contactable and has nothing to contact."""
    ours, theirs = dbs("ours"), dbs("theirs")
    _trace(ours, "OWNER-A|78201", 1, ["a@example.com"], TS_NEW)
    _trace(theirs, "OWNER-A|78201", 0, [], TS_MID)
    if swap:
        ours, theirs = theirs, ours
    state_merge.merge_databases(str(ours), str(theirs))
    matched, emails = one(ours, "SELECT matched, emails FROM skip_traces")
    assert (matched, json.loads(emails)) == (1, ["a@example.com"])


def test_a_leads_trace_reference_is_translated_to_our_own_row(dbs):
    """The two lineages hand out skip_traces.id independently, so their id 1 is
    someone else's row here. Copying it would attach one homeowner's lead to
    another homeowner's phone number."""
    ours, theirs = dbs("ours"), dbs("theirs")
    ours_id = _trace(ours, "OWNER-A|78201", 1, ["a@example.com"], TS_MID)
    theirs_id = _trace(theirs, "OWNER-B|78202", 1, ["b@example.com"], TS_MID)
    assert ours_id == theirs_id == 1, "the ids must really collide"
    _lead(ours, "1001", owner_name="OWNER-A", skip_trace_id=ours_id)
    _lead(theirs, "2002", owner_name="OWNER-B", skip_trace_id=theirs_id)

    state_merge.merge_databases(str(ours), str(theirs))
    joined = {row[0]: row[1] for row in rows(
        ours, "leads l LEFT JOIN skip_traces t ON t.id = l.skip_trace_id",
        "l.owner_name, t.owner_key")}
    assert joined == {"OWNER-A": "OWNER-A|78201", "OWNER-B": "OWNER-B|78202"}


def test_an_untranslatable_trace_reference_becomes_null(dbs):
    """Their lead points at a trace their file no longer holds — the tracer
    expires stale no-matches. NULL costs a free cache lookup next run; a wrong
    id costs the wrong person's data."""
    ours, theirs = dbs("ours"), dbs("theirs")
    _trace(ours, "OWNER-A|78201", 1, ["a@example.com"], TS_MID)
    _lead(theirs, "2002", owner_name="OWNER-B", skip_trace_id=1)
    state_merge.merge_databases(str(ours), str(theirs))
    assert one(ours, "SELECT skip_trace_id FROM leads WHERE prop_id='2002'")[0] is None


# ── Divorce matching: the billable retry cap ──────────────────────────────────


def _divorce(path: Path, case: str, attempts: int, matched=None, last=None) -> None:
    run(path, "INSERT INTO divorce_cases (case_number, county, filed_date, party_names, "
              "matched_prop_id, match_confidence, created_at, match_attempts, last_attempt_at) "
              "VALUES (?,'bexar','2026-01-01','[\"A\",\"B\"]',?,?,?,?,?)",
        case, matched, 0.9 if matched else None, TS_OLD, attempts, last)


@pytest.mark.parametrize("swap", [False, True])
def test_the_divorce_match_attempt_cap_never_regresses(dbs, swap):
    """match_attempts is the cap on billable Claude calls (one per party name).
    Lowering it re-sends the same never-matching case every weekly run, forever.
    """
    ours, theirs = dbs("ours"), dbs("theirs")
    _divorce(ours, "D-1", attempts=3, last=TS_NEW)
    _divorce(theirs, "D-1", attempts=1, last=TS_OLD)
    if swap:
        ours, theirs = theirs, ours
    state_merge.merge_databases(str(ours), str(theirs))
    assert one(ours, "SELECT match_attempts, last_attempt_at FROM divorce_cases") \
        == (3, TS_NEW)


@pytest.mark.parametrize("swap", [False, True])
def test_a_divorce_match_found_by_either_side_survives(dbs, swap):
    """The match cost a Claude call. A known value is never replaced by NULL."""
    ours, theirs = dbs("ours"), dbs("theirs")
    _divorce(ours, "D-1", attempts=1, matched="1001", last=TS_OLD)
    _divorce(theirs, "D-1", attempts=2, last=TS_NEW)
    if swap:
        ours, theirs = theirs, ours
    state_merge.merge_databases(str(ours), str(theirs))
    assert one(ours, "SELECT matched_prop_id, match_attempts FROM divorce_cases") \
        == ("1001", 2)


# ── Derived per-parcel state ─────────────────────────────────────────────────


def _first_seen(path: Path, prop_id: str, owner: str, first_seen_at: str) -> None:
    run(path, "INSERT INTO owners_first_seen (county, prop_id, owner_hash, first_seen_at) "
              "VALUES ('bexar',?,?,?)", prop_id, owner_hash(owner), first_seen_at)


@pytest.mark.parametrize("swap", [False, True])
def test_the_same_owner_keeps_the_earliest_sighting(dbs, swap):
    """first_seen_at IS the tenure proxy — the earlier observation is the true
    one, and moving it forward would erase tenure the owner does have."""
    ours, theirs = dbs("ours"), dbs("theirs")
    _first_seen(ours, "1001", "SMITH JOHN", TS_NEW)
    _first_seen(theirs, "1001", "SMITH JOHN", "2014-01-01T06:00:00-06:00")
    if swap:
        ours, theirs = theirs, ours
    state_merge.merge_databases(str(ours), str(theirs))
    assert one(ours, "SELECT first_seen_at FROM owners_first_seen")[0] \
        == "2014-01-01T06:00:00-06:00"


@pytest.mark.parametrize("swap", [False, True])
def test_an_owner_change_resets_the_tenure_clock_instead_of_merging_it(dbs, swap):
    """parcels.py resets first_seen_at when the owner changes. Keeping the older
    stamp would invent tenure the new owner does not have, lift a 30-point
    absentee lead over the 40-point trace threshold, and spend money on it."""
    ours, theirs = dbs("ours"), dbs("theirs")
    _first_seen(ours, "1001", "OLD OWNER", "2014-01-01T06:00:00-06:00")
    _first_seen(theirs, "1001", "NEW OWNER", TS_NEW)
    if swap:
        ours, theirs = theirs, ours
    state_merge.merge_databases(str(ours), str(theirs))
    assert one(ours, "SELECT owner_hash, first_seen_at FROM owners_first_seen") \
        == (owner_hash("NEW OWNER"), TS_NEW)


@pytest.mark.parametrize("swap", [False, True])
def test_the_later_warm_score_wins_and_no_warm_row_is_lost(dbs, swap):
    ours, theirs = dbs("ours"), dbs("theirs")
    run(ours, "INSERT INTO warm_leads (county, prop_id, score, signals, updated_at) "
              "VALUES ('bexar','1001',30,'[]',?)", TS_OLD)
    run(theirs, "INSERT INTO warm_leads (county, prop_id, score, signals, updated_at) "
                "VALUES ('bexar','1001',35,'[]',?)", TS_NEW)
    run(theirs, "INSERT INTO warm_leads (county, prop_id, score, signals, updated_at) "
                "VALUES ('bexar','2002',31,'[]',?)", TS_MID)
    if swap:
        ours, theirs = theirs, ours
    state_merge.merge_databases(str(ours), str(theirs))
    assert rows(ours, "warm_leads", "prop_id, score") == [("1001", 35), ("2002", 31)]


@pytest.mark.parametrize("swap", [False, True])
def test_the_later_parcel_snapshot_bookkeeping_wins(dbs, swap):
    """asset_key decides whether a daily run may skip the owner-change
    bookkeeping. The older row would make a run redo work it already did — or,
    worse, believe unchanged data it has not actually seen."""
    ours, theirs = dbs("ours"), dbs("theirs")
    run(ours, "INSERT INTO parcel_snapshot_meta (county, asset_key, synced_at, kept, absentee) "
              "VALUES ('bexar','old:1',?,10,2)", TS_OLD)
    run(theirs, "INSERT INTO parcel_snapshot_meta (county, asset_key, synced_at, kept, absentee) "
                "VALUES ('bexar','new:2',?,20,4)", TS_NEW)
    if swap:
        ours, theirs = theirs, ours
    state_merge.merge_databases(str(ours), str(theirs))
    assert one(ours, "SELECT asset_key, synced_at, kept FROM parcel_snapshot_meta") \
        == ("new:2", TS_NEW, 20)


def test_a_deed_date_is_never_lost_and_the_later_import_wins(dbs):
    ours, theirs = dbs("ours"), dbs("theirs")
    run(ours, "INSERT INTO deed_dates (county, prop_id, deed_date, source, imported_at) "
              "VALUES ('bexar','1001','2010-05-01','manual_csv',?)", TS_OLD)
    run(theirs, "INSERT INTO deed_dates (county, prop_id, deed_date, source, imported_at) "
                "VALUES ('bexar','1001','2010-06-02','bcad_export',?)", TS_NEW)
    run(theirs, "INSERT INTO deed_dates (county, prop_id, deed_date, source, imported_at) "
                "VALUES ('comal','9001','2001-01-01','bcad_export',?)", TS_MID)
    state_merge.merge_databases(str(ours), str(theirs))
    assert rows(ours, "deed_dates", "county, prop_id, deed_date") == [
        ("bexar", "1001", "2010-06-02"), ("comal", "9001", "2001-01-01")]


# ── The one merge that deletes: exemption snapshots ──────────────────────────


def _exempt(path: Path, county: str, prop_id: str, exempts: str, seen: str) -> None:
    run(path, "INSERT INTO exempt_parcels (county, prop_id, exempts, last_seen_at) "
              "VALUES (?,?,?,?)", county, prop_id, exempts, seen)


@pytest.mark.parametrize("swap", [False, True])
def test_the_later_exemption_pull_replaces_the_county_snapshot(dbs, swap):
    """exemptions.py rewrites a county wholesale from the live feed, and
    homestead_removed is computed as "in the previous snapshot, not in this one".
    Resurrecting a row the later pull dropped awards +10 to a parcel that never
    lost its exemption — and +10 is enough to turn a 30-point absentee lead into
    skip-trace spend.
    """
    ours, theirs = dbs("ours"), dbs("theirs")
    _exempt(ours, "bexar", "1001", "HS", TS_OLD)
    _exempt(ours, "bexar", "1002", "HS-OV65", TS_OLD)     # gone in the later pull
    _exempt(theirs, "bexar", "1001", "HS", TS_NEW)
    if swap:
        ours, theirs = theirs, ours
    state_merge.merge_databases(str(ours), str(theirs))
    assert rows(ours, "exempt_parcels", "prop_id, last_seen_at") == [("1001", TS_NEW)]


def test_a_county_the_later_pull_never_touched_is_left_alone(dbs):
    """The replace is per county, not per table: a run that pulled only Bexar
    must not wipe Comal's snapshot."""
    ours, theirs = dbs("ours"), dbs("theirs")
    _exempt(ours, "comal", "9001", "HS", TS_OLD)
    _exempt(theirs, "bexar", "1001", "HS", TS_NEW)
    state_merge.merge_databases(str(ours), str(theirs))
    assert rows(ours, "exempt_parcels", "county, prop_id") == \
        [("bexar", "1001"), ("comal", "9001")]


def test_our_newer_snapshot_is_not_replaced_by_their_older_one(dbs):
    ours, theirs = dbs("ours"), dbs("theirs")
    _exempt(ours, "bexar", "1001", "HS", TS_NEW)
    _exempt(theirs, "bexar", "1001", "HS", TS_OLD)
    _exempt(theirs, "bexar", "1002", "HS", TS_OLD)
    state_merge.merge_databases(str(ours), str(theirs))
    assert rows(ours, "exempt_parcels", "prop_id, last_seen_at") == [("1001", TS_NEW)]


def test_nothing_outside_a_superseded_snapshot_is_ever_deleted(dbs):
    """The blanket guarantee, swept over the whole schema: every row we hold
    before the merge is still there after it, exemptions aside."""
    ours, theirs = dbs("ours"), dbs("theirs")
    _record_run(ours, 1)
    _lead(ours, "1001", status="pushed", fub_person_id="99")
    _trace(ours, "OWNER-A|78201", 1, ["a@example.com"], TS_MID)
    _divorce(ours, "D-1", attempts=2, matched="1001")
    _first_seen(ours, "1001", "SMITH JOHN", TS_OLD)
    run(ours, "INSERT INTO owner_history (county, prop_id, owner_name, observed_at) "
              "VALUES ('bexar','1001','SMITH JOHN',?)", TS_OLD)
    run(ours, "INSERT INTO ingested_files (kind, file_name, content_sha256, rows, ingested_at) "
              "VALUES ('deeds','a.csv','abc',1,?)", TS_OLD)
    counts_before = {t: len(rows(ours, t)) for t in schema_tables()}

    _record_run(theirs, 2)
    _lead(theirs, "2002")
    state_merge.merge_databases(str(ours), str(theirs))

    for table, before in counts_before.items():
        assert len(rows(ours, table)) >= before, f"{table} lost rows"


# ── Convergence ──────────────────────────────────────────────────────────────


def _canonical(path: Path) -> dict:
    """Every table's rows, with surrogate ids projected out.

    `leads.skip_trace_id` is resolved to the trace's natural key: the ids
    themselves are per-lineage and MUST differ, so comparing them would be
    comparing noise. What has to agree is which owner's trace each lead points
    at.
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    traces = {r["id"]: r["owner_key"] for r in conn.execute(
        "SELECT id, owner_key FROM skip_traces")}
    out = {}
    for table in sorted(schema_tables()):
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        payload = [c for c in columns if c != "id"]
        rendered = []
        for row in conn.execute(f"SELECT {', '.join(payload)} FROM {table}"):
            record = dict(zip(payload, row))
            if table == "leads":
                record["skip_trace_id"] = traces.get(record["skip_trace_id"])
            rendered.append(tuple(sorted(record.items(), key=lambda kv: kv[0])))
        out[table] = sorted(rendered, key=repr)   # rows mix NULLs and strings
    conn.close()
    return out


def test_both_racers_converge_on_the_same_file(dbs):
    """merge(A, B) and merge(B, A) must agree, or the two writers in a race push
    different files and the loser's retry has nothing stable to build on."""
    a, b = dbs("a"), dbs("b")
    _record_run(a, 1)
    _record_run(b, 2)
    _lead(a, "1001", status="pushed", fub_person_id="99", updated_at=TS_MID)
    _lead(b, "1001", status="qualified", score=60, updated_at=TS_NEW)
    _lead(b, "2002", owner_name="DOE JANE")
    trace_a = _trace(a, "OWNER-A|78201", 1, ["a@example.com"], TS_MID)
    _trace(b, "OWNER-B|78202", 1, ["b@example.com"], TS_NEW)
    run(a, "UPDATE leads SET skip_trace_id=? WHERE prop_id='1001'", trace_a)
    _divorce(a, "D-1", attempts=3, last=TS_NEW)
    _divorce(b, "D-1", attempts=1, matched="1001", last=TS_OLD)
    _first_seen(a, "1001", "OLD OWNER", TS_OLD)
    _first_seen(b, "1001", "NEW OWNER", TS_NEW)
    _exempt(a, "bexar", "1001", "HS", TS_OLD)
    _exempt(b, "bexar", "1002", "HS-OV65", TS_NEW)

    a_into_b, b_into_a = dbs("a2"), dbs("b2")
    for src, dst in ((a, a_into_b), (b, b_into_a)):
        dst.write_bytes(src.read_bytes())
    state_merge.merge_databases(str(a_into_b), str(b))   # A merging B in
    state_merge.merge_databases(str(b_into_a), str(a))   # B merging A in
    assert _canonical(a_into_b) == _canonical(b_into_a)


# ── Failure discipline ───────────────────────────────────────────────────────


def test_a_merge_that_fails_leaves_our_file_exactly_as_it_was(dbs, monkeypatch):
    """One transaction. state_sync turns a MergeError into a failed push, and a
    failed push must not leave half a merge behind for the next run to inherit.
    """
    ours, theirs = dbs("ours"), dbs("theirs")
    _record_run(ours, 1)
    _record_run(theirs, 2)
    _lead(theirs, "2002")
    _divorce(theirs, "D-1", attempts=1)
    before = ours.read_bytes()

    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    # exempt_parcels is merged after deed_dates and divorce_cases, so rows have
    # already been written into `ours` when this fires.
    monkeypatch.setattr(state_merge, "merge_snapshot", explode)
    with pytest.raises(state_merge.MergeError):
        state_merge.merge_databases(str(ours), str(theirs))
    assert rows(ours, "divorce_cases") == []
    assert {json.loads(r[0])["marker"] for r in rows(ours, "runs", "stats")} == {1}
    assert ours.read_bytes() == before


def test_two_schemas_that_share_no_columns_refuse_to_merge(dbs):
    """Better a failed push than a table quietly emptied by a rename."""
    ours, theirs = dbs("ours"), dbs("theirs")
    run(ours, "CREATE TABLE experiment (b TEXT)")
    run(theirs, "CREATE TABLE experiment (a TEXT)")
    with pytest.raises(state_merge.MergeError):
        state_merge.merge_databases(str(ours), str(theirs))


def test_a_table_only_their_lineage_has_is_created_not_dropped(dbs):
    """Their job ran newer code. Take the table rather than every row in it."""
    ours, theirs = dbs("ours"), dbs("theirs")
    run(theirs, "CREATE TABLE experiment (id INTEGER PRIMARY KEY AUTOINCREMENT, note TEXT)")
    run(theirs, "INSERT INTO experiment (note) VALUES ('from the newer code')")
    summary = state_merge.merge_databases(str(ours), str(theirs))
    assert summary["experiment"]["created"] is True
    assert summary["experiment"]["unclassified"] == state_merge.UNCLASSIFIED_FALLBACK
    assert rows(ours, "experiment", "note") == [("from the newer code",)]


def test_a_column_only_their_lineage_has_is_reported_not_guessed(dbs):
    ours, theirs = dbs("ours"), dbs("theirs")
    run(theirs, "ALTER TABLE runs ADD COLUMN experiment TEXT")
    _record_run(theirs, 5)
    summary = state_merge.merge_databases(str(ours), str(theirs))
    assert summary["runs"]["columns_ignored"] == ["experiment"]
    assert len(rows(ours, "runs")) == 1


def test_the_summary_says_what_happened(dbs):
    ours, theirs = dbs("ours"), dbs("theirs")
    _record_run(theirs, 1)
    summary = state_merge.merge_databases(str(ours), str(theirs))
    assert "runs +1" in state_merge.format_summary(summary)
    assert state_merge.format_summary({}) == \
        "nothing to reconcile — the two lineages already agree"


def test_sqlite_internal_bookkeeping_is_not_merged(dbs):
    """sqlite_sequence is derived from the rows; sqlite maintains it on INSERT."""
    ours, theirs = dbs("ours"), dbs("theirs")
    _record_run(theirs, 1)
    summary = state_merge.merge_databases(str(ours), str(theirs))
    assert "sqlite_sequence" not in summary


def test_a_merged_db_still_opens_through_the_production_schema(dbs, tmp_path):
    """The file the push carries is the file the next run resumes from, and it
    goes through get_db()'s migrations on the way in."""
    ours, theirs = dbs("ours"), dbs("theirs")
    _record_run(theirs, 1)
    _lead(theirs, "2002")
    state_merge.merge_databases(str(ours), str(theirs))
    conn = get_db(ours, parcels_cache=tmp_path / "reopen-cache.sqlite3")
    assert conn.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"] == 1
    conn.close()


def test_the_cli_refuses_a_missing_file(tmp_path, capsys):
    assert state_merge.main(["--ours", str(tmp_path / "nope.sqlite3"),
                             "--theirs", str(tmp_path / "also-nope.sqlite3")]) == 1


def test_the_cli_merges_two_real_files(dbs, capsys):
    ours, theirs = dbs("ours"), dbs("theirs")
    _record_run(theirs, 1)
    assert state_merge.main(["--ours", str(ours), "--theirs", str(theirs)]) == 0
    assert "runs +1" in capsys.readouterr().out
    assert len(rows(ours, "runs")) == 1


def test_reconciling_one_row_is_pure_and_order_independent():
    """The rule engine itself, driven directly in both argument orders."""
    spec = state_merge.KeyedRow(
        name="leads", key=("county", "prop_id"), clock=("updated_at",),
        forward_only=("updated_at",), backward_only=("created_at",),
        sticky=(("status", "pushed"),))
    columns = ("county", "prop_id", "status", "fub_person_id", "created_at", "updated_at")
    mine = {"county": "bexar", "prop_id": "1", "status": "pushed",
            "fub_person_id": "99", "created_at": TS_MID, "updated_at": TS_MID}
    yours = {"county": "bexar", "prop_id": "1", "status": "qualified",
             "fub_person_id": None, "created_at": TS_OLD, "updated_at": TS_NEW}
    expected = {"county": "bexar", "prop_id": "1", "status": "pushed",
                "fub_person_id": "99", "created_at": TS_OLD, "updated_at": TS_NEW}
    assert state_merge.reconcile_keyed_row(mine, yours, spec, columns) == expected
    assert state_merge.reconcile_keyed_row(yours, mine, spec, columns) == expected
    assert mine["status"] == "pushed" and yours["status"] == "qualified", \
        "reconcile must not mutate its inputs"

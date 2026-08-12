"""state_merge.py — reconcile two lineages of the committed state DB.

Three workflows write one SQLite file that lives, encrypted, as a whole-file
blob on the orphan `state` branch. The old sync was pull-at-start /
push-at-end with no merge and no compare-and-swap, so any run that pulled
before a sibling's push and finished after it overwrote everything the sibling
had written — and both pushes reported success. That is the mechanism that
cost LDR-Automation-Clean sixteen hours of unpersisted state on 2026-08-11
(their PR #9 → #10); this module is the seller-side port of the fix.

state_sync.py detects the conflict (the encrypted blob moved since we pulled).
This module is what makes the conflict survivable: it folds the rows that
appeared on the branch while we were running INTO our file, so the push that
follows carries both sides. Nothing here talks to git or openssl — it takes
two SQLite files.

─────────────────────────────────────────────────────────────────────────────
WHY A ROW-LEVEL MERGE IS SAFE HERE

Every committed table falls into one of four shapes, and none needs a
three-way base:

LEDGERS (append-only). `runs`, `owner_history`, `ingested_files` only ever
    grow. Merging is a union. `runs` and `owner_history` carry an
    autoincrement `id` and no uniqueness constraint, so identity is the
    PAYLOAD, never the id: the same logical row gets different ids in two
    lineages, and keying on the id would duplicate every row on every merge.
    The union is multiset-aware (max of the two counts per key) so a lineage
    that legitimately holds two identical rows keeps both, and it is
    idempotent: merging the same pair again inserts nothing.

    `runs` is THE table the 2026-08-11 loss was measured in on the other side:
    telemetry.py derives every published counter from it, so a lost run row is
    a day that publishes a confident zero for work that actually happened.

    `ingested_files` is the exactly-once ledger for hand-committed county
    CSVs. Losing a row there re-ingests a file — duplicate deed dates and
    foreclosure notices — so it is never dropped, and on doubt it keeps the
    EARLIER ingest stamp, which is the one the rows were written under.

KEYED ROWS (UPSERT, one row per natural key). Reconciled field by field under
    four rules, chosen so a merge can only ever cost LESS money and leak LESS
    data than either side alone:

      forward_only  — clocks and capped counters. Always the maximum of the
                      two sides. `divorce_cases.match_attempts` is the one
                      that costs money: lowering it re-sends the same party
                      names to Claude on every future weekly run, forever
                      (see divorce.MAX_MATCH_ATTEMPTS).
      backward_only — "first observed at" stamps (`created_at`). Always the
                      minimum: the earlier observation is the true one.
      reset_on      — a column whose change invalidates the rest of the row.
                      `owners_first_seen.owner_hash`: parcels.py resets
                      `first_seen_at` to now when the owner changes, because
                      first_seen_at IS the tenure proxy that pays out +20 for
                      "owned 10+ years". Keeping the older stamp across an
                      owner change would invent tenure the new owner does not
                      have, lift a 30-point absentee lead over the 40-point
                      trace threshold, and spend real money on it. So the side
                      that saw the newer owner wins the row whole.
      sticky        — a status a lead can reach but never come back from.
                      `leads.status == 'pushed'`: a lineage that never saw the
                      push would otherwise hand the lead back to
                      auto_push_leads, which re-runs the Claude note call and
                      adds a second note to the same FUB person. FUB's dedupe
                      catches the person, not the spend.

    Everything else — score, signals, addresses, contact info — comes from the
    side whose clock is newer, except that a known value is never replaced by
    NULL (which is what keeps `fub_person_id` once either side has it).

    `skip_traces` is deliberately NOT field-wise beyond that: (matched,
    emails, phones, dnc, litigator) is one atomic provider result, and mixing
    a `matched=1` from one side with an empty `emails` from the other would
    produce a lead that reads as traced and contactable but has no contact
    info. The whole newer row wins, and neither side's paid row is ever
    dropped — the cache is what stops this repo paying twice for one owner.

    SURROGATE IDS ARE REMAPPED, NOT COPIED. `leads.skip_trace_id` points at
    `skip_traces.id`, and the two lineages assign those ids independently, so
    their id 5 is very likely someone else's row here. skip_traces is merged
    first, keyed on its natural `owner_key`, building their-id → our-id; every
    incoming `leads.skip_trace_id` is translated through that map, and a
    reference that cannot be translated becomes NULL (the lead re-traces from
    cache, free) rather than silently pointing at ANOTHER HOMEOWNER'S phone
    number and email.

    CONSEQUENCE WORTH KNOWING: a lead merged in from the other lineage gets a
    fresh local `leads.id`, because both lineages autoincrement from the same
    base and their id is usually already taken here. Rows already in this file
    keep theirs. So the `exclude_ids` input of the push-approved workflow — which
    matches on `leads.id` — can name a different lead than the review CSV it was
    read from, if that CSV came from the run that LOST the race. The CSV also
    carries county, prop_id and owner, which are stable; check those before
    excluding after a run whose push logged a CONFLICT. (Under the old
    whole-file push the losing run's rows were not renumbered — they were gone.)

SNAPSHOTS (`exempt_parcels`). A full per-county replace: exemptions.py deletes
    the county and rewrites it from the live feed every weekly run. This is
    THE ONE PLACE this module deletes rows, and it has to. Resurrecting a row
    the other lineage's newer pull dropped would make the next run see
    "previously had homestead, does not now" and award +10 homestead-removed
    to a parcel that never lost anything — the exact false-positive
    exemptions.py's truncation guard exists to prevent, and +10 is enough to
    turn a 30-point absentee lead into skip-trace spend. So the county's
    snapshot is taken WHOLE from whichever side pulled it later; nothing
    irreplaceable is lost, because the snapshot is only a diff baseline and is
    rebuilt from the feed on the next run.

The result is commutative: merge(A, B) and merge(B, A) agree on every column,
which is what lets the two writers in a race reach the same file whichever of
them lands second. The tests in tests/test_state_merge.py assert that in both
directions, table by table.

A table the DB has and this module does not know about is NOT overwritten — it
falls back to a union that cannot lose a row (see UNCLASSIFIED_FALLBACK below)
and is reported as unclassified in the summary. test_state_merge.py fails if
any table state.SCHEMA creates ends up there, so the rule set has to be
extended deliberately rather than discovered in a run log.

NOT MERGED: pc.parcels. The full parcel snapshot is ephemeral by design (see
state.py) — it lives in a gitignored cache DB that is never committed, never
pulled, and rebuilt from the release mirror every run.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

# SQLite's own bookkeeping for AUTOINCREMENT columns. It is derived from the
# rows, so merging it would be meaningless; sqlite maintains it on INSERT.
INTERNAL_TABLES = {"sqlite_sequence"}


@dataclass(frozen=True)
class Ledger:
    """An append-only table. Merging is a union keyed on the payload."""

    name: str
    key: tuple[str, ...]
    #: Autoincrement surrogate key. Never part of identity, never copied.
    rowid_column: Optional[str] = None
    #: Columns outside the key where the EARLIER value wins.
    earliest: tuple[str, ...] = ()


@dataclass(frozen=True)
class KeyedRow:
    """One row per natural key, written by UPSERT. Reconciled field by field."""

    name: str
    #: Columns tried in order for "which side is newer" — first non-null wins,
    #: i.e. COALESCE, because the later clocks are NULL until something happens.
    clock: tuple[str, ...]
    key: tuple[str, ...]
    #: Autoincrement surrogate key: dropped on insert, remapped for referrers.
    rowid_column: Optional[str] = None
    forward_only: tuple[str, ...] = ()
    backward_only: tuple[str, ...] = ()
    reset_on: tuple[str, ...] = ()
    #: (column, value) pairs a row can reach but never come back from.
    sticky: tuple[tuple[str, str], ...] = ()
    #: (column, referenced table) — translated through that table's id remap.
    foreign_keys: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Snapshot:
    """A table rewritten wholesale, one partition at a time, from a live feed.

    The later pull of a partition replaces the earlier one entirely. See the
    module docstring: for exempt_parcels a union would manufacture
    homestead-removed signals, and those cost money.
    """

    name: str
    partition: tuple[str, ...]
    clock: str


LEDGERS: tuple[Ledger, ...] = (
    # Every published number comes out of this table (telemetry.load_runs), so
    # a lost row is a day that reports zero for work that really happened.
    Ledger(
        name="runs",
        key=("run_type", "started_at", "finished_at", "stats"),
        rowid_column="id",
    ),
    Ledger(
        name="owner_history",
        key=("county", "prop_id", "owner_name", "observed_at"),
        rowid_column="id",
    ),
    # Exactly-once ledger for data/inbox CSVs. A dropped row re-ingests a file.
    Ledger(
        name="ingested_files",
        key=("kind", "file_name", "content_sha256"),
        earliest=("ingested_at",),
    ),
)

KEYED_ROWS: tuple[KeyedRow, ...] = (
    # Merged BEFORE leads: its id remap is what keeps a lead attached to its
    # own owner's contact info. Never field-wise — one provider result, whole.
    KeyedRow(
        name="skip_traces",
        key=("owner_key",),
        clock=("traced_at",),
        rowid_column="id",
    ),
    KeyedRow(
        name="leads",
        key=("county", "prop_id"),
        clock=("updated_at", "created_at"),
        rowid_column="id",
        forward_only=("updated_at",),
        backward_only=("created_at",),
        sticky=(("status", "pushed"),),
        foreign_keys=(("skip_trace_id", "skip_traces"),),
    ),
    KeyedRow(
        name="divorce_cases",
        key=("case_number",),
        clock=("last_attempt_at", "created_at"),
        rowid_column="id",
        # match_attempts is the cap on billable Claude calls; lowering it means
        # paying for the same never-matching case every week from now on.
        forward_only=("match_attempts", "last_attempt_at"),
        backward_only=("created_at",),
    ),
    KeyedRow(
        name="deed_dates",
        key=("county", "prop_id"),
        clock=("imported_at",),
        forward_only=("imported_at",),
    ),
    KeyedRow(
        name="owners_first_seen",
        key=("county", "prop_id"),
        clock=("first_seen_at",),
        backward_only=("first_seen_at",),
        # An owner change resets the tenure clock on purpose — see the module
        # docstring. The side that saw the newer owner takes the row whole.
        reset_on=("owner_hash",),
    ),
    KeyedRow(
        name="warm_leads",
        key=("county", "prop_id"),
        clock=("updated_at",),
        forward_only=("updated_at",),
    ),
    KeyedRow(
        name="parcel_snapshot_meta",
        key=("county",),
        clock=("synced_at",),
        forward_only=("synced_at",),
    ),
)

SNAPSHOTS: tuple[Snapshot, ...] = (
    Snapshot(name="exempt_parcels", partition=("county",), clock="last_seen_at"),
)

#: How an unknown table is merged: union on every column except an
#: autoincrement `id`, keeping both sides' rows. It cannot preserve UPSERT
#: semantics — two versions of one lead's row would both survive, and the
#: UNIQUE(county, prop_id) style constraints in state.SCHEMA would refuse the
#: insert — but it cannot silently lose a write either, which is the failure
#: this module exists to stop.
UNCLASSIFIED_FALLBACK = "union-all-columns"


class MergeError(RuntimeError):
    """Raised when the two files cannot be reconciled. Fails the push loudly."""


# ── Ordering ─────────────────────────────────────────────────────────────────


def _order_key(value) -> tuple:
    """Total order over the values these columns actually hold.

    NULL sorts below everything, so max() never picks "no value" over a value
    and min() never picks it over a real timestamp (callers drop NULLs before
    calling min). Timestamps are compared as INSTANTS, not as strings: this
    repo writes CT-local stamps with an offset (state.now_iso) while imported
    data and telemetry use "…Z", and a lexical compare puts the Z form after
    every offset form.
    """
    if value is None:
        return (0,)
    if isinstance(value, bool):
        return (1, float(value))
    if isinstance(value, (int, float)):
        return (1, float(value))
    text = str(value)
    parsed = _parse_instant(text)
    if parsed is not None:
        return (2, parsed)
    return (3, text)


def _parse_instant(text: str) -> Optional[float]:
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def _newest(values: Iterable) -> Optional[object]:
    present = [v for v in values if v is not None]
    return max(present, key=_order_key) if present else None


def _oldest(values: Iterable) -> Optional[object]:
    present = [v for v in values if v is not None]
    return min(present, key=_order_key) if present else None


def _clock_value(row: dict, spec: KeyedRow):
    for column in spec.clock:
        value = row.get(column)
        if value is not None:
            return value
    return None


def _row_rank(row: dict, spec: KeyedRow, columns: Sequence[str]) -> tuple:
    """Sort key deciding which side's row is "newer".

    The clock decides it. The full row is the tie-break, and it is there for
    commutativity rather than for meaning: two sides holding different rows
    with the same clock must reduce to the SAME row no matter which one is
    merging, or the two writers in a race would push different files.
    """
    return (
        _order_key(_clock_value(row, spec)),
        tuple(_order_key(row.get(c)) for c in columns),
    )


# ── Schema inspection ────────────────────────────────────────────────────────


def _tables(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {name: sql for name, sql in rows if name not in INTERNAL_TABLES}


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _read_rows(conn: sqlite3.Connection, table: str, columns: Sequence[str]) -> list[dict]:
    cols = ", ".join(f'"{c}"' for c in columns)
    return [
        dict(zip(columns, row))
        for row in conn.execute(f'SELECT {cols} FROM "{table}"').fetchall()
    ]


def _autoincrement_column(conn: sqlite3.Connection, table: str) -> Optional[str]:
    for row in conn.execute(f"PRAGMA table_info({table})").fetchall():
        _, name, decl_type, _, _, pk = row
        if pk and str(decl_type).upper() == "INTEGER":
            return name
    return None


# ── Table merges ─────────────────────────────────────────────────────────────


def _insert(conn: sqlite3.Connection, table: str, row: dict) -> int:
    cols = list(row)
    cur = conn.execute(
        f'INSERT INTO "{table}" ({", ".join(chr(34) + c + chr(34) for c in cols)}) '
        f'VALUES ({", ".join("?" for _ in cols)})',
        [row[c] for c in cols],
    )
    return cur.lastrowid


def merge_ledger(
    ours: sqlite3.Connection,
    theirs: sqlite3.Connection,
    spec: Ledger,
    columns: Sequence[str],
) -> dict:
    """Union an append-only table. Never updates identity, never deletes."""
    payload = [c for c in columns if c != spec.rowid_column]

    def key_of(row: dict) -> tuple:
        return tuple(row[c] for c in spec.key)

    our_rows = _read_rows(ours, spec.name, payload)
    their_rows = _read_rows(theirs, spec.name, payload)

    our_counts = Counter(key_of(r) for r in our_rows)
    inserted = 0
    for key, their_count in Counter(key_of(r) for r in their_rows).items():
        # max of the two counts, so a lineage holding two byte-identical rows
        # keeps both AND a second merge of the same pair inserts nothing.
        missing = their_count - our_counts.get(key, 0)
        if missing <= 0:
            continue
        template = next(r for r in their_rows if key_of(r) == key)
        for _ in range(missing):
            _insert(ours, spec.name, template)
        inserted += missing

    updated = 0
    if spec.earliest:
        ours_by_key = {key_of(r): r for r in our_rows}
        for their_row in their_rows:
            our_row = ours_by_key.get(key_of(their_row))
            if our_row is None:
                continue  # just inserted verbatim above
            changes = {
                column: _oldest([our_row.get(column), their_row.get(column)])
                for column in spec.earliest
            }
            changes = {c: v for c, v in changes.items() if v != our_row.get(c)}
            if not changes:
                continue
            assignments = ", ".join(f'"{c}"=?' for c in changes)
            where = " AND ".join(f'"{c}" IS ?' for c in spec.key)
            ours.execute(
                f'UPDATE "{spec.name}" SET {assignments} WHERE {where}',
                [*changes.values(), *key_of(their_row)],
            )
            updated += 1

    return {"inserted": inserted, "updated": updated}


def reconcile_keyed_row(ours: dict, theirs: dict, spec: KeyedRow,
                        columns: Sequence[str]) -> dict:
    """Field-by-field reconciliation of one row. Pure; the tests drive it
    directly, in both argument orders."""
    ranked = sorted([ours, theirs], key=lambda r: _row_rank(r, spec, columns))
    older, newer = ranked[0], ranked[1]

    if spec.reset_on and any(ours.get(c) != theirs.get(c) for c in spec.reset_on):
        # The reset column changed, so the rest of the row belongs to the older
        # observation and must not be carried over — see owners_first_seen in
        # the module docstring.
        merged = dict(newer)
    else:
        merged = dict(newer)
        for column in columns:
            if column in spec.forward_only:
                merged[column] = _newest([ours.get(column), theirs.get(column)])
            elif column in spec.backward_only:
                merged[column] = _oldest([ours.get(column), theirs.get(column)])
            elif merged.get(column) is None:
                # Never let a merge replace a value with "unknown".
                merged[column] = older.get(column)

    for column, value in spec.sticky:
        # A one-way door: reached by either side, held by the merge.
        if column in columns and value in (ours.get(column), theirs.get(column)):
            merged[column] = value
    return merged


def _translate(row: dict, spec: KeyedRow, remaps: dict[str, dict]) -> dict:
    """Point their surrogate references at OUR rows for the same real thing.

    An untranslatable reference becomes NULL. That costs a free cache lookup on
    the next run; keeping their number would attach one homeowner's lead to
    another homeowner's phone number and email.
    """
    if not spec.foreign_keys:
        return row
    out = dict(row)
    for column, table in spec.foreign_keys:
        if column not in out or out[column] is None:
            continue
        out[column] = remaps.get(table, {}).get(out[column])
    return out


def merge_keyed_table(
    ours: sqlite3.Connection,
    theirs: sqlite3.Connection,
    spec: KeyedRow,
    columns: Sequence[str],
    remaps: Optional[dict[str, dict]] = None,
) -> dict:
    """Reconcile one row per natural key, and record this table's id remap."""
    remaps = remaps if remaps is not None else {}
    payload_columns = [c for c in columns if c != spec.rowid_column]

    def key_of(row: dict) -> tuple:
        return tuple(row[c] for c in spec.key)

    our_rows = {key_of(r): r for r in _read_rows(ours, spec.name, columns)}
    remap = remaps.setdefault(spec.name, {})
    inserted = updated = 0

    for their_row in _read_rows(theirs, spec.name, columns):
        key = key_of(their_row)
        their_id = their_row.get(spec.rowid_column) if spec.rowid_column else None
        our_row = our_rows.get(key)
        incoming = _translate(their_row, spec, remaps)

        if our_row is None:
            # The surrogate id is dropped, never copied: ours assigns its own.
            new_id = _insert(
                ours, spec.name, {c: incoming[c] for c in payload_columns})
            if spec.rowid_column and their_id is not None:
                remap[their_id] = new_id
            inserted += 1
            continue

        if spec.rowid_column and their_id is not None:
            remap[their_id] = our_row[spec.rowid_column]

        merged = reconcile_keyed_row(our_row, incoming, spec, payload_columns)
        if all(merged.get(c) == our_row.get(c) for c in payload_columns):
            continue
        where = " AND ".join(f'"{c}" IS ?' for c in spec.key)
        assignments = ", ".join(
            chr(34) + c + chr(34) + "=?" for c in payload_columns if c not in spec.key)
        if not assignments:
            # A table that is nothing but its key: presence is the whole fact,
            # and the row is already present.
            continue
        ours.execute(
            f'UPDATE "{spec.name}" SET {assignments} WHERE {where}',
            [*(merged[c] for c in payload_columns if c not in spec.key), *key],
        )
        updated += 1
    return {"inserted": inserted, "updated": updated}


def _snapshot_rank(rows: Sequence[dict], spec: Snapshot,
                   columns: Sequence[str]) -> tuple:
    """Which side's snapshot of one partition is the later one.

    The pull stamp decides it (every row in a partition shares it). Row count
    and then the sorted rows themselves are the tie-break — meaningless, but
    deterministic, so both racers pick the same winner and converge.
    """
    return (
        _order_key(_newest([r.get(spec.clock) for r in rows])),
        len(rows),
        sorted(tuple(_order_key(r.get(c)) for c in columns) for r in rows),
    )


def merge_snapshot(
    ours: sqlite3.Connection,
    theirs: sqlite3.Connection,
    spec: Snapshot,
    columns: Sequence[str],
) -> dict:
    """Replace whole partitions with the later pull. THE ONE MERGE THAT DELETES.

    Justified in the module docstring: a union here manufactures
    homestead-removed signals, and those turn straight into skip-trace spend.
    """
    def part_of(row: dict) -> tuple:
        return tuple(row[c] for c in spec.partition)

    ours_by_part: dict[tuple, list[dict]] = {}
    for row in _read_rows(ours, spec.name, columns):
        ours_by_part.setdefault(part_of(row), []).append(row)
    theirs_by_part: dict[tuple, list[dict]] = {}
    for row in _read_rows(theirs, spec.name, columns):
        theirs_by_part.setdefault(part_of(row), []).append(row)

    inserted = deleted = 0
    where = " AND ".join(f'"{c}" IS ?' for c in spec.partition)
    for part, their_rows in theirs_by_part.items():
        our_rows = ours_by_part.get(part, [])
        if our_rows and _snapshot_rank(our_rows, spec, columns) >= \
                _snapshot_rank(their_rows, spec, columns):
            continue
        if our_rows:
            deleted += ours.execute(
                f'DELETE FROM "{spec.name}" WHERE {where}', part).rowcount
        for row in their_rows:
            _insert(ours, spec.name, row)
            inserted += 1
    return {"inserted": inserted, "deleted": deleted}


def merge_unclassified(
    ours: sqlite3.Connection,
    theirs: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
) -> dict:
    """Safety net for a table added to state.SCHEMA without a rule here."""
    rowid_column = _autoincrement_column(ours, table)
    spec = Ledger(
        name=table,
        key=tuple(c for c in columns if c != rowid_column),
        rowid_column=rowid_column,
    )
    result = merge_ledger(ours, theirs, spec, columns)
    result["unclassified"] = UNCLASSIFIED_FALLBACK
    return result


def rule_for(table: str):
    for ledger in LEDGERS:
        if ledger.name == table:
            return ledger
    for keyed in KEYED_ROWS:
        if keyed.name == table:
            return keyed
    for snapshot in SNAPSHOTS:
        if snapshot.name == table:
            return snapshot
    return None


def _merge_order(tables: Iterable[str]) -> list[str]:
    """Referenced tables first, so an id remap exists before its referrer.

    One level deep is all this schema has (leads → skip_traces) and all this
    function claims: a table with no declared foreign key is merged in the
    first pass, everything else in the second.
    """
    def has_foreign_keys(table: str) -> bool:
        rule = rule_for(table)
        return bool(isinstance(rule, KeyedRow) and rule.foreign_keys)

    return sorted(tables, key=lambda t: (has_foreign_keys(t), t))


# ── The whole file ───────────────────────────────────────────────────────────


def merge_databases(ours_path: str, theirs_path: str) -> dict:
    """Fold `theirs` into `ours`, in place. Returns a per-table summary.

    `ours` is the running job's file — the one about to be pushed. `theirs` is
    what is currently on the `state` branch. Nothing is deleted except a
    superseded snapshot partition (see merge_snapshot), so the result carries
    every row either side wrote, and running it twice with the same inputs
    changes nothing the second time.

    All of it is one transaction: a merge that raises leaves `ours` exactly as
    it was, and state_sync turns that into a failed push rather than a push of
    half a merge.
    """
    ours = sqlite3.connect(ours_path)
    theirs = sqlite3.connect(f"{Path(theirs_path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        summary: dict = {}
        remaps: dict[str, dict] = {}
        with ours:  # commit on success, roll back on any exception
            our_tables = _tables(ours)
            their_tables = _tables(theirs)

            for table, create_sql in their_tables.items():
                if table not in our_tables and create_sql:
                    # Their lineage ran newer code. Take the table rather than
                    # dropping every row in it.
                    ours.execute(create_sql)
                    our_tables[table] = create_sql
                    summary.setdefault(table, {})["created"] = True

            for table in _merge_order(their_tables):
                our_columns = _columns(ours, table)
                their_columns = _columns(theirs, table)
                shared = [c for c in our_columns if c in their_columns]
                if not shared:
                    raise MergeError(f"{table}: the two schemas share no columns")

                rule = rule_for(table)
                if isinstance(rule, Ledger):
                    result = merge_ledger(ours, theirs, rule, shared)
                elif isinstance(rule, KeyedRow):
                    result = merge_keyed_table(ours, theirs, rule, shared, remaps)
                elif isinstance(rule, Snapshot):
                    result = merge_snapshot(ours, theirs, rule, shared)
                else:
                    result = merge_unclassified(ours, theirs, table, shared)

                dropped = [c for c in their_columns if c not in our_columns]
                if dropped:
                    # Their lineage has a column we cannot store. Say so; the
                    # rows still land, minus that field.
                    result["columns_ignored"] = dropped
                if any(result.get(k) for k in
                       ("inserted", "updated", "deleted", "columns_ignored")):
                    summary.setdefault(table, {}).update(result)
    except sqlite3.Error as exc:
        raise MergeError(f"merge failed: {exc}") from exc
    finally:
        theirs.close()
        ours.close()
    return summary


def format_summary(summary: dict) -> str:
    if not summary:
        return "nothing to reconcile — the two lineages already agree"
    parts = []
    for table in sorted(summary):
        detail = summary[table]
        bits = [f"+{detail.get('inserted', 0)}"]
        if detail.get("updated"):
            bits.append(f"~{detail['updated']}")
        if detail.get("deleted"):
            bits.append(f"-{detail['deleted']} superseded")
        if detail.get("created"):
            bits.append("new table")
        if detail.get("unclassified"):
            bits.append("no merge rule")
        if detail.get("columns_ignored"):
            bits.append("dropped " + ",".join(detail["columns_ignored"]))
        parts.append(f"{table} {' '.join(bits)}")
    return "; ".join(parts)


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: Optional[Sequence[str]] = None) -> int:
    """`python -m seller_finder.state_merge --ours A --theirs B`.

    Exit codes:
      0 — merged (possibly a no-op).
      1 — could not merge. The caller must NOT push: `ours` is unchanged, and
          pushing it would discard whatever is in `theirs`.
    """
    parser = argparse.ArgumentParser(
        description="Fold the state branch's DB into this run's copy.")
    parser.add_argument("--ours", required=True, help="This run's DB. Merged in place.")
    parser.add_argument("--theirs", required=True, help="The DB currently on the state branch.")
    args = parser.parse_args(argv)

    for path in (args.ours, args.theirs):
        if not Path(path).exists():
            print(f"[state-merge] {path} does not exist", file=sys.stderr)
            return 1
    try:
        summary = merge_databases(args.ours, args.theirs)
    except MergeError as exc:
        print(f"[state-merge] REFUSING TO PUSH — {exc}", file=sys.stderr)
        return 1
    print(f"[state-merge] {format_summary(summary)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

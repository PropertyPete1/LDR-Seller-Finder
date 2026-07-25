"""Encrypted SQLite state management — split into committed + ephemeral DBs.

The committed DB (data/seller_finder.sqlite3) is plain SQLite while a
workflow is running; the state-sync composite action encrypts it
(AES-256-CBC via openssl) before committing it to the orphan `state`
branch — the same pattern used in LDR-Automation-Clean.

GitHub rejects files over 100 MB, so RAW PARCEL RECORDS ARE NEVER STORED
in the committed DB. Parcels are re-downloadable from the release mirror
every run, so they live in an EPHEMERAL cache DB
(data/parcels_cache.sqlite3, gitignored, rebuilt each weekly run) that is
ATTACHed to the main connection as schema `pc` — cross-DB joins work
transparently. Committed state stays a few MB even at 5+ counties.

Committed tables (must survive between runs):
  leads             — scored leads (qualified only — see scoring.py)
  skip_traces       — cached skip-trace results (never pay to trace twice)
  divorce_cases     — divorce filings (stubbed module)
  deed_dates        — deed/purchase dates for the tenure signal
  runs              — run log for the weekly digest
  owners_first_seen — compact (county, prop_id, owner_hash, first_seen)
                      for absentee parcels: tenure proxy + owner-change
                      detection across runs
  owner_history     — owner-change audit trail (event log, tiny)
  exempt_parcels    — compact snapshot of parcels with exemptions, for
                      homestead-removed diffing between runs

Ephemeral table (attached schema `pc`, rebuilt every run):
  pc.parcels        — full parcel snapshot for the current run
"""
import datetime as dt
import json
import logging
import sqlite3
from pathlib import Path

from . import config

LOGGER = logging.getLogger("state")

SCHEMA = """
CREATE TABLE IF NOT EXISTS owner_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    county          TEXT NOT NULL,
    prop_id         TEXT NOT NULL,
    owner_name      TEXT,
    observed_at     TEXT
);

CREATE TABLE IF NOT EXISTS leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    county          TEXT NOT NULL,
    prop_id         TEXT NOT NULL,
    owner_name      TEXT,
    property_addr   TEXT,
    mail_addr       TEXT,
    score           INTEGER DEFAULT 0,
    signals         TEXT,               -- JSON list of signal dicts
    primary_source  TEXT,               -- absentee | divorce | preforeclosure
    status          TEXT DEFAULT 'new', -- new|qualified|traced|awaiting_approval|pushed|skipped
    skip_trace_id   INTEGER,
    fub_person_id   TEXT,
    notes           TEXT,
    created_at      TEXT,
    updated_at      TEXT,
    UNIQUE (county, prop_id)
);

CREATE TABLE IF NOT EXISTS skip_traces (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_key       TEXT UNIQUE,        -- normalized owner name + mail zip
    provider        TEXT,
    matched         INTEGER DEFAULT 0,
    emails          TEXT,               -- JSON list
    phones          TEXT,               -- JSON list of dicts
    dnc             INTEGER DEFAULT 0,
    litigator       INTEGER DEFAULT 0,
    raw             TEXT,               -- full JSON response for the person
    traced_at       TEXT
);

CREATE TABLE IF NOT EXISTS divorce_cases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    case_number     TEXT UNIQUE,
    county          TEXT,
    filed_date      TEXT,
    party_names     TEXT,               -- JSON list
    matched_prop_id TEXT,
    matched_county  TEXT,
    match_confidence REAL,
    created_at      TEXT
);

CREATE TABLE IF NOT EXISTS deed_dates (
    county          TEXT NOT NULL,
    prop_id         TEXT NOT NULL,
    deed_date       TEXT,               -- ISO YYYY-MM-DD (most recent conveyance)
    source          TEXT,               -- e.g. bcad_export | manual_csv
    imported_at     TEXT,
    PRIMARY KEY (county, prop_id)
);

CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_type        TEXT,               -- weekly_pull | push_approved | digest
    started_at      TEXT,
    finished_at     TEXT,
    stats           TEXT                -- JSON
);

CREATE TABLE IF NOT EXISTS owners_first_seen (
    county          TEXT NOT NULL,
    prop_id         TEXT NOT NULL,
    owner_hash      INTEGER,            -- 63-bit hash of normalized owner name
    first_seen_at   TEXT,               -- ISO date this owner was first observed
    PRIMARY KEY (county, prop_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS exempt_parcels (
    county          TEXT NOT NULL,
    prop_id         TEXT NOT NULL,
    exempts         TEXT,               -- current exemption codes, e.g. "HS-OV65"
    last_seen_at    TEXT,
    PRIMARY KEY (county, prop_id)
) WITHOUT ROWID;

-- Warm tier: leads scoring warm_tier_min..threshold-1 (absentee-only, etc).
-- Deliberately COMPACT (no names/addresses — joined from pc.parcels at
-- runtime) so 100K+ warm rows stay a few MB in the committed DB. NEVER
-- traced or pushed; auto-promote to `leads` when new signals lift the score.
CREATE TABLE IF NOT EXISTS warm_leads (
    county          TEXT NOT NULL,
    prop_id         TEXT NOT NULL,
    score           INTEGER DEFAULT 0,
    signals         TEXT,               -- compact JSON list of signal names
    updated_at      TEXT,
    PRIMARY KEY (county, prop_id)
) WITHOUT ROWID;

-- Parcel snapshot bookkeeping: mirror release asset key per county, so
-- daily runs can skip the expensive owner-change bookkeeping when the
-- mirror data hasn't changed (light sync).
CREATE TABLE IF NOT EXISTS parcel_snapshot_meta (
    county          TEXT PRIMARY KEY,
    asset_key       TEXT,               -- "{release_asset_id}:{size}"
    synced_at       TEXT,
    kept            INTEGER DEFAULT 0,
    absentee        INTEGER DEFAULT 0
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_leads_status ON leads (status);
CREATE INDEX IF NOT EXISTS idx_leads_score ON leads (score);
"""

# Ephemeral parcel cache — rebuilt from the mirror download every run.
PARCELS_SCHEMA = """
CREATE TABLE IF NOT EXISTS pc.parcels (
    county          TEXT NOT NULL,
    prop_id         TEXT NOT NULL,
    owner_name      TEXT,
    situs_addr      TEXT,
    situs_city      TEXT,
    situs_zip       TEXT,
    mail_addr       TEXT,
    mail_city       TEXT,
    mail_state      TEXT,
    mail_zip        TEXT,
    mkt_value       REAL,
    tax_year        INTEGER,
    is_absentee     INTEGER DEFAULT 0,
    PRIMARY KEY (county, prop_id)
);
CREATE INDEX IF NOT EXISTS pc.idx_parcels_absentee ON parcels (is_absentee);
"""


def now_iso() -> str:
    return dt.datetime.now(config.CT).isoformat(timespec="seconds")


def get_db(path: Path | None = None, parcels_cache: Path | None = None,
           fresh_parcels: bool = False) -> sqlite3.Connection:
    """Open (and initialize) the committed state DB + attached parcel cache.

    parcels_cache : path of the ephemeral cache DB (default alongside the
                    main DB as parcels_cache.sqlite3; ":memory:" works too).
    fresh_parcels : drop any existing cached parcels (weekly runs rebuild
                    the snapshot from the mirror download).
    """
    db_path = Path(path or config.DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate_legacy(conn)
    _migrate_versioned(conn)

    cache = str(parcels_cache) if parcels_cache is not None \
        else str(db_path.parent / "parcels_cache.sqlite3")
    conn.execute("ATTACH DATABASE ? AS pc", (cache,))
    if fresh_parcels:
        conn.execute("DROP TABLE IF EXISTS pc.parcels")
    conn.executescript(PARCELS_SCHEMA)
    return conn


def _migrate_legacy(conn: sqlite3.Connection) -> None:
    """One-time cleanup of pre-split state DBs (parcels used to be committed).

    The old committed DB may contain a full `parcels` table (the 100MB+
    problem). Salvage the compact derived attributes it holds — exemption
    snapshot + absentee first-seen — then drop it and VACUUM.
    """
    has_parcels = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='parcels'"
    ).fetchone()
    if not has_parcels:
        return
    LOGGER.info("Migrating legacy state DB: extracting compact attributes, dropping parcels table")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(parcels)")}
    if {"exempts", "county", "prop_id"} <= cols:
        conn.execute(
            """INSERT OR IGNORE INTO exempt_parcels (county, prop_id, exempts, last_seen_at)
               SELECT county, prop_id, exempts, COALESCE(last_seen_at, '')
               FROM parcels WHERE exempts IS NOT NULL AND exempts <> ''""")
    if {"is_absentee", "owner_name", "first_seen_at"} <= cols:
        for row in conn.execute(
            "SELECT county, prop_id, owner_name, first_seen_at FROM parcels WHERE is_absentee=1"
        ).fetchall():
            conn.execute(
                "INSERT OR IGNORE INTO owners_first_seen (county, prop_id, owner_hash, first_seen_at) "
                "VALUES (?,?,?,?)",
                (row["county"], row["prop_id"], owner_hash(row["owner_name"]),
                 row["first_seen_at"] or now_iso()))
    conn.execute("DROP TABLE parcels")
    conn.execute("DROP INDEX IF EXISTS idx_parcels_absentee")
    # Purge unqualified 'new' leads (score<threshold) — they are recomputed
    # from the parcel cache every run and only bloat the committed file.
    conn.execute("DELETE FROM leads WHERE status='new' AND score < ?",
                 (config.SCORE_THRESHOLD,))
    conn.commit()
    conn.execute("VACUUM")
    LOGGER.info("Legacy migration complete")


def _migrate_versioned(conn: sqlite3.Connection) -> None:
    """Versioned one-time migrations tracked via PRAGMA user_version."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]

    if version < 2:
        # v2 (2026-07): a BatchData 403 (token missing the skip-trace endpoint
        # permission) was recorded as 64 "completed" no-match traces, which
        # permanently blocked re-tracing those owners. Purge ALL cached
        # no-match entries (matched results are untouched — already paid for)
        # and requeue the affected leads so the next run re-traces them.
        stale = [r["id"] for r in conn.execute(
            "SELECT id FROM skip_traces WHERE matched=0")]
        if stale:
            ph = ",".join("?" * len(stale))
            requeued = conn.execute(
                f"UPDATE leads SET status='qualified', skip_trace_id=NULL, "
                f"updated_at=? WHERE skip_trace_id IN ({ph}) "
                f"AND status IN ('traced','held_no_contact')",
                (now_iso(), *stale)).rowcount
            conn.execute(f"DELETE FROM skip_traces WHERE id IN ({ph})", stale)
            LOGGER.info(
                "Migration v2: purged %d cached no-match skip-trace entries "
                "(poisoned by the 403 error bug) and requeued %d leads for "
                "re-tracing", len(stale), requeued)
        conn.execute("PRAGMA user_version = 2")
        conn.commit()


def owner_hash(owner_name: str) -> int:
    """Stable 63-bit hash of a normalized owner name (compact owner identity)."""
    import hashlib
    norm = " ".join((owner_name or "").upper().split())
    return int.from_bytes(hashlib.sha1(norm.encode()).digest()[:8], "big") >> 1


def check_state_size(db_path: Path | None = None, limit_mb: int = 90) -> float:
    """Return committed DB size in MB; raise if it approaches GitHub's 100MB cap."""
    p = Path(db_path or config.DB_PATH)
    size_mb = p.stat().st_size / 1e6 if p.exists() else 0.0
    if size_mb > limit_mb:
        raise RuntimeError(
            f"Committed state DB is {size_mb:.1f} MB (> {limit_mb} MB guard) — "
            "it would exceed GitHub's 100 MB file limit. Investigate table growth "
            "before the state push corrupts the branch.")
    LOGGER.info("Committed state DB size: %.1f MB", size_mb)
    return size_mb


def record_run(conn: sqlite3.Connection, run_type: str, started_at: str, stats: dict) -> None:
    conn.execute(
        "INSERT INTO runs (run_type, started_at, finished_at, stats) VALUES (?,?,?,?)",
        (run_type, started_at, now_iso(), json.dumps(stats)),
    )
    conn.commit()


def owner_key(owner_name: str, mail_zip: str) -> str:
    """Stable key used to dedupe skip traces per owner."""
    return f"{' '.join((owner_name or '').upper().split())}|{(mail_zip or '').strip()[:5]}"

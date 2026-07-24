"""Encrypted SQLite state management.

The DB file itself is plain SQLite while a workflow is running; the
state-sync composite action encrypts it (AES-256-CBC via openssl) before
committing it to the orphan `state` branch — the same pattern used in
LDR-Automation-Clean.

Tables:
  parcels        — latest snapshot of every parcel we've seen (per county)
  owner_history  — owner-change audit trail (proxy for purchase date)
  leads          — scored leads, lifecycle: new → qualified → traced → approved → pushed
  skip_traces    — cached skip-trace results so an owner is never traced twice
  divorce_cases  — divorce filings pulled by the (stubbed) divorce module
  runs           — run log for the weekly digest
"""
import datetime as dt
import json
import logging
import sqlite3
from pathlib import Path

from . import config

LOGGER = logging.getLogger("state")

SCHEMA = """
CREATE TABLE IF NOT EXISTS parcels (
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
    exempts         TEXT,
    is_absentee     INTEGER DEFAULT 0,
    first_seen_at   TEXT,
    last_seen_at    TEXT,
    PRIMARY KEY (county, prop_id)
);

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

CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_type        TEXT,               -- weekly_pull | push_approved | digest
    started_at      TEXT,
    finished_at     TEXT,
    stats           TEXT                -- JSON
);

CREATE INDEX IF NOT EXISTS idx_leads_status ON leads (status);
CREATE INDEX IF NOT EXISTS idx_leads_score ON leads (score);
CREATE INDEX IF NOT EXISTS idx_parcels_absentee ON parcels (is_absentee);
"""


def now_iso() -> str:
    return dt.datetime.now(config.CT).isoformat(timespec="seconds")


def get_db(path: Path | None = None) -> sqlite3.Connection:
    """Open (and initialize) the state database."""
    db_path = Path(path or config.DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def record_run(conn: sqlite3.Connection, run_type: str, started_at: str, stats: dict) -> None:
    conn.execute(
        "INSERT INTO runs (run_type, started_at, finished_at, stats) VALUES (?,?,?,?)",
        (run_type, started_at, now_iso(), json.dumps(stats)),
    )
    conn.commit()


def owner_key(owner_name: str, mail_zip: str) -> str:
    """Stable key used to dedupe skip traces per owner."""
    return f"{' '.join((owner_name or '').upper().split())}|{(mail_zip or '').strip()[:5]}"

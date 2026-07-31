"""Core pipeline tests — run with: python3 -m pytest tests/ -v"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from seller_finder.state import get_db, owner_key, owner_hash, now_iso
from seller_finder.sources.parcels import is_absentee, is_individual_owner, normalize_addr
from seller_finder.sources.preforeclosure import _addr_key
from seller_finder.scoring import compute_scores
from seller_finder import config


@pytest.fixture
def db(tmp_path):
    conn = get_db(tmp_path / "test.sqlite3", parcels_cache=tmp_path / "cache.sqlite3")
    yield conn
    conn.close()


def _insert_parcel(conn, county="bexar", prop_id="1001", owner="SMITH JOHN",
                   situs="123 MAIN ST", situs_zip="78201", mail="500 OTHER RD",
                   mail_zip="78209", absentee=1, first_seen=None):
    conn.execute(
        """INSERT OR REPLACE INTO pc.parcels
           (county, prop_id, owner_name, situs_addr, situs_city, situs_zip,
            mail_addr, mail_city, mail_state, mail_zip, mkt_value, tax_year,
            is_absentee)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (county, prop_id, owner, situs, "SAN ANTONIO", situs_zip, mail,
         "SAN ANTONIO", "TX", mail_zip, 250000, 2025, absentee),
    )
    if absentee:
        conn.execute(
            "INSERT OR REPLACE INTO owners_first_seen (county, prop_id, owner_hash, first_seen_at) "
            "VALUES (?,?,?,?)",
            (county, prop_id, owner_hash(owner), first_seen or now_iso()))
    conn.commit()


# ── Absentee detection ───────────────────────────────────────────────────

def test_absentee_different_zip():
    assert is_absentee("123 MAIN ST", "78201", "999 ELSEWHERE AVE", "78209")

def test_not_absentee_same_address():
    assert not is_absentee("123 MAIN ST", "78201", "123 MAIN STREET", "78201")

def test_not_absentee_missing_mail():
    assert not is_absentee("123 MAIN ST", "78201", "", "")

def test_not_absentee_degenerate_situs():
    """Travis publishes ', TX 78704' situs for most rows — no street number
    means no comparison, never absentee (prevents county-wide false positives)."""
    assert not is_absentee(", TX 78704", "78704", "999 ELSEWHERE AVE", "78209")
    assert not is_absentee(", TX", "", "PO BOX 32368", "78764")

def test_normalize_addr_abbreviations():
    assert normalize_addr("123 North Main Street") == normalize_addr("123 N MAIN ST")


# ── Owner filtering ──────────────────────────────────────────────────────

def test_institutional_owner_filtered():
    assert not is_individual_owner("ACME HOLDINGS LLC")
    assert not is_individual_owner("CITY OF SAN ANTONIO")
    assert not is_individual_owner("FIRST BAPTIST CHURCH")

def test_individual_owner_kept():
    assert is_individual_owner("SMITH JOHN A")
    assert is_individual_owner("GARCIA MARIA & JOSE")


# ── Foreclosure address matching ─────────────────────────────────────────

def test_addr_key_match():
    assert _addr_key("123 MAIN ST", "78201") == _addr_key("123 MAIN STREET", "78201-1234")


# ── Scoring ──────────────────────────────────────────────────────────────

def test_absentee_only_scores_30_not_persisted(db):
    """Absentee-only (30 < 40) is counted as a candidate but NOT stored —
    sub-threshold leads are recomputed from the parcel cache every run, so
    persisting them would bloat the committed state DB past GitHub's cap."""
    _insert_parcel(db, prop_id="2001", absentee=1)
    stats = compute_scores(db, [], [], {})
    lead = db.execute("SELECT * FROM leads WHERE prop_id='2001'").fetchone()
    assert lead is None
    assert stats["candidates"] == 1
    assert stats["below_threshold"] == 1
    assert stats["leads_created"] == 0
    assert stats["qualified"] == 0

def test_absentee_plus_foreclosure_qualifies(db):
    _insert_parcel(db, prop_id="2002", absentee=1)
    fc = [{"county": "bexar", "prop_id": "2002", "kind": "mortgage",
           "doc_number": "20260800001", "month": 8, "year": 2026}]
    compute_scores(db, fc, [], {})
    lead = db.execute("SELECT * FROM leads WHERE prop_id='2002'").fetchone()
    assert lead["score"] == config.SCORE_ABSENTEE + config.SCORE_PREFORECLOSURE  # 60
    assert lead["status"] == "qualified"
    assert lead["primary_source"] == "preforeclosure"

def test_divorce_match_scoring(db):
    _insert_parcel(db, prop_id="2003", absentee=1)
    dv = [{"county": "bexar", "prop_id": "2003", "case_number": "2026CI00123",
           "owner_name": "SMITH JOHN", "confidence": 0.92}]
    compute_scores(db, [], dv, {})
    lead = db.execute("SELECT * FROM leads WHERE prop_id='2003'").fetchone()
    assert lead["score"] == config.SCORE_ABSENTEE + config.SCORE_DIVORCE  # 55
    assert lead["primary_source"] == "divorce"

def test_homestead_removed_scoring(db):
    _insert_parcel(db, prop_id="2004", absentee=1)
    compute_scores(db, [], [], {"bexar": ["2004"]})
    lead = db.execute("SELECT * FROM leads WHERE prop_id='2004'").fetchone()
    assert lead["score"] == config.SCORE_ABSENTEE + config.SCORE_HOMESTEAD_REMOVED  # 40
    assert lead["status"] == "qualified"

def test_signals_stored_as_json(db):
    _insert_parcel(db, prop_id="2005", absentee=1)
    fc = [{"county": "bexar", "prop_id": "2005", "kind": "mortgage",
           "doc_number": "20260800009", "month": 8, "year": 2026}]
    compute_scores(db, fc, [], {})
    lead = db.execute("SELECT signals FROM leads WHERE prop_id='2005'").fetchone()
    signals = json.loads(lead["signals"])
    assert {s["signal"] for s in signals} == {"absentee_owner", "preforeclosure"}

def test_rescore_carries_forward_event_signals(db):
    """A preforeclosure lead must stay qualified on the next weekly run even
    if the notice has rotated out of the current-month feed."""
    _insert_parcel(db, prop_id="2007", absentee=1)
    fc = [{"county": "bexar", "prop_id": "2007", "kind": "mortgage",
           "doc_number": "20260800002", "month": 8, "year": 2026}]
    compute_scores(db, fc, [], {})
    # Next weekly run: no foreclosure matches passed in
    compute_scores(db, [], [], {})
    lead = db.execute("SELECT * FROM leads WHERE prop_id='2007'").fetchone()
    assert lead["score"] == config.SCORE_ABSENTEE + config.SCORE_PREFORECLOSURE
    assert lead["status"] == "qualified"
    assert lead["primary_source"] == "preforeclosure"


def test_rescore_does_not_demote_pushed(db):
    _insert_parcel(db, prop_id="2006", absentee=1)
    compute_scores(db, [{"county": "bexar", "prop_id": "2006", "kind": "mortgage",
                         "doc_number": "1", "month": 8, "year": 2026}], [], {})
    db.execute("UPDATE leads SET status='pushed' WHERE prop_id='2006'")
    db.commit()
    compute_scores(db, [], [], {})
    lead = db.execute("SELECT status FROM leads WHERE prop_id='2006'").fetchone()
    assert lead["status"] == "pushed"


# ── Skip-trace dedupe ────────────────────────────────────────────────────

def test_owner_key_normalization():
    assert owner_key("SMITH  JOHN", "78209") == owner_key("smith john", "78209-1234")

def test_skip_trace_cache_prevents_double_billing(db, monkeypatch):
    from seller_finder.skiptrace import tracer

    _insert_parcel(db, prop_id="3001", absentee=1)
    _insert_parcel(db, prop_id="3002", absentee=1, situs="777 PINE RD", situs_zip="78210")
    fc = [{"county": "bexar", "prop_id": p, "kind": "mortgage",
           "doc_number": "1", "month": 8, "year": 2026} for p in ("3001", "3002")]
    compute_scores(db, fc, [], {})

    calls = {"n": 0}

    class FakeProvider:
        name = "fake"
        def trace_batch(self, reqs):
            calls["n"] += len(reqs)
            from seller_finder.skiptrace.base import SkipTraceResult
            return [SkipTraceResult(matched=True, emails=["x@y.com"], phones=[],
                                    provider="fake") for _ in reqs]

    monkeypatch.setattr(tracer, "get_provider", lambda name="x": FakeProvider())
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "BATCHDATA_API_KEY", "fake-key-for-test")

    stats1 = tracer.trace_qualified_leads(db)
    assert stats1["traced"] == 1  # same owner both parcels → one billable trace
    # `or` used to let either half carry the assertion, so a regression in one
    # passed silently. Both must hold: exactly one request left the process,
    # and the second lead was satisfied from the within-run cache.
    assert calls["n"] == 1, "the same owner must be sent to the provider once"
    assert stats1["cached"] == 1
    # ...and BOTH leads end up attached to that single paid trace.
    rows = db.execute(
        "SELECT prop_id, status, skip_trace_id FROM leads ORDER BY prop_id").fetchall()
    assert [r["status"] for r in rows] == ["traced", "traced"]
    assert rows[0]["skip_trace_id"] == rows[1]["skip_trace_id"] is not None


# ── Skip-trace error handling (errors are NOT no-matches) ────────────

def test_api_error_not_cached_lead_stays_qualified(db, monkeypatch):
    """An API error (e.g. 403) must NOT create a cache entry or mark the lead
    traced — the lead stays 'qualified' and is retried next run."""
    from seller_finder.skiptrace import tracer
    from seller_finder.skiptrace.base import SkipTraceResult

    _insert_parcel(db, prop_id="6001", absentee=1)
    compute_scores(db, [{"county": "bexar", "prop_id": "6001", "kind": "mortgage",
                         "doc_number": "1", "month": 8, "year": 2026}], [], {})

    class ErrorProvider:
        name = "err"
        def trace_batch(self, reqs):
            return [SkipTraceResult(provider="err",
                                    error="HTTP 403: Forbidden") for _ in reqs]

    monkeypatch.setattr(tracer, "get_provider", lambda name="x": ErrorProvider())
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "BATCHDATA_API_KEY", "fake-key")

    stats = tracer.trace_qualified_leads(db)
    assert stats["errors"] == 1
    assert stats["traced"] == 0
    assert "403" in stats["top_error"]
    # No poisoned cache entry:
    assert db.execute("SELECT COUNT(*) c FROM skip_traces").fetchone()["c"] == 0
    # Lead is still qualified → retried automatically next run:
    lead = db.execute("SELECT status, skip_trace_id FROM leads WHERE prop_id='6001'").fetchone()
    assert lead["status"] == "qualified" and lead["skip_trace_id"] is None


def test_batchdata_403_returns_error_results(monkeypatch):
    """Provider surfaces raw status+body as error on 403 (no retry for 4xx)."""
    from seller_finder.skiptrace import batchdata
    from seller_finder.skiptrace.base import SkipTraceRequest

    class FakeResp:
        status_code = 403
        text = '{"status":{"code":403,"text":"Forbidden"}}'

    posts = {"n": 0}
    def fake_post(*a, **k):
        posts["n"] += 1
        return FakeResp()
    monkeypatch.setattr(batchdata.requests, "post", fake_post)

    p = batchdata.BatchDataProvider(api_key="k")
    out = p.trace_batch([SkipTraceRequest(street="1 A ST", city="SA", state="TX", zip="78201")])
    assert posts["n"] == 1  # 403 is permanent — no pointless retries
    assert out[0].error and "403" in out[0].error and "Forbidden" in out[0].error
    assert out[0].matched is False


def test_batchdata_429_retries_then_succeeds(monkeypatch):
    from seller_finder.skiptrace import batchdata
    from seller_finder.skiptrace.base import SkipTraceRequest

    class R429:
        status_code = 429
        text = "rate limited"
    class R200:
        status_code = 200
        text = "{}"
        @staticmethod
        def json():
            return {"results": {"persons": [], "meta": {"results": {
                "matchCount": 0, "noMatchCount": 1, "errorCount": 0}}}}

    seq = [R429(), R200()]
    monkeypatch.setattr(batchdata.requests, "post", lambda *a, **k: seq.pop(0))
    monkeypatch.setattr(batchdata.time, "sleep", lambda s: None)

    p = batchdata.BatchDataProvider(api_key="k")
    out = p.trace_batch([SkipTraceRequest(street="1 A ST", city="SA", state="TX", zip="78201")])
    assert out[0].error is None  # succeeded after retry
    assert out[0].matched is False  # genuine no-match


def test_no_match_cache_expires_and_requeues(db, monkeypatch):
    """Cached no-match entries older than no_match_retrace_days expire and the
    lead re-enters the tracing queue."""
    from seller_finder.skiptrace import tracer
    from seller_finder.skiptrace.base import SkipTraceResult

    _insert_parcel(db, prop_id="6101", absentee=1)
    compute_scores(db, [{"county": "bexar", "prop_id": "6101", "kind": "mortgage",
                         "doc_number": "1", "month": 8, "year": 2026}], [], {})
    # Seed an OLD no-match cache entry attached to the lead.
    db.execute(
        "INSERT INTO skip_traces (owner_key, provider, matched, emails, phones, "
        "dnc, litigator, raw, traced_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (owner_key("SMITH JOHN", "78209"), "batchdata", 0, "[]", "[]", 0, 0,
         "null", "2026-01-01T00:00:00"))
    tid = db.execute("SELECT id FROM skip_traces").fetchone()["id"]
    db.execute("UPDATE leads SET status='held_no_contact', skip_trace_id=? "
               "WHERE prop_id='6101'", (tid,))
    db.commit()

    class MatchProvider:
        name = "m"
        def trace_batch(self, reqs):
            return [SkipTraceResult(matched=True, emails=["a@b.com"],
                                    provider="m") for _ in reqs]

    monkeypatch.setattr(tracer, "get_provider", lambda name="x": MatchProvider())
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "BATCHDATA_API_KEY", "fake-key")

    stats = tracer.trace_qualified_leads(db)
    # Old no-match expired → lead requeued → re-traced → matched this time.
    assert stats["matched"] == 1
    lead = db.execute("SELECT status FROM leads WHERE prop_id='6101'").fetchone()
    assert lead["status"] == "traced"
    assert db.execute("SELECT COUNT(*) c FROM skip_traces WHERE matched=0").fetchone()["c"] == 0


def test_migration_v2_purges_poisoned_no_matches(tmp_path):
    """Opening a pre-v2 DB deletes matched=0 cache rows and requeues leads."""
    import sqlite3
    db_file = tmp_path / "old.sqlite3"
    conn = get_db(db_file, parcels_cache=tmp_path / "c1.sqlite3")
    # Simulate the poisoned state from the 403 run: no-match cache + traced lead.
    conn.execute(
        "INSERT INTO skip_traces (owner_key, provider, matched, emails, phones, "
        "dnc, litigator, raw, traced_at) VALUES ('K|1','batchdata',0,'[]','[]',0,0,'null',?)",
        (now_iso(),))
    tid = conn.execute("SELECT id FROM skip_traces").fetchone()["id"]
    conn.execute(
        "INSERT INTO leads (county, prop_id, owner_name, property_addr, mail_addr, "
        "score, signals, status, skip_trace_id, created_at, updated_at) "
        "VALUES ('bexar','9001','X','1 A ST','2 B ST',60,'{}','held_no_contact',?,?,?)",
        (tid, now_iso(), now_iso()))
    conn.execute("PRAGMA user_version = 0")  # pretend pre-v2
    conn.commit()
    conn.close()

    conn2 = get_db(db_file, parcels_cache=tmp_path / "c2.sqlite3")
    assert conn2.execute("SELECT COUNT(*) c FROM skip_traces WHERE matched=0").fetchone()["c"] == 0
    lead = conn2.execute("SELECT status, skip_trace_id FROM leads WHERE prop_id='9001'").fetchone()
    assert lead["status"] == "qualified" and lead["skip_trace_id"] is None
    # v2 ran; the DB is carried forward to the current schema version (>=3
    # adds the inbox ingest ledger).
    assert conn2.execute("PRAGMA user_version").fetchone()[0] >= 2
    conn2.close()


def test_migrations_are_versioned_and_rerunnable(tmp_path):
    """Reopening the same DB must be a no-op: user_version stops re-running the
    one-time migrations, and a legitimately cached matched trace survives."""
    db_file = tmp_path / "m.sqlite3"
    conn = get_db(db_file, parcels_cache=tmp_path / "c1.sqlite3")
    version_after_first = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.execute(
        "INSERT INTO skip_traces (owner_key, provider, matched, emails, phones, "
        "dnc, litigator, raw, traced_at) VALUES ('PAID|78201','batchdata',1,'[]','[]',0,0,'null',?)",
        (now_iso(),))
    conn.commit()
    conn.close()

    for _ in range(3):  # re-runnable: opening again changes nothing
        c = get_db(db_file, parcels_cache=tmp_path / "c2.sqlite3")
        assert c.execute("PRAGMA user_version").fetchone()[0] == version_after_first
        # matched (already paid for) traces are never purged by any migration
        assert c.execute(
            "SELECT COUNT(*) c FROM skip_traces WHERE matched=1").fetchone()["c"] == 1
        c.close()


def test_diagnostics_show_trace_outcomes():
    from seller_finder.review import _diagnostics_md
    md = "\n".join(_diagnostics_md({"skiptrace": {
        "eligible": 65, "cached": 1, "traced": 0, "matched": 0, "no_match": 0,
        "errors": 64, "top_error": "HTTP 403: Forbidden", "skipped_no_api_key": 0}}))
    assert "API errors" in md and "64" in md
    assert "HTTP 403: Forbidden" in md


# ── Optional secrets (graceful degradation) ────────────────────────────

def test_no_batchdata_key_still_advances_leads(db, monkeypatch):
    """Without BATCHDATA_API_KEY, qualified leads must still reach review."""
    from seller_finder.skiptrace import tracer
    from seller_finder import review

    _insert_parcel(db, prop_id="5001", absentee=1)
    compute_scores(db, [{"county": "bexar", "prop_id": "5001", "kind": "mortgage",
                         "doc_number": "1", "month": 8, "year": 2026}], [], {})
    monkeypatch.setattr(config, "BATCHDATA_API_KEY", "")
    monkeypatch.setattr(config, "DRY_RUN", False)
    stats = tracer.trace_qualified_leads(db)
    assert stats["skipped_no_api_key"] == 1
    lead = db.execute("SELECT status FROM leads WHERE prop_id='5001'").fetchone()
    assert lead["status"] == "traced"
    assert review.stage_traced_leads(db) == 1


def test_no_smtp_skips_digest_without_error(db, monkeypatch):
    from seller_finder import review
    monkeypatch.setattr(config, "SMTP_USER", "")
    monkeypatch.setattr(config, "SMTP_PASSWORD", "")
    monkeypatch.setattr(config, "DRY_RUN", False)
    assert review.send_digest_email(db) is False  # skipped, no exception


def test_no_healthcheck_url_skips_ping(monkeypatch):
    from seller_finder import health
    monkeypatch.setattr(config, "HEALTHCHECK_URL", "")
    health.ping_healthcheck()  # must not raise


# ── Deed dates / tenure signal ────────────────────────────────────────────

def test_deed_date_parsing():
    from seller_finder.sources.deeds import parse_deed_date
    assert parse_deed_date("2012-05-01") == "2012-05-01"
    assert parse_deed_date("5/1/2012") == "2012-05-01"
    assert parse_deed_date("20120501") == "2012-05-01"
    assert parse_deed_date("2012") == "2012-07-01"
    assert parse_deed_date("") is None
    assert parse_deed_date("not a date") is None


def test_deed_inbox_ingest_and_tenure_scoring(db, tmp_path, monkeypatch):
    from seller_finder.sources import deeds

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "deeds_bexar.csv").write_text(
        "county,prop_id,deed_date\nbexar,6001,2009-03-15\nbexar,6002,not-a-date\n"
    )
    stats = deeds.ingest_inbox(db)
    assert stats["files"] == 1 and stats["rows"] == 1
    # The CSV STAYS in the repo (it is committed — that is how it reaches the
    # Actions runner). Exactly-once comes from the ingest ledger, not a rename.
    assert (inbox / "deeds_bexar.csv").exists()
    assert db.execute(
        "SELECT COUNT(*) c FROM ingested_files WHERE kind='deeds'").fetchone()["c"] == 1
    # Re-running must not re-import
    stats2 = deeds.ingest_inbox(db)
    assert stats2["files"] == 0
    # A corrected re-upload under the same name IS re-ingested (content differs)
    (inbox / "deeds_bexar.csv").write_text(
        "county,prop_id,deed_date\nbexar,6001,2009-03-15\nbexar,6002,2010-01-01\n")
    stats3 = deeds.ingest_inbox(db)
    assert stats3["files"] == 1 and stats3["rows"] == 2

    # Tenure signal: absentee (30) + owned 10+ years (20) = 50 → qualified
    _insert_parcel(db, prop_id="6001", absentee=1)
    compute_scores(db, [], [], {})
    lead = db.execute("SELECT * FROM leads WHERE prop_id='6001'").fetchone()
    assert lead["score"] == config.SCORE_ABSENTEE + config.SCORE_LONG_OWNERSHIP
    assert lead["status"] == "qualified"
    signals = [s["signal"] for s in json.loads(lead["signals"])]
    assert "owned_10_plus_years" in signals


def test_recent_deed_does_not_add_tenure(db):
    db.execute("INSERT OR REPLACE INTO deed_dates (county, prop_id, deed_date, source, imported_at) "
               "VALUES ('bexar','6003','2025-06-01','test',?)", (now_iso(),))
    db.commit()
    _insert_parcel(db, prop_id="6003", absentee=1)
    stats = compute_scores(db, [], [], {})
    # absentee(30) + recent deed (no tenure bump) = 30 → below threshold → not stored
    lead = db.execute("SELECT score FROM leads WHERE prop_id='6003'").fetchone()
    assert lead is None
    assert stats["below_threshold"] == 1


# ── State DB split / migration / size guard ────────────────────────

def test_legacy_migration_drops_parcels_and_salvages_state(tmp_path):
    """Opening a pre-split DB (with a committed parcels table) must salvage
    exemptions + absentee first-seen, drop the heavy table, and purge
    sub-threshold 'new' leads."""
    import sqlite3 as _sq
    legacy = tmp_path / "legacy.sqlite3"
    c = _sq.connect(str(legacy))
    c.executescript("""
        CREATE TABLE parcels (
            county TEXT, prop_id TEXT, owner_name TEXT, situs_addr TEXT,
            situs_city TEXT, situs_zip TEXT, mail_addr TEXT, mail_city TEXT,
            mail_state TEXT, mail_zip TEXT, mkt_value REAL, tax_year INTEGER,
            exempts TEXT, is_absentee INTEGER, first_seen_at TEXT,
            last_seen_at TEXT, PRIMARY KEY (county, prop_id));
        CREATE TABLE leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT, county TEXT, prop_id TEXT,
            owner_name TEXT, property_addr TEXT, mail_addr TEXT,
            score INTEGER DEFAULT 0, signals TEXT, primary_source TEXT,
            status TEXT DEFAULT 'new', skip_trace_id INTEGER,
            fub_person_id TEXT, notes TEXT, created_at TEXT, updated_at TEXT,
            UNIQUE (county, prop_id));
    """)
    c.execute("INSERT INTO parcels VALUES ('bexar','P1','SMITH JOHN','1 A ST','SA','78201',"
              "'9 B RD','SA','TX','78209',100000,2025,'HS',1,'2015-01-01','2026-01-01')")
    c.execute("INSERT INTO parcels VALUES ('bexar','P2','DOE JANE','2 A ST','SA','78201',"
              "'2 A ST','SA','TX','78201',100000,2025,NULL,0,'2020-01-01','2026-01-01')")
    c.execute("INSERT INTO leads (county,prop_id,score,status) VALUES ('bexar','P1',30,'new')")
    c.execute("INSERT INTO leads (county,prop_id,score,status) VALUES ('bexar','P3',60,'qualified')")
    c.commit(); c.close()

    conn = get_db(legacy, parcels_cache=tmp_path / "cache.sqlite3")
    # parcels table dropped from the committed DB
    assert conn.execute("SELECT name FROM main.sqlite_master WHERE name='parcels'").fetchone() is None
    # compact attributes salvaged
    ex = conn.execute("SELECT exempts FROM exempt_parcels WHERE prop_id='P1'").fetchone()
    assert ex["exempts"] == "HS"
    fs = conn.execute("SELECT first_seen_at FROM owners_first_seen WHERE prop_id='P1'").fetchone()
    assert fs["first_seen_at"] == "2015-01-01"
    assert conn.execute("SELECT 1 FROM owners_first_seen WHERE prop_id='P2'").fetchone() is None
    # sub-threshold 'new' leads purged; qualified kept
    assert conn.execute("SELECT 1 FROM leads WHERE prop_id='P1'").fetchone() is None
    assert conn.execute("SELECT 1 FROM leads WHERE prop_id='P3'").fetchone() is not None
    conn.close()


def test_provider_consumer_profile_is_never_stored(db, monkeypatch):
    """skip_traces.raw must hold provenance only, never the provider's
    consumer profile (relatives, address history, DOB). No code reads that
    column, and at up to 100 KB/owner it also threatened the 100 MB cap."""
    from seller_finder.skiptrace import tracer
    from seller_finder.skiptrace.base import SkipTraceResult

    _insert_parcel(db, prop_id="C001", absentee=1)
    compute_scores(db, [{"county": "bexar", "prop_id": "C001", "kind": "mortgage",
                         "doc_number": "1", "month": 8, "year": 2026}], [], {})

    profile = {
        "emails": [{"email": "a@b.com"}],
        "phoneNumbers": [{"number": "2105551234"}],
        "dateOfBirth": "1961-04-02",
        "relatives": [{"name": "SMITH JANE", "age": 58}],
        "addressHistory": [{"street": "9 PRIOR RD", "city": "AUSTIN"}],
    }

    class Prov:
        name = "p"
        def trace_batch(self, reqs):
            return [SkipTraceResult(matched=True, emails=["a@b.com"],
                                    phones=[{"number": "2105551234"}],
                                    raw=profile, provider="p") for _ in reqs]

    monkeypatch.setattr(tracer, "get_provider", lambda name="x": Prov())
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "BATCHDATA_API_KEY", "fake-key")
    tracer.trace_qualified_leads(db)

    stored = db.execute("SELECT raw, emails, phones FROM skip_traces").fetchone()
    for leaked in ("1961-04-02", "SMITH JANE", "9 PRIOR RD", "AUSTIN"):
        assert leaked not in stored["raw"], f"{leaked!r} leaked into skip_traces.raw"
    # The contact info we actually paid for is still there, in its own columns.
    assert json.loads(stored["emails"]) == ["a@b.com"]
    assert json.loads(stored["phones"])[0]["number"] == "2105551234"
    assert json.loads(stored["raw"])["email_count"] == 1


def test_migration_v5_purges_stored_consumer_profiles(tmp_path):
    """An existing state DB carries the old full payloads — purge on open,
    without disturbing the cached match result (already paid for)."""
    db_file = tmp_path / "pii.sqlite3"
    conn = get_db(db_file, parcels_cache=tmp_path / "c1.sqlite3")
    fat = json.dumps({"dateOfBirth": "1961-04-02",
                      "relatives": [{"name": "SMITH JANE"}] * 50,
                      "addressHistory": [{"street": "9 PRIOR RD"}] * 50})
    conn.execute(
        "INSERT INTO skip_traces (owner_key, provider, matched, emails, phones, "
        "dnc, litigator, raw, traced_at) VALUES ('K|78201','batchdata',1,"
        "'[\"a@b.com\"]','[]',0,0,?,?)", (fat, now_iso()))
    conn.execute("PRAGMA user_version = 4")  # pretend pre-v5
    conn.commit()
    conn.close()

    conn2 = get_db(db_file, parcels_cache=tmp_path / "c2.sqlite3")
    row = conn2.execute("SELECT raw, matched, emails FROM skip_traces").fetchone()
    assert "1961-04-02" not in row["raw"] and "SMITH JANE" not in row["raw"]
    assert json.loads(row["raw"])["purged_by_migration"] == "v5"
    # The billable facts survive — nobody gets re-traced or re-billed.
    assert row["matched"] == 1 and json.loads(row["emails"]) == ["a@b.com"]
    conn2.close()


def test_state_size_guard(tmp_path, monkeypatch):
    from seller_finder.state import check_state_size
    small = tmp_path / "small.db"
    small.write_bytes(b"x" * 1000)
    assert check_state_size(small) < 1
    big = tmp_path / "big.db"
    big.write_bytes(b"x" * 95_000_000)
    with pytest.raises(RuntimeError, match="100 MB"):
        check_state_size(big, limit_mb=90)


def test_summary_counts_held_leads_in_buckets(db):
    """Regression: held_no_contact leads must appear in score bands/by-source
    (a run without a BatchData key previously reported 0 everywhere)."""
    from seller_finder.review import _summary_stats
    _insert_parcel(db, prop_id="9001", absentee=1)
    fc = [{"county": "bexar", "prop_id": "9001", "kind": "mortgage",
           "doc_number": "20260800077", "month": 8, "year": 2026}]
    compute_scores(db, fc, [], {})
    db.execute("UPDATE leads SET status='held_no_contact', updated_at=? WHERE prop_id='9001'",
               (now_iso(),))
    db.commit()
    stats = _summary_stats(db)
    assert stats["score_buckets"]["55-69"] == 1
    assert stats["by_source"].get("preforeclosure") == 1
    assert "absentee_owner + preforeclosure" in stats["by_signal_combo"]


# ── Parcel download mirror ────────────────────────────────────────────

def test_github_token_from_env(monkeypatch):
    from seller_finder.sources import parcels
    monkeypatch.setenv("GITHUB_TOKEN", "tok-abc")
    assert parcels._github_token() == "tok-abc"


def test_github_token_from_git_config(monkeypatch):
    import base64
    from seller_finder.sources import parcels
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    b64 = base64.b64encode(b"x-access-token:tok-xyz").decode()

    class FakeCompleted:
        stdout = f"AUTHORIZATION: basic {b64}\n"
    monkeypatch.setattr(parcels.subprocess, "run", lambda *a, **k: FakeCompleted())
    assert parcels._github_token() == "tok-xyz"


def test_download_falls_back_to_txgio_when_mirror_fails(monkeypatch, tmp_path):
    from seller_finder.sources import parcels
    calls = []
    monkeypatch.setattr(parcels, "_download_from_mirror",
                        lambda county, zp: calls.append("mirror") or False)

    def fake_txgio(county, cfg, zp):
        calls.append("txgio")
        import zipfile
        gdb = tmp_path / "fake.gdb"; gdb.mkdir()
        (gdb / "gdb").write_text("x")
        with zipfile.ZipFile(zp, "w") as z:
            z.write(gdb / "gdb", "fake.gdb/gdb")
        return True
    monkeypatch.setattr(parcels, "_download_from_txgio", fake_txgio)
    gdb_path = parcels.download_county_gdb("bexar", tmp_path / "dl")
    assert calls == ["mirror", "txgio"]
    assert gdb_path.name == "fake.gdb"


def test_download_raises_when_all_sources_fail(monkeypatch, tmp_path):
    import pytest as _pytest
    from seller_finder.sources import parcels
    monkeypatch.setattr(parcels, "_download_from_mirror", lambda c, z: False)
    monkeypatch.setattr(parcels, "_download_from_txgio", lambda c, cfg, z: False)
    with _pytest.raises(RuntimeError, match="All parcel download sources failed"):
        parcels.download_county_gdb("bexar", tmp_path / "dl")


# ── FUB auto-push ─────────────────────────────────────────────────────────────

def _make_awaiting_lead(db, prop_id, emails="[]", phones="[]", dnc=0, litigator=0,
                        matched=1, with_trace=True):
    _insert_parcel(db, prop_id=prop_id, absentee=1)
    compute_scores(db, [{"county": "bexar", "prop_id": prop_id, "kind": "mortgage",
                         "doc_number": "1", "month": 8, "year": 2026}], [], {})
    trace_id = None
    if with_trace:
        cur = db.execute(
            "INSERT INTO skip_traces (owner_key, provider, matched, emails, phones, dnc, litigator, traced_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"OWNER-{prop_id}|78209", "test", matched, emails, phones, dnc, litigator, now_iso()))
        trace_id = cur.lastrowid
    db.execute("UPDATE leads SET status='awaiting_approval', skip_trace_id=? WHERE prop_id=?",
               (trace_id, prop_id))
    db.commit()
    return db.execute("SELECT id FROM leads WHERE prop_id=?", (prop_id,)).fetchone()["id"]


def test_auto_push_holds_uncontactable(db, monkeypatch):
    from seller_finder import fub
    _make_awaiting_lead(db, "7001", emails="[]", phones="[]")  # matched but empty
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "FUB_API_KEY", "fake")
    monkeypatch.setattr(fub, "push_lead", lambda conn, lead: "999")
    stats = fub.auto_push_leads(db)
    assert stats["held_no_contact"] == 1 and stats["pushed"] == 0
    assert db.execute("SELECT status FROM leads WHERE prop_id='7001'").fetchone()["status"] == "held_no_contact"


def test_auto_push_pushes_contactable(db, monkeypatch):
    from seller_finder import fub
    _make_awaiting_lead(db, "7002", emails='["a@b.com"]', phones='[{"number": "2105551234"}]')
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "FUB_API_KEY", "fake")
    monkeypatch.setattr(fub, "push_lead", lambda conn, lead: "12345")
    stats = fub.auto_push_leads(db)
    assert stats["pushed"] == 1
    lead = db.execute("SELECT status, fub_person_id FROM leads WHERE prop_id='7002'").fetchone()
    assert lead["status"] == "pushed" and lead["fub_person_id"] == "12345"


def test_auto_push_skipped_without_fub_key(db, monkeypatch):
    from seller_finder import fub
    _make_awaiting_lead(db, "7003", emails='["a@b.com"]')
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "FUB_API_KEY", "")
    stats = fub.auto_push_leads(db)
    assert stats["pushed"] == 0 and stats["total"] == 0
    assert db.execute("SELECT status FROM leads WHERE prop_id='7003'").fetchone()["status"] == "awaiting_approval"


def test_dnc_flag_adds_dnc_tag(db, monkeypatch):
    """The DNC tag must reach FUB on the create path.

    Rewritten: the previous version built the tag list in the test body and
    then asserted its own list contained "DNC". It passed whatever push_lead
    did — including doing nothing at all — because production code never ran.
    Now it captures the payload push_lead actually sends.
    """
    from seller_finder import fub
    lead_id = _make_awaiting_lead(db, "7004", phones='[{"number": "2105551234"}]', dnc=1)
    lead = dict(db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())

    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "FUB_API_KEY", "fake")
    monkeypatch.setattr(fub, "find_existing_person", lambda *a, **k: None)
    monkeypatch.setattr(fub, "generate_summary_note", lambda lead: "")

    sent = {}

    class Resp:
        status_code = 200
        def raise_for_status(self):
            return None
        def json(self):
            return {"id": 4242}

    class Session:
        auth = None
        headers = {}
        def update(self, *a, **k):
            return None
        def get(self, *a, **k):
            return Resp()
        def post(self, url, json=None, **k):
            if "/people" in url:
                sent["payload"] = json
            return Resp()
    monkeypatch.setattr(fub.requests, "Session", lambda: Session())

    assert fub.push_lead(db, lead) == "4242"
    assert "DNC" in sent["payload"]["tags"]
    assert config.TAG_SELLER in sent["payload"]["tags"]
    assert "Pre-Foreclosure" in sent["payload"]["tags"]


def test_no_dnc_flag_means_no_dnc_tag(db, monkeypatch):
    """Over-correction guard: a clean owner must not be tagged DNC, or the
    nurture system would suppress outreach to every lead we push."""
    from seller_finder import fub
    lead_id = _make_awaiting_lead(db, "7005b", phones='[{"number": "2105559999"}]', dnc=0)
    lead = dict(db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())

    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "FUB_API_KEY", "fake")
    monkeypatch.setattr(fub, "find_existing_person", lambda *a, **k: None)
    monkeypatch.setattr(fub, "generate_summary_note", lambda lead: "")

    sent = {}

    class Resp:
        status_code = 200
        def raise_for_status(self):
            return None
        def json(self):
            return {"id": 5}

    class Session:
        auth = None
        headers = {}
        def update(self, *a, **k):
            return None
        def get(self, *a, **k):
            return Resp()
        def post(self, url, json=None, **k):
            if "/people" in url:
                sent["payload"] = json
            return Resp()
    monkeypatch.setattr(fub.requests, "Session", lambda: Session())

    fub.push_lead(db, lead)
    assert "DNC" not in sent["payload"]["tags"]


def test_held_leads_eligible_for_retrace(db, monkeypatch):
    """held_no_contact leads without a skip trace must be retraced once a key exists."""
    from seller_finder.skiptrace import tracer
    _insert_parcel(db, prop_id="7005", absentee=1)
    compute_scores(db, [{"county": "bexar", "prop_id": "7005", "kind": "mortgage",
                         "doc_number": "1", "month": 8, "year": 2026}], [], {})
    db.execute("UPDATE leads SET status='held_no_contact', skip_trace_id=NULL WHERE prop_id='7005'")
    db.commit()

    class FakeProvider:
        name = "fake"
        def trace_batch(self, reqs):
            from seller_finder.skiptrace.base import SkipTraceResult
            return [SkipTraceResult(matched=True, emails=["x@y.com"], phones=[],
                                    provider="fake") for _ in reqs]

    monkeypatch.setattr(tracer, "get_provider", lambda name="x": FakeProvider())
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "BATCHDATA_API_KEY", "fake-key")
    stats = tracer.trace_qualified_leads(db)
    assert stats["eligible"] == 1 and stats["traced"] == 1
    assert db.execute("SELECT status FROM leads WHERE prop_id='7005'").fetchone()["status"] == "traced"


# ── Review files ─────────────────────────────────────────────────────────

def test_review_csv_written(db, tmp_path, monkeypatch):
    from seller_finder import review

    monkeypatch.setattr(config, "REVIEW_DIR", tmp_path / "review")
    _insert_parcel(db, prop_id="4001", absentee=1)
    compute_scores(db, [{"county": "bexar", "prop_id": "4001", "kind": "mortgage",
                         "doc_number": "1", "month": 8, "year": 2026}], [], {})
    db.execute("UPDATE leads SET status='traced' WHERE prop_id='4001'")
    db.commit()
    review.stage_traced_leads(db)
    out = review.write_review_files(db)
    assert out["pending"] == 1
    assert (tmp_path / "review" / "pending_leads.csv").exists()
    assert (tmp_path / "review" / "run_summary.md").exists()


# ── Warm tier ────────────────────────────────────────────────────────────

def test_warm_lead_stored_compact(db):
    """Absentee-only (30) lands in warm_leads, not leads — never traced/pushed."""
    _insert_parcel(db, prop_id="8001", absentee=1)
    stats = compute_scores(db, [], [], {})
    assert stats["warm"] == 1 and stats["qualified"] == 0
    row = db.execute(
        "SELECT score, signals FROM warm_leads WHERE prop_id='8001'").fetchone()
    assert row["score"] == 30
    assert json.loads(row["signals"]) == ["absentee_owner"]
    assert db.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"] == 0


def test_warm_lead_auto_promotes(db):
    """A warm lead that gains a foreclosure signal is promoted to qualified
    and its warm row is removed."""
    _insert_parcel(db, prop_id="8002", absentee=1)
    compute_scores(db, [], [], {})
    assert db.execute("SELECT COUNT(*) c FROM warm_leads").fetchone()["c"] == 1
    stats = compute_scores(db, [{"county": "bexar", "prop_id": "8002",
                                 "kind": "mortgage", "doc_number": "d1",
                                 "month": 8, "year": 2026}], [], {})
    assert stats["warm_promoted"] == 1 and stats["qualified"] == 1
    assert db.execute("SELECT COUNT(*) c FROM warm_leads").fetchone()["c"] == 0
    lead = db.execute("SELECT score, status FROM leads WHERE prop_id='8002'").fetchone()
    assert lead["score"] == 60 and lead["status"] == "qualified"


def test_warm_leads_never_eligible_for_trace(db, monkeypatch):
    """Warm-tier leads must never enter the skip-trace queue (zero spend)."""
    from seller_finder.skiptrace import tracer
    _insert_parcel(db, prop_id="8003", absentee=1)
    compute_scores(db, [], [], {})
    monkeypatch.setattr(config, "BATCHDATA_API_KEY", "fake-key")
    stats = tracer.trace_qualified_leads(db)
    assert stats["eligible"] == 0


# ── Budget + spend reporting ─────────────────────────────────────────────

def test_spend_stats_month_to_date(db, monkeypatch):
    from seller_finder.skiptrace import tracer
    monkeypatch.setattr(config, "SKIP_TRACE_COST_USD", 0.15)
    # Seed 3 traces this month
    for i in range(3):
        db.execute(
            "INSERT INTO skip_traces (owner_key, provider, matched, traced_at) "
            "VALUES (?,?,1,?)", (f"owner{i}|78201", "batchdata", now_iso()))
    db.commit()
    stats = {"traced": 2}
    tracer._add_spend_stats(db, stats)
    assert stats["run_cost_usd"] == 0.30
    assert stats["mtd_traces"] == 3
    assert stats["mtd_cost_usd"] == 0.45


def test_budget_caps_traces_per_run(db, monkeypatch):
    """Only `budget` owners are traced; the rest are budget-deferred."""
    from seller_finder.skiptrace import tracer
    for i in range(4):
        _insert_parcel(db, prop_id=f"90{i}", owner=f"OWNER{i} TEST",
                       situs=f"{i} ELM ST", mail=f"{i} OAK AVE")
        compute_scores(db, [{"county": "bexar", "prop_id": f"90{i}",
                             "kind": "mortgage", "doc_number": f"d{i}",
                             "month": 8, "year": 2026}], [], {})
    class FakeProvider:
        name = "fake"
        def trace_batch(self, reqs):
            from seller_finder.skiptrace.base import SkipTraceResult
            return [SkipTraceResult(matched=True, emails=["a@b.com"], phones=[],
                                    provider="fake") for _ in reqs]
    monkeypatch.setattr(tracer, "get_provider", lambda name="x": FakeProvider())
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "BATCHDATA_API_KEY", "fake-key")
    monkeypatch.setattr(config, "MAX_SKIP_TRACES_PER_RUN", 2)
    stats = tracer.trace_qualified_leads(db)
    assert stats["traced"] == 2 and stats["budget_skipped"] == 2


# ── Foreclosure CSV inbox (Travis et al) ─────────────────────────────────

def test_foreclosure_inbox_ingest(db, tmp_path, monkeypatch):
    from seller_finder.sources import preforeclosure
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setitem(config.SETTINGS, "foreclosure_sources",
                        {"travis": {"inbox": True}})
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "foreclosures_travis_2026-07.csv").write_text(
        "address,zip,doc_number,city\n"
        "42 CONGRESS AVE,78701,2026123456,AUSTIN\n"
        ",78701,skip-no-address,AUSTIN\n")
    notices = preforeclosure.fetch("travis", conn=db)
    assert len(notices) == 1
    assert notices[0]["address"] == "42 CONGRESS AVE"
    assert notices[0]["doc_number"] == "2026123456"
    # The CSV stays committed in the repo; the ledger (not a rename) is what
    # makes ingestion exactly-once, because the runner never commits back to main.
    assert (inbox / "foreclosures_travis_2026-07.csv").exists()
    assert preforeclosure.fetch("travis", conn=db) == []


def test_foreclosure_inbox_csvs_are_not_gitignored():
    """Regression: data/inbox/*.csv was gitignored, so Travis (inbox-only, no
    live feed) could never receive a single foreclosure notice on Actions."""
    import subprocess
    from pathlib import Path as _P
    repo = _P(__file__).resolve().parents[1]
    out = subprocess.run(
        ["git", "check-ignore", "-v", "data/inbox/foreclosures_travis_2026-07.csv"],
        cwd=repo, capture_output=True, text=True)
    # exit 1 == "not ignored"
    assert out.returncode == 1, (
        "data/inbox/*.csv is gitignored — committed county exports will never "
        f"reach the Actions runner. Matched rule: {out.stdout.strip()}")


class _OkFeedResp:
    """One ArcGIS page carrying a single mortgage/tax notice."""
    status_code = 200
    @staticmethod
    def raise_for_status():
        return None
    @staticmethod
    def json():
        return {"features": [{"attributes": {
            "ADDRESS": "1 MAIN ST", "DOC_NUMBER": "D1", "YEAR": 2026,
            "MONTH": 8, "CITY": "SAN ANTONIO", "ZIP": "78201"}}]}


@pytest.fixture
def no_backoff(monkeypatch):
    """Skip the ArcGIS retry sleeps so feed tests stay fast."""
    from seller_finder import arcgis
    monkeypatch.setattr(arcgis.time, "sleep", lambda s: None)


def test_live_feed_failure_is_an_error_not_zero_notices(monkeypatch, no_backoff):
    """A blocked foreclosure feed must raise, never look like a quiet month."""
    from seller_finder import arcgis
    from seller_finder.sources import preforeclosure

    monkeypatch.setitem(config.SETTINGS, "foreclosure_sources",
                        {"bexar": {"mortgage_url": "https://example.invalid/m",
                                   "tax_url": "https://example.invalid/t"}})

    def boom(*a, **k):
        raise OSError("connection reset")
    monkeypatch.setattr(arcgis.requests, "get", boom)

    with pytest.raises(preforeclosure.FeedUnavailable):
        preforeclosure.fetch("bexar")


def test_partial_feed_failure_still_returns_the_good_half(monkeypatch, no_backoff):
    """One of two feeds failing is degraded, not fatal — keep what we got."""
    from seller_finder import arcgis
    from seller_finder.sources import preforeclosure

    monkeypatch.setitem(config.SETTINGS, "foreclosure_sources",
                        {"bexar": {"mortgage_url": "https://example.invalid/m",
                                   "tax_url": "https://example.invalid/t"}})

    def flaky(url, **k):
        # The mortgage layer is down for good — every retry fails. The tax
        # layer answers normally.
        if url.endswith("/m"):
            raise OSError("mortgage feed down")
        return _OkFeedResp()
    monkeypatch.setattr(arcgis.requests, "get", flaky)

    notices = preforeclosure.fetch("bexar")
    assert len(notices) == 1 and notices[0]["kind"] == "tax"


def test_transient_feed_error_is_retried_then_succeeds(monkeypatch, no_backoff):
    """A one-off blip must not cost us the county's notices for the day."""
    from seller_finder import arcgis
    from seller_finder.sources import preforeclosure

    monkeypatch.setitem(config.SETTINGS, "foreclosure_sources",
                        {"bexar": {"mortgage_url": "https://example.invalid/m"}})
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient reset")
        return _OkFeedResp()
    monkeypatch.setattr(arcgis.requests, "get", flaky)

    notices = preforeclosure.fetch("bexar")
    assert len(notices) == 1 and calls["n"] == 2


def test_arcgis_200_with_error_body_is_a_failure_not_zero_notices(monkeypatch, no_backoff):
    """ArcGIS reports query failures as HTTP 200 + {"error": ...}.

    raise_for_status() passes and data["features"] is absent, so the old code
    read a broken layer as 'no foreclosures this month' and the run went green.
    Same defect class as the BatchData 403-became-64-no-matches incident.
    """
    from seller_finder import arcgis
    from seller_finder.sources import preforeclosure

    monkeypatch.setitem(config.SETTINGS, "foreclosure_sources",
                        {"bexar": {"mortgage_url": "https://example.invalid/m",
                                   "tax_url": "https://example.invalid/t"}})

    class ArcErr:
        status_code = 200
        @staticmethod
        def raise_for_status():
            return None
        @staticmethod
        def json():
            return {"error": {"code": 400, "message": "Unable to complete operation",
                              "details": ["Invalid field: ADDRESS"]}}
    monkeypatch.setattr(arcgis.requests, "get", lambda *a, **k: ArcErr())

    with pytest.raises(preforeclosure.FeedUnavailable):
        preforeclosure.fetch("bexar")


def test_arcgis_body_error_is_not_retried(monkeypatch, no_backoff):
    """A malformed query fails identically every time — don't burn 3 attempts."""
    from seller_finder import arcgis

    calls = {"n": 0}

    class ArcErr:
        status_code = 200
        @staticmethod
        def raise_for_status():
            return None
        @staticmethod
        def json():
            return {"error": {"code": 400, "message": "Invalid field"}}

    def counting(*a, **k):
        calls["n"] += 1
        return ArcErr()
    monkeypatch.setattr(arcgis.requests, "get", counting)

    with pytest.raises(arcgis.ArcGISError, match="Invalid field"):
        arcgis.query(None, "https://example.invalid/q", {}, attempts=3)
    assert calls["n"] == 1


def test_arcgis_empty_result_is_still_a_valid_zero(monkeypatch, no_backoff):
    """Guard against over-correction: a healthy empty page must stay empty,
    not become an error — some months genuinely have no notices."""
    from seller_finder import arcgis
    from seller_finder.sources import preforeclosure

    monkeypatch.setitem(config.SETTINGS, "foreclosure_sources",
                        {"bexar": {"mortgage_url": "https://example.invalid/m"}})

    class Empty:
        status_code = 200
        @staticmethod
        def raise_for_status():
            return None
        @staticmethod
        def json():
            return {"features": []}
    monkeypatch.setattr(arcgis.requests, "get", lambda *a, **k: Empty())

    assert preforeclosure.fetch("bexar") == []


def test_exemption_arcgis_error_raises_instead_of_emptying_snapshot(db, monkeypatch, no_backoff):
    """An ArcGIS error body must not end the paging loop as a clean zero.

    An empty pull is diffed against the previous snapshot, which is exactly the
    mass-homestead-removed scenario the truncation guard exists to prevent —
    and +10 is enough to lift an absentee lead to the 40 trace threshold.
    """
    from seller_finder import arcgis
    from seller_finder.sources import exemptions

    monkeypatch.setitem(config.SETTINGS, "exemption_sources",
                        {"bexar": {"url": "https://example.invalid/q"}})

    class ArcErr:
        status_code = 200
        @staticmethod
        def raise_for_status():
            return None
        @staticmethod
        def json():
            return {"error": {"code": 500, "message": "Layer not found"}}
    monkeypatch.setattr(arcgis.requests, "Session",
                        lambda: type("S", (), {"get": staticmethod(lambda *a, **k: ArcErr())})())
    monkeypatch.setattr(exemptions.requests, "Session",
                        lambda: type("S", (), {"get": staticmethod(lambda *a, **k: ArcErr())})())

    with pytest.raises(arcgis.ArcGISError, match="Layer not found"):
        exemptions.sync_county(db, "bexar")


# ── Divorce matching: spend control ──────────────────────────────────────

def _seed_divorce_case(db, case_number="2026CI001", party="JOHN SMITH"):
    from seller_finder.sources import divorce
    divorce.store_filings(db, [{"case_number": case_number, "filed_date": "2026-07-01",
                                "party_names": [party], "county": "bexar"}])
    return case_number


def test_divorce_match_attempts_are_capped(db, monkeypatch):
    """A case whose parties own nothing in our counties never matches.

    The query was `WHERE matched_prop_id IS NULL`, so every such case was
    re-sent to Claude on every weekly run, forever — an unbounded recurring
    bill that grows with every filing list ingested.
    """
    from seller_finder.sources import divorce

    _insert_parcel(db, prop_id="AA1", owner="SMITH JOHN")
    _seed_divorce_case(db)
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-fake")

    calls = {"n": 0}

    class NeverMatches:
        class messages:
            @staticmethod
            def create(**k):
                calls["n"] += 1
                return type("R", (), {"content": [type("T", (), {
                    "text": '{"match_index": null, "confidence": 0.0}'})()]})()

    monkeypatch.setattr(divorce, "_anthropic_client", lambda: NeverMatches())

    for _ in range(6):  # six weekly runs
        divorce.match_filings_to_owners(db)
    assert calls["n"] == divorce.MAX_MATCH_ATTEMPTS, (
        f"unmatched case was retried {calls['n']} times; cap is "
        f"{divorce.MAX_MATCH_ATTEMPTS}")
    row = db.execute("SELECT match_attempts FROM divorce_cases").fetchone()
    assert row["match_attempts"] == divorce.MAX_MATCH_ATTEMPTS


def test_divorce_match_succeeds_within_budget(db, monkeypatch):
    """Over-correction guard: a real match still lands on the first attempt."""
    from seller_finder.sources import divorce

    _insert_parcel(db, prop_id="AA2", owner="SMITH JOHN")
    _seed_divorce_case(db, "2026CI002")
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-fake")

    class Matches:
        class messages:
            @staticmethod
            def create(**k):
                return type("R", (), {"content": [type("T", (), {
                    "text": '{"match_index": 0, "confidence": 0.95}'})()]})()

    monkeypatch.setattr(divorce, "_anthropic_client", lambda: Matches())
    matches = divorce.match_filings_to_owners(db)
    assert len(matches) == 1 and matches[0]["prop_id"] == "AA2"
    # A matched case is never re-sent, regardless of the attempt counter.
    assert divorce.match_filings_to_owners(db) == []


def test_divorce_matching_is_skipped_in_dry_run(db, monkeypatch):
    """`dry_run: true` is documented as zero spend; matching is a paid call."""
    from seller_finder.sources import divorce

    _insert_parcel(db, prop_id="AA3", owner="SMITH JOHN")
    _seed_divorce_case(db, "2026CI003")
    monkeypatch.setattr(config, "DRY_RUN", True)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-fake")

    def explode():
        raise AssertionError("DRY_RUN must not build an Anthropic client")
    monkeypatch.setattr(divorce, "_anthropic_client", explode)

    assert divorce.match_filings_to_owners(db) == []
    # A dry run must not consume the retry budget either.
    assert db.execute(
        "SELECT COALESCE(match_attempts,0) a FROM divorce_cases").fetchone()["a"] == 0


def test_divorce_bad_llm_confidence_does_not_kill_the_stage(db, monkeypatch):
    """A non-numeric confidence used to raise ValueError out of the whole
    function, taking every remaining case down with it."""
    from seller_finder.sources import divorce

    _insert_parcel(db, prop_id="AA4", owner="SMITH JOHN")
    _seed_divorce_case(db, "2026CI004")
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-fake")

    class Garbage:
        class messages:
            @staticmethod
            def create(**k):
                return type("R", (), {"content": [type("T", (), {
                    "text": '{"match_index": 0, "confidence": "high"}'})()]})()

    monkeypatch.setattr(divorce, "_anthropic_client", lambda: Garbage())
    assert divorce.match_filings_to_owners(db) == []  # no exception


def test_divorce_inbox_dir_follows_data_dir(tmp_path, monkeypatch):
    """INBOX_DIR was a module-level constant, so this module read a different
    inbox than deeds.py and preforeclosure.py whenever DATA_DIR was set."""
    from seller_finder.sources import divorce
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "divorce_2026-07-01.csv").write_text(
        "case_number,filed_date,petitioner,respondent\n"
        "2026CI999,2026-07-01,Jane Doe,John Doe\n")
    filings = divorce.fetch_from_csv()
    assert len(filings) == 1 and filings[0]["case_number"] == "2026CI999"


# ── Light sync (daily runs) ──────────────────────────────────────────────

@pytest.fixture
def clean_asset_key():
    """_LAST_ASSET_KEY is module-global. Tests that write it must not leak into
    each other — one test asserting a key is ABSENT and another setting it made
    the pair order-dependent."""
    from seller_finder.sources import parcels as pmod
    saved = dict(pmod._LAST_ASSET_KEY)
    pmod._LAST_ASSET_KEY.clear()
    yield pmod._LAST_ASSET_KEY
    pmod._LAST_ASSET_KEY.clear()
    pmod._LAST_ASSET_KEY.update(saved)


def _fake_gdb_rows(n=3, owner="SMITH JOHN"):
    """Rows in the shape read_gdb_rows yields, so sync_county runs for real."""
    return [{"prop_id": str(1000 + i), "owner_name": owner,
             "situs_addr": f"{i} MAIN ST", "situs_city": "SAN ANTONIO",
             "situs_zip": "78201", "mail_addr": f"{i} FAR AWAY RD",
             "mail_city": "AUSTIN", "mail_state": "TX", "mail_zip": "78701",
             "mkt_value": 250000.0, "tax_year": 2025} for i in range(n)]


def test_sync_county_records_mirror_asset_key(db, monkeypatch, clean_asset_key, tmp_path):
    """sync_county must persist the asset key it actually synced from.

    Rewritten: the previous version inserted the row itself and asserted it
    equalled a global it had also set — production code never ran, so the
    light-sync bookkeeping this guards was entirely untested.
    """
    from seller_finder.sources import parcels as pmod
    monkeypatch.setattr(pmod, "read_gdb_rows", lambda p: iter(_fake_gdb_rows()))
    clean_asset_key["bexar"] = "777:12345"

    stats = pmod.sync_county(db, "bexar", gdb_path=tmp_path / "fake.gdb")
    assert stats["kept"] == 3 and stats["absentee"] == 3
    row = db.execute(
        "SELECT asset_key, kept, absentee FROM parcel_snapshot_meta "
        "WHERE county='bexar'").fetchone()
    assert row["asset_key"] == "777:12345"
    assert row["kept"] == 3 and row["absentee"] == 3


def test_light_sync_skips_bookkeeping_only_when_snapshot_unchanged(
        db, monkeypatch, clean_asset_key, tmp_path):
    """The daily light-sync optimisation: identical mirror asset => skip the
    owner-change writes. A CHANGED asset must re-run them, or an owner change
    landing in a quarterly refresh would be missed forever."""
    from seller_finder.sources import parcels as pmod
    monkeypatch.setattr(pmod, "read_gdb_rows", lambda p: iter(_fake_gdb_rows()))
    clean_asset_key["bexar"] = "777:12345"
    pmod.sync_county(db, "bexar", gdb_path=tmp_path / "fake.gdb")
    assert db.execute(
        "SELECT COUNT(*) c FROM owners_first_seen WHERE county='bexar'").fetchone()["c"] == 3

    # Same asset -> light sync skips bookkeeping.
    stats = pmod.sync_county(db, "bexar", gdb_path=tmp_path / "fake.gdb", light=True)
    assert stats["light_sync"] is True and stats["owner_changes"] == 0

    # New owner AND a new asset key -> bookkeeping must run and see the change.
    monkeypatch.setattr(pmod, "read_gdb_rows",
                        lambda p: iter(_fake_gdb_rows(owner="JONES MARY")))
    clean_asset_key["bexar"] = "888:99999"
    stats = pmod.sync_county(db, "bexar", gdb_path=tmp_path / "fake.gdb", light=True)
    assert stats["light_sync"] is False, "a changed mirror asset must not light-sync"
    assert stats["owner_changes"] == 3
    assert db.execute(
        "SELECT COUNT(*) c FROM owner_history WHERE county='bexar'").fetchone()["c"] == 3


def test_light_sync_still_rebuilds_the_parcel_cache(db, monkeypatch, clean_asset_key, tmp_path):
    """pc.parcels is ephemeral — even a skipped-bookkeeping run must repopulate
    it, or scoring and foreclosure address matching have nothing to work with."""
    from seller_finder.sources import parcels as pmod
    monkeypatch.setattr(pmod, "read_gdb_rows", lambda p: iter(_fake_gdb_rows()))
    clean_asset_key["bexar"] = "777:12345"
    pmod.sync_county(db, "bexar", gdb_path=tmp_path / "fake.gdb")
    db.execute("DELETE FROM pc.parcels")          # simulate a fresh runner
    db.commit()
    pmod.sync_county(db, "bexar", gdb_path=tmp_path / "fake.gdb", light=True)
    assert db.execute(
        "SELECT COUNT(*) c FROM pc.parcels WHERE county='bexar'").fetchone()["c"] == 3


# ── Idempotency: the property that keeps this from double-billing ────────
# The pipeline runs 6x/week against overlapping data (Monday full + Tue-Sat
# fast), can be re-triggered by hand on the same day, and can die mid-run with
# the state DB already committed — the state-sync push step is `if: always()`.
# None of that may cost a second trace or create a second FUB person.

def _cycle(db, foreclosures, api, pushes, monkeypatch=None):
    """One end-to-end run: score -> trace -> stage -> push."""
    from seller_finder import fub
    from seller_finder.skiptrace import tracer
    compute_scores(db, foreclosures, [], {})
    tracer.trace_qualified_leads(db)
    review_stage_traced(db)
    fub.auto_push_leads(db)


def review_stage_traced(db):
    from seller_finder import review
    return review.stage_traced_leads(db)


@pytest.fixture
def pipeline_harness(db, monkeypatch):
    """Counting provider + FUB stub so runs can be replayed and measured."""
    from seller_finder import fub
    from seller_finder.skiptrace import tracer
    from seller_finder.skiptrace.base import SkipTraceResult

    api = {"calls": 0}
    pushes = {"n": 0}

    class CountingProvider:
        name = "count"
        def trace_batch(self, reqs):
            api["calls"] += len(reqs)
            return [SkipTraceResult(matched=True, emails=["a@b.com"],
                                    provider="count") for _ in reqs]

    monkeypatch.setattr(tracer, "get_provider", lambda name="x": CountingProvider())
    monkeypatch.setattr(fub, "push_lead",
                        lambda conn, lead: pushes.__setitem__("n", pushes["n"] + 1)
                        or f"fub-{lead['id']}")
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "BATCHDATA_API_KEY", "fake-key")
    monkeypatch.setattr(config, "FUB_API_KEY", "fake-key")
    return db, api, pushes


def test_repeated_runs_never_double_trace_or_double_push(pipeline_harness):
    """Mon weekly, Tue daily (same notice still in feed), Wed daily (notice
    rotated out), then a hand-triggered re-run on the same day."""
    db, api, pushes = pipeline_harness
    _insert_parcel(db, prop_id="D1", absentee=1)
    fc = [{"county": "bexar", "prop_id": "D1", "kind": "mortgage",
           "doc_number": "d1", "month": 8, "year": 2026}]

    _cycle(db, fc, api, pushes)          # Monday weekly
    assert (api["calls"], pushes["n"]) == (1, 1)

    _cycle(db, fc, api, pushes)          # Tuesday daily, same notice
    _cycle(db, [], api, pushes)          # Wednesday daily, notice gone
    _cycle(db, [], api, pushes)          # manual re-run, same day

    assert api["calls"] == 1, "an owner was skip-traced more than once"
    assert pushes["n"] == 1, "a lead was pushed to FUB more than once"
    assert db.execute("SELECT COUNT(*) c FROM skip_traces").fetchone()["c"] == 1
    row = db.execute("SELECT status, fub_person_id FROM leads").fetchone()
    assert row["status"] == "pushed" and row["fub_person_id"] == "fub-1"


def test_crash_after_trace_does_not_re_bill_on_recovery(pipeline_harness):
    """The state DB is pushed with `if: always()`, so a run that dies after
    tracing keeps the paid result. The recovery run must reuse it and finish
    the push, not pay again."""
    db, api, pushes = pipeline_harness
    from seller_finder.skiptrace import tracer

    _insert_parcel(db, prop_id="D2", absentee=1)
    compute_scores(db, [{"county": "bexar", "prop_id": "D2", "kind": "mortgage",
                         "doc_number": "d2", "month": 8, "year": 2026}], [], {})
    tracer.trace_qualified_leads(db)     # money spent and committed...
    assert api["calls"] == 1
    assert db.execute("SELECT status FROM leads").fetchone()["status"] == "traced"
    # ...then the process dies here, before staging and pushing.

    _cycle(db, [], api, pushes)          # next scheduled run recovers
    assert api["calls"] == 1, "recovery re-billed an already-paid trace"
    assert pushes["n"] == 1
    assert db.execute("SELECT status FROM leads").fetchone()["status"] == "pushed"


def test_daily_and_weekly_same_week_share_one_trace(pipeline_harness, monkeypatch):
    """Budgets differ by run mode but the cache is shared, so a lead traced by
    Monday's weekly run is free for the rest of the week."""
    db, api, pushes = pipeline_harness
    _insert_parcel(db, prop_id="D3", absentee=1)
    fc = [{"county": "bexar", "prop_id": "D3", "kind": "mortgage",
           "doc_number": "d3", "month": 8, "year": 2026}]

    monkeypatch.setattr(config, "RUN_MODE", "weekly")
    monkeypatch.setattr(config, "MAX_SKIP_TRACES_PER_RUN", 75)
    _cycle(db, fc, api, pushes)

    monkeypatch.setattr(config, "RUN_MODE", "daily")
    monkeypatch.setattr(config, "MAX_SKIP_TRACES_PER_RUN", 15)
    for _ in range(5):                   # Tue-Sat
        _cycle(db, fc, api, pushes)

    assert api["calls"] == 1 and pushes["n"] == 1


def test_budget_deferred_lead_is_picked_up_next_run(pipeline_harness, monkeypatch):
    """Leads that miss the budget must not be lost — they stay qualified and
    the next run traces them, highest score first."""
    db, api, pushes = pipeline_harness
    for i in range(3):
        _insert_parcel(db, prop_id=f"E{i}", owner=f"OWNER{i} TEST",
                       situs=f"{i} ELM ST", mail=f"{i} OAK AVE")
        compute_scores(db, [{"county": "bexar", "prop_id": f"E{i}", "kind": "mortgage",
                             "doc_number": f"e{i}", "month": 8, "year": 2026}], [], {})

    monkeypatch.setattr(config, "MAX_SKIP_TRACES_PER_RUN", 1)
    _cycle(db, [], api, pushes)
    assert api["calls"] == 1 and pushes["n"] == 1

    monkeypatch.setattr(config, "MAX_SKIP_TRACES_PER_RUN", 5)
    _cycle(db, [], api, pushes)
    assert api["calls"] == 3, "budget-deferred leads were never picked up"
    assert pushes["n"] == 3
    assert db.execute(
        "SELECT COUNT(*) c FROM leads WHERE status='pushed'").fetchone()["c"] == 3


def test_all_migrations_are_rerunnable_end_to_end(tmp_path):
    """Every migration (v2..v5) must be a no-op on reopen. Regression cover for
    the newer ones: v4 ALTERs a table and v5 rewrites rows + VACUUMs, both of
    which are corrupting if they run twice or inside a transaction."""
    db_file = tmp_path / "mig.sqlite3"
    conn = get_db(db_file, parcels_cache=tmp_path / "c0.sqlite3")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version >= 5
    conn.execute(
        "INSERT INTO skip_traces (owner_key, provider, matched, emails, phones, "
        "dnc, litigator, raw, traced_at) VALUES ('PAID|78201','batchdata',1,"
        "'[\"keep@me.com\"]','[]',0,0,'{\"provider\":\"batchdata\"}',?)", (now_iso(),))
    from seller_finder.sources import divorce
    divorce.store_filings(conn, [{"case_number": "RERUN1", "filed_date": "2026-07-01",
                                  "party_names": ["JANE DOE"], "county": "bexar"}])
    conn.commit()
    conn.close()

    for i in range(3):
        c = get_db(db_file, parcels_cache=tmp_path / f"c{i + 1}.sqlite3")
        assert c.execute("PRAGMA user_version").fetchone()[0] == version
        paid = c.execute("SELECT emails, matched FROM skip_traces").fetchone()
        assert paid["matched"] == 1 and json.loads(paid["emails"]) == ["keep@me.com"]
        assert c.execute("SELECT COUNT(*) c FROM divorce_cases").fetchone()["c"] == 1
        assert c.execute(
            "SELECT COALESCE(match_attempts,0) a FROM divorce_cases").fetchone()["a"] == 0
        c.close()


# ── Production entry points actually import ──────────────────────────────
# This file starts with `sys.path.insert(0, .../src)`, which is a test-harness
# convenience the real runners do not get: they do `sys.path.insert(0, "src")`,
# relative to the working directory, and are invoked as `python3 run_*.py` from
# the repo root by the workflows. Nothing here exercised that, so the suite
# passed green with a broken import in a runner — the Monday run would simply
# die on the first line of the job. These tests run each entry point in a FRESH
# interpreter, from the repo root, with no path help, exactly as Actions does.

REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRY_POINTS = ("run_weekly_pull.py", "run_daily_pull.py", "run_push_approved.py")


def _import_in_subprocess(target: str, extra_env: dict | None = None):
    """Import `target` in a clean interpreter without executing its main().

    Loading it under a module name other than "__main__" runs every top-level
    import while leaving the `if __name__ == "__main__"` block alone — so this
    proves the imports resolve without running the pipeline or touching a
    network. Deliberately no conftest.py and no PYTHONPATH: if production
    needs a path fixup, production has to be the thing that provides it.
    """
    import os
    import subprocess
    code = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('entry_under_test', {target!r})\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "assert callable(mod.main), 'entry point exposes no main()'\n"
        "print('IMPORT_OK')\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                          capture_output=True, text=True, env=env, timeout=120)


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_entry_point_imports_cleanly(entry):
    """A broken import in a runner must fail the suite, not just Monday."""
    result = _import_in_subprocess(entry)
    assert result.returncode == 0, (
        f"{entry} does not import as the workflow runs it:\n{result.stderr}")
    assert "IMPORT_OK" in result.stdout


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_entry_point_imports_without_any_secrets(entry):
    """Every secret is optional-by-design; import must not depend on one.

    Import-time work (config module constants, int() on SMTP_PORT, provider
    construction) is where a missing or empty secret turns into a crash before
    a single log line is written.
    """
    blank = {k: "" for k in (
        "ANTHROPIC_API_KEY", "FUB_API_KEY", "BATCHDATA_API_KEY", "HEALTHCHECK_URL",
        "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_FROM",
        "MAX_SKIP_TRACES_PER_RUN", "RUN_MODE", "DRY_RUN", "LLM_MODEL")}
    result = _import_in_subprocess(entry, blank)
    assert result.returncode == 0, (
        f"{entry} fails to import with all secrets empty — GitHub Actions sets "
        f"unset secrets to the empty string:\n{result.stderr}")


def test_every_package_module_imports():
    """Catches a module no test happens to import (a new source, a helper).

    Without this, adding a file with a syntax error or a bad import can sit
    green until the run that first needs it.
    """
    import subprocess
    pkg = REPO_ROOT / "src" / "seller_finder"
    modules = sorted(
        ".".join(p.relative_to(REPO_ROOT / "src").with_suffix("").parts)
        for p in pkg.rglob("*.py") if p.name != "__init__.py")
    assert len(modules) >= 12, f"expected the full package, found {modules}"
    code = "import importlib\n" + "".join(
        f"importlib.import_module({m!r})\n" for m in modules) + "print('ALL_OK')\n"
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT / "src",
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, f"package import failed:\n{result.stderr}"


def test_workflows_invoke_the_entry_points_that_exist():
    """Guard against renaming a runner and leaving the workflow pointing at
    the old name — the failure would only appear on the next cron fire."""
    import re
    wf_dir = REPO_ROOT / ".github" / "workflows"
    referenced = set()
    for wf in wf_dir.glob("*.yml"):
        referenced |= set(re.findall(r"python3?\s+(run_[a-z_]+\.py)", wf.read_text()))
    assert referenced, "no workflow invokes a runner — did the run: steps change?"
    for script in referenced:
        assert (REPO_ROOT / script).exists(), (
            f"a workflow runs {script}, which does not exist in the repo")


# ── Audit regressions (2026-07) ──────────────────────────────────────────
# Each test below pins a defect found in the pre-scale audit. See FINDINGS.md.

def test_batchdata_200_with_error_body_is_an_error_not_no_matches(monkeypatch):
    """The 403→64-no-matches bug, one layer down: BatchData can return HTTP 200
    with the real status in the body. That must NOT be cached as no-match."""
    from seller_finder.skiptrace import batchdata
    from seller_finder.skiptrace.base import SkipTraceRequest

    class R200Err:
        status_code = 200
        text = '{"status":{"code":401,"text":"Unauthorized"}}'
        @staticmethod
        def json():
            return {"status": {"code": 401, "text": "Unauthorized"},
                    "results": {"persons": []}}

    monkeypatch.setattr(batchdata.requests, "post", lambda *a, **k: R200Err())
    p = batchdata.BatchDataProvider(api_key="k")
    out = p.trace_batch([SkipTraceRequest(street=f"{i} A ST", city="SA", state="TX",
                                          zip="78201") for i in range(3)])
    assert len(out) == 3
    assert all(r.error and "401" in r.error for r in out)
    assert all(r.matched is False for r in out)


def test_batchdata_200_all_records_errored_is_an_error(monkeypatch):
    """meta errorCount covering the whole chunk means nothing was looked up."""
    from seller_finder.skiptrace import batchdata
    from seller_finder.skiptrace.base import SkipTraceRequest

    class R200:
        status_code = 200
        text = "{}"
        @staticmethod
        def json():
            return {"results": {"persons": [], "meta": {"results": {
                "requestCount": 2, "matchCount": 0, "noMatchCount": 0,
                "errorCount": 2}}}}

    monkeypatch.setattr(batchdata.requests, "post", lambda *a, **k: R200())
    p = batchdata.BatchDataProvider(api_key="k")
    out = p.trace_batch([SkipTraceRequest(street=f"{i} A ST", city="SA", state="TX",
                                          zip="78201") for i in range(2)])
    assert all(r.error and "errorCount=2" in r.error for r in out)


def test_batchdata_genuine_no_match_is_still_cacheable(monkeypatch):
    """Guard against over-correction: a real all-no-match run must stay clean."""
    from seller_finder.skiptrace import batchdata
    from seller_finder.skiptrace.base import SkipTraceRequest

    class R200:
        status_code = 200
        text = "{}"
        @staticmethod
        def json():
            return {"status": {"code": 200}, "results": {"persons": [], "meta": {
                "results": {"requestCount": 1, "matchCount": 0,
                            "noMatchCount": 1, "errorCount": 0}}}}

    monkeypatch.setattr(batchdata.requests, "post", lambda *a, **k: R200())
    p = batchdata.BatchDataProvider(api_key="k")
    out = p.trace_batch([SkipTraceRequest(street="1 A ST", city="SA", state="TX", zip="78201")])
    assert out[0].error is None and out[0].matched is False


def test_batchdata_unaligned_matches_are_errors_not_no_matches(monkeypatch):
    """A PAID match we can't map back to our request must never be cached.

    We index the provider's persons by normalized street+zip. If BatchData
    normalizes differently ("123 Main Street" vs our "123 MAIN ST") the lookup
    misses, and the old code wrote the result out as a genuine no-match: money
    spent, contact info discarded, and the no-match cache then blocked a
    re-trace for no_match_retrace_days (90). meta.matchCount is the tell.
    """
    from seller_finder.skiptrace import batchdata
    from seller_finder.skiptrace.base import SkipTraceRequest

    class R200:
        status_code = 200
        text = "{}"
        @staticmethod
        def json():
            return {"status": {"code": 200}, "results": {
                "persons": [
                    {"propertyAddress": {"street": "123 Main Street", "zip": "78201"},
                     "meta": {"matched": True}, "emails": [{"email": "a@b.com"}]},
                    {"propertyAddress": {"street": "456 Oak Avenue", "zip": "78209"},
                     "meta": {"matched": True}, "emails": [{"email": "c@d.com"}]}],
                "meta": {"results": {"requestCount": 2, "matchCount": 2,
                                     "noMatchCount": 0, "errorCount": 0}}}}

    monkeypatch.setattr(batchdata.requests, "post", lambda *a, **k: R200())
    p = batchdata.BatchDataProvider(api_key="k")
    out = p.trace_batch([
        SkipTraceRequest(street="123 MAIN ST", city="SA", state="TX", zip="78201"),
        SkipTraceRequest(street="456 OAK AVE", city="SA", state="TX", zip="78209")])
    assert all(r.error and "alignment" in r.error for r in out)
    assert not any(r.matched for r in out)


def test_batchdata_aligned_matches_pass_through(monkeypatch):
    """Guard against over-correction: when the keys line up, matches survive."""
    from seller_finder.skiptrace import batchdata
    from seller_finder.skiptrace.base import SkipTraceRequest

    class R200:
        status_code = 200
        text = "{}"
        @staticmethod
        def json():
            return {"status": {"code": 200}, "results": {
                "persons": [{"propertyAddress": {"street": "123 MAIN ST", "zip": "78201"},
                             "meta": {"matched": True},
                             "emails": [{"email": "a@b.com"}],
                             "phones": []}],
                "meta": {"results": {"requestCount": 1, "matchCount": 1,
                                     "noMatchCount": 0, "errorCount": 0}}}}

    monkeypatch.setattr(batchdata.requests, "post", lambda *a, **k: R200())
    p = batchdata.BatchDataProvider(api_key="k")
    out = p.trace_batch([SkipTraceRequest(street="123 MAIN ST", city="SA",
                                          state="TX", zip="78201")])
    assert out[0].error is None and out[0].matched is True
    assert out[0].emails == ["a@b.com"]


def test_batchdata_partial_match_response_still_aligns(monkeypatch):
    """1 match + 1 genuine no-match in one chunk is normal and must pass."""
    from seller_finder.skiptrace import batchdata
    from seller_finder.skiptrace.base import SkipTraceRequest

    class R200:
        status_code = 200
        text = "{}"
        @staticmethod
        def json():
            return {"status": {"code": 200}, "results": {
                "persons": [{"propertyAddress": {"street": "123 MAIN ST", "zip": "78201"},
                             "meta": {"matched": True}, "emails": [{"email": "a@b.com"}]}],
                "meta": {"results": {"requestCount": 2, "matchCount": 1,
                                     "noMatchCount": 1, "errorCount": 0}}}}

    monkeypatch.setattr(batchdata.requests, "post", lambda *a, **k: R200())
    p = batchdata.BatchDataProvider(api_key="k")
    out = p.trace_batch([
        SkipTraceRequest(street="123 MAIN ST", city="SA", state="TX", zip="78201"),
        SkipTraceRequest(street="999 GONE RD", city="SA", state="TX", zip="78209")])
    assert [r.error for r in out] == [None, None]
    assert [r.matched for r in out] == [True, False]


def test_fub_tag_update_failure_is_not_a_successful_push(db, monkeypatch):
    """The tag PUT is the entire job on the existing-person path.

    Its response used to be discarded, so a 4xx/5xx marked the lead 'pushed'
    while the DNC tag never reached FUB — the nurture system would then call
    or text a do-not-call owner with nothing in the logs to show it.
    """
    from seller_finder import fub

    lead_id = _make_awaiting_lead(db, "7201", phones='[{"number": "2105551234"}]', dnc=1)
    lead = dict(db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "FUB_API_KEY", "fake")
    monkeypatch.setattr(fub, "generate_summary_note", lambda lead: "")
    monkeypatch.setattr(fub, "find_existing_person", lambda *a, **k: "555")

    class Resp:
        def __init__(self, code):
            self.status_code = code
        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")
        def json(self):
            return {"tags": ["Existing"]}

    class Session:
        auth = None
        headers = {}
        def update(self, *a, **k):
            return None
        def get(self, *a, **k):
            return Resp(200)
        def put(self, *a, **k):
            return Resp(500)          # applying the tags fails
        def post(self, *a, **k):
            return Resp(200)
    monkeypatch.setattr(fub.requests, "Session", lambda: Session())

    assert fub.push_lead(db, lead) is None, "a failed tag PUT must not report success"


def test_fub_tag_update_success_returns_person(db, monkeypatch):
    """Guard against over-correction: a 200 PUT still tags and returns the id."""
    from seller_finder import fub

    lead_id = _make_awaiting_lead(db, "7202", phones='[{"number": "2105551234"}]')
    lead = dict(db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "FUB_API_KEY", "fake")
    monkeypatch.setattr(fub, "generate_summary_note", lambda lead: "")
    monkeypatch.setattr(fub, "find_existing_person", lambda *a, **k: "555")

    sent = {}

    class Resp:
        status_code = 200
        def raise_for_status(self):
            return None
        def json(self):
            return {"tags": ["Existing"]}

    class Session:
        auth = None
        headers = {}
        def update(self, *a, **k):
            return None
        def get(self, *a, **k):
            return Resp()
        def put(self, url, json=None, **k):
            sent["tags"] = json["tags"]
            return Resp()
        def post(self, *a, **k):
            return Resp()
    monkeypatch.setattr(fub.requests, "Session", lambda: Session())

    assert fub.push_lead(db, lead) == "555"
    assert "Existing" in sent["tags"] and config.TAG_SELLER in sent["tags"]


def test_dry_run_never_calls_anthropic(db, monkeypatch):
    """`dry_run: true` is documented as zero spend. The Claude note call used
    to sit ABOVE the DRY_RUN bail-out, billing one completion per lead."""
    from seller_finder import fub

    lead_id = _make_awaiting_lead(db, "7203", emails='["a@b.com"]')
    lead = dict(db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
    monkeypatch.setattr(config, "DRY_RUN", True)
    monkeypatch.setattr(config, "FUB_API_KEY", "fake")
    monkeypatch.setattr(fub, "find_existing_person", lambda *a, **k: None)

    def explode(lead):
        raise AssertionError("DRY_RUN must not spend Anthropic tokens")
    monkeypatch.setattr(fub, "generate_summary_note", explode)

    assert fub.push_lead(db, lead) is None


def test_push_approved_without_fub_key_is_a_reported_error(db, monkeypatch):
    """Parity with auto_push_leads: name the missing secret once, change
    nothing, and surface it as a stage error rather than N per-lead failures."""
    from seller_finder import fub
    from seller_finder.health import collect_stage_errors

    _make_awaiting_lead(db, "7204", emails='["a@b.com"]')
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "FUB_API_KEY", "")
    stats = fub.push_approved_leads(db)
    assert stats["failed"] == 0 and stats["pushed"] == 0
    assert "FUB_API_KEY" in stats["error"]
    assert collect_stage_errors({"fub_push": stats}) == ["fub_push"]
    assert db.execute(
        "SELECT status FROM leads WHERE prop_id='7204'").fetchone()["status"] == "awaiting_approval"


def test_fub_search_failure_never_creates_a_duplicate(db, monkeypatch):
    """A FUB outage during dedupe must abort the push, not create a new person."""
    from seller_finder import fub

    lead_id = _make_awaiting_lead(db, "7101", emails='["dup@x.com"]')
    lead = dict(db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "FUB_API_KEY", "fake")
    monkeypatch.setattr(fub, "generate_summary_note", lambda lead: "")

    created = {"n": 0}

    class DeadSession:
        auth = None
        headers = {}
        def update(self, *a, **k):
            return None
        def get(self, *a, **k):
            raise OSError("FUB unreachable")
        def post(self, *a, **k):
            created["n"] += 1
            raise AssertionError("must not create a person when dedupe failed")
    monkeypatch.setattr(fub.requests, "Session", lambda: DeadSession())

    assert fub.push_lead(db, lead) is None
    assert created["n"] == 0


def test_fub_push_failure_leaves_lead_for_retry(db, monkeypatch):
    """Failed pushes stay awaiting_approval so the next run retries them."""
    from seller_finder import fub
    _make_awaiting_lead(db, "7102", emails='["a@b.com"]')
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "FUB_API_KEY", "fake")
    monkeypatch.setattr(fub, "push_lead", lambda conn, lead: None)
    stats = fub.auto_push_leads(db)
    assert stats["failed"] == 1 and stats["pushed"] == 0
    row = db.execute("SELECT status, fub_person_id FROM leads WHERE prop_id='7102'").fetchone()
    assert row["status"] == "awaiting_approval" and row["fub_person_id"] is None


def test_event_signal_expires_after_retention_window(db, monkeypatch):
    """A one-off foreclosure notice must NOT keep its +30 forever.

    Regression: retention was measured from leads.updated_at, which
    compute_scores rewrites every run, so age was always ~0 and the signal
    never expired.
    """
    monkeypatch.setitem(config.SETTINGS, "event_signal_retention_days", 120)
    _insert_parcel(db, prop_id="2101", absentee=1)
    compute_scores(db, [{"county": "bexar", "prop_id": "2101", "kind": "mortgage",
                         "doc_number": "d1", "month": 8, "year": 2026}], [], {})
    assert db.execute(
        "SELECT score FROM leads WHERE prop_id='2101'").fetchone()["score"] == 60

    # Age the stored signal past the retention window (simulating ~1 year of
    # weekly runs), leaving updated_at fresh exactly as real runs do.
    row = db.execute("SELECT signals FROM leads WHERE prop_id='2101'").fetchone()
    signals = json.loads(row["signals"])
    for s in signals:
        if s["signal"] == "preforeclosure":
            s["observed_at"] = "2025-01-01"
    db.execute("UPDATE leads SET signals=?, updated_at=? WHERE prop_id='2101'",
               (json.dumps(signals), now_iso()))
    db.commit()

    compute_scores(db, [], [], {})
    lead = db.execute("SELECT score, signals, status FROM leads WHERE prop_id='2101'").fetchone()
    assert lead["score"] == config.SCORE_ABSENTEE  # foreclosure points dropped
    assert "preforeclosure" not in {s["signal"] for s in json.loads(lead["signals"])}


def test_event_signal_survives_inside_retention_window(db, monkeypatch):
    """The carry-forward itself must keep working — no demotion between runs."""
    monkeypatch.setitem(config.SETTINGS, "event_signal_retention_days", 120)
    _insert_parcel(db, prop_id="2102", absentee=1)
    compute_scores(db, [{"county": "bexar", "prop_id": "2102", "kind": "mortgage",
                         "doc_number": "d2", "month": 8, "year": 2026}], [], {})
    for _ in range(3):  # three more runs with no notice in the feed
        compute_scores(db, [], [], {})
    lead = db.execute("SELECT score, status FROM leads WHERE prop_id='2102'").fetchone()
    assert lead["score"] == 60 and lead["status"] == "qualified"


def test_truncated_exemption_feed_does_not_mass_flag_homestead_removed(db, monkeypatch):
    """A short ArcGIS page must not report thousands of homestead removals —
    absentee(30) + homestead_removed(10) == the 40 trace threshold, so this
    would convert straight into skip-trace spend and FUB pushes."""
    from seller_finder.sources import exemptions

    monkeypatch.setitem(config.SETTINGS, "exemption_sources",
                        {"bexar": {"url": "https://example.invalid/q"}})
    ts = now_iso()
    db.executemany(
        "INSERT INTO exempt_parcels (county, prop_id, exempts, last_seen_at) VALUES (?,?,?,?)",
        [("bexar", str(i), "HS", ts) for i in range(100)])
    db.commit()

    # Feed returns only 10 of the 100 known rows (truncated page)
    monkeypatch.setattr(exemptions, "fetch_exemptions",
                        lambda county: iter([(str(i), "HS") for i in range(10)]))
    stats = exemptions.sync_county(db, "bexar")
    assert stats["homestead_removed"] == []
    assert stats.get("truncated_feed") is True
    # Previous snapshot preserved, not overwritten with the partial pull
    assert db.execute(
        "SELECT COUNT(*) c FROM exempt_parcels WHERE county='bexar'").fetchone()["c"] == 100


def test_real_homestead_removal_still_detected(db, monkeypatch):
    """Guard against over-correction: a full pull must still diff normally."""
    from seller_finder.sources import exemptions

    monkeypatch.setitem(config.SETTINGS, "exemption_sources",
                        {"bexar": {"url": "https://example.invalid/q"}})
    ts = now_iso()
    db.executemany(
        "INSERT INTO exempt_parcels (county, prop_id, exempts, last_seen_at) VALUES (?,?,?,?)",
        [("bexar", str(i), "HS", ts) for i in range(10)])
    db.commit()
    # 9 of 10 still have HS; parcel "0" lost it
    monkeypatch.setattr(exemptions, "fetch_exemptions",
                        lambda county: iter([(str(i), "HS") for i in range(1, 10)]))
    stats = exemptions.sync_county(db, "bexar")
    assert stats["homestead_removed"] == ["0"]


def test_healthcheck_pings_fail_endpoint_on_failure(monkeypatch):
    from seller_finder import health
    seen = {}
    monkeypatch.setattr(config, "HEALTHCHECK_URL", "https://hc.example/abc")
    monkeypatch.setattr(config, "DRY_RUN", False)

    class Resp:
        status_code = 200
    monkeypatch.setattr(health.requests, "get",
                        lambda url, **k: (seen.update(url=url), Resp())[1])

    health.ping_healthcheck(failed=False)
    assert seen["url"] == "https://hc.example/abc"
    health.ping_healthcheck(failed=True)
    assert seen["url"] == "https://hc.example/abc/fail"


def test_collect_stage_errors_finds_swallowed_failures():
    from seller_finder.health import collect_stage_errors
    assert collect_stage_errors({}) == []
    assert collect_stage_errors({
        "counties": {"bexar": {"parcels": {"rows": 10, "kept": 8}},
                     "travis": {"parcels": {"error": "403 blocked"},
                                "preforeclosure": {"error": "feed down"}}},
        "fub_push": {"error": "FUB 500"},
        "scoring": {"candidates": 5},
    }) == ["parcels:travis", "preforeclosure:travis", "fub_push"]


def test_healthy_run_reports_no_failures():
    """Over-correction guard: a normal run must stay clean, including a run
    with a partial (not total) skip-trace error rate and zero pushes because
    everything was legitimately held."""
    from seller_finder.health import collect_stage_errors
    assert collect_stage_errors({
        "counties": {"bexar": {"parcels": {"rows": 700000, "kept": 400000,
                                           "absentee": 150000},
                               "exemptions": {"updated": 405000, "homestead": 400000},
                               "preforeclosure": {"notices": 457, "matched": 50}}},
        "scoring": {"candidates": 180000, "qualified": 60},
        "skiptrace": {"eligible": 60, "traced": 55, "matched": 40, "errors": 5},
        "fub_push": {"pushed": 0, "held_no_contact": 40, "failed": 0, "total": 40},
        "deeds": {"files": 0, "rows": 0, "file_errors": []},
    }) == []


def test_total_skiptrace_failure_is_a_run_failure():
    """The 403 incident: 50 eligible leads, every trace errored, nothing cached.

    This exited 0 and pinged the dead-man's switch GREEN — the failure was
    visible only to someone reading the diagnostics table row by row.
    """
    from seller_finder.health import collect_stage_errors
    failures = collect_stage_errors({
        "counties": {"bexar": {"parcels": {"rows": 700000, "kept": 400000}}},
        "skiptrace": {"eligible": 50, "traced": 0, "matched": 0, "no_match": 0,
                      "errors": 50, "top_error": "HTTP 403: Forbidden"},
    })
    assert "skiptrace-all-errors" in failures


def test_total_fub_push_failure_is_a_run_failure():
    from seller_finder.health import collect_stage_errors
    assert "fub_push-all-failed" in collect_stage_errors({
        "fub_push": {"pushed": 0, "held_no_contact": 0, "failed": 42, "total": 42}})


def test_zero_row_parcel_sync_is_a_run_failure():
    """A blocked mirror / changed GDB schema yields an empty candidate
    universe. Every downstream count is then legitimately 0 and the run looks
    like a quiet week."""
    from seller_finder.health import collect_stage_errors
    assert collect_stage_errors({
        "counties": {"bexar": {"parcels": {"rows": 0, "kept": 0, "absentee": 0}}},
    }) == ["parcels-empty:bexar"]
    # rows arrived but every one was filtered out — a field-name change
    assert collect_stage_errors({
        "counties": {"comal": {"parcels": {"rows": 103537, "kept": 0}}},
    }) == ["parcels-empty:comal"]


def test_truncated_exemption_feed_is_a_run_failure():
    """The guard keeps the old snapshot (correct) but that means the signal
    silently did not run — which must not be reported as a healthy stage."""
    from seller_finder.health import collect_stage_errors
    assert collect_stage_errors({
        "counties": {"bexar": {"parcels": {"kept": 10},
                               "exemptions": {"updated": 0, "truncated_feed": True}}},
    }) == ["exemptions-truncated:bexar"]


def test_unreadable_inbox_file_is_a_run_failure():
    """A county export Peter committed by hand that fails to parse used to be
    a single ERROR log line in a 10,000-line Actions log."""
    from seller_finder.health import collect_stage_errors
    assert collect_stage_errors({
        "deeds": {"files": 0, "rows": 0,
                  "file_errors": ["deeds_bexar.csv: 'utf-8' codec can't decode"]},
    }) == ["deeds-inbox-unreadable"]


def test_run_summary_leads_with_the_failure_verdict(db, tmp_path, monkeypatch):
    """The verdict must be at the TOP of the job summary, above the counts —
    the counts are exactly what look reassuring when a stage produced nothing."""
    from seller_finder import review
    monkeypatch.setattr(config, "REVIEW_DIR", tmp_path / "review")
    out = review.write_review_files(db, run_stats={
        "counties": {}, "stage_failures": ["skiptrace-all-errors", "parcels-empty:bexar"],
        "skiptrace": {"errors": 50, "traced": 0, "top_error": "HTTP 403: Forbidden"}})
    md = (tmp_path / "review" / "run_summary.md").read_text()
    assert "STAGE(S) FAILED" in md
    assert md.index("STAGE(S) FAILED") < md.index("Score breakdown")
    assert "skiptrace-all-errors" in md and "parcels-empty:bexar" in md
    assert out["pending"] == 0


def test_run_summary_says_healthy_when_clean(db, tmp_path, monkeypatch):
    from seller_finder import review
    monkeypatch.setattr(config, "REVIEW_DIR", tmp_path / "review")
    review.write_review_files(db, run_stats={"counties": {}, "stage_failures": []})
    md = (tmp_path / "review" / "run_summary.md").read_text()
    assert "All pipeline stages healthy" in md


def test_push_approved_does_not_reset_the_dead_mans_switch(monkeypatch):
    """run_push_approved is manual and shares HEALTHCHECK_URL with the crons.

    Pinging it green would reset the switch, masking the one thing it exists
    to catch: the weekly cron silently no longer firing.
    """
    import subprocess
    from pathlib import Path as _P
    repo = _P(__file__).resolve().parents[1]
    src = (repo / "run_push_approved.py").read_text()
    assert "ping_healthcheck" not in src, (
        "run_push_approved.py must not ping the shared healthcheck — a manual "
        "retry would reset the dead-man's switch for the scheduled runs")
    # And it must still fail loudly on its own terms.
    assert "collect_stage_errors" in src and "return 1" in src


def test_mirror_asset_key_cleared_when_download_fails(monkeypatch, tmp_path, clean_asset_key):
    """A stale key would let a daily run skip owner-change bookkeeping against
    data that actually came from the TxGIO fallback."""
    from seller_finder.sources import parcels

    clean_asset_key["bexar"] = "999:888"
    monkeypatch.setattr(parcels, "_github_token", lambda: "")
    assert parcels._download_from_mirror("bexar", tmp_path / "x.zip") is False
    assert "bexar" not in parcels._LAST_ASSET_KEY


def test_warm_tier_leads_are_never_traced_even_after_rescoring(db, monkeypatch):
    """Budget rail: warm rows carry no lead row, so no trace can be charged."""
    from seller_finder.skiptrace import tracer
    _insert_parcel(db, prop_id="8101", absentee=1)
    for _ in range(3):
        compute_scores(db, [], [], {})
    assert db.execute("SELECT COUNT(*) c FROM warm_leads").fetchone()["c"] == 1
    assert db.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"] == 0

    class ExplodingProvider:
        name = "boom"
        def trace_batch(self, reqs):
            raise AssertionError("warm-tier lead reached the paid API")

    monkeypatch.setattr(tracer, "get_provider", lambda name="x": ExplodingProvider())
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "BATCHDATA_API_KEY", "fake-key")
    stats = tracer.trace_qualified_leads(db)
    assert stats["eligible"] == 0 and stats["traced"] == 0


def test_budget_is_enforced_at_the_api_call_site(db, monkeypatch):
    """The cap must bound what is SENT to the provider, not just what is
    reported — assert on the request count the provider actually received."""
    from seller_finder.skiptrace import tracer
    for i in range(6):
        _insert_parcel(db, prop_id=f"95{i}", owner=f"CAPOWNER{i} TEST",
                       situs=f"{i} BUDGET LN", mail=f"{i} FAR AWAY RD")
        compute_scores(db, [{"county": "bexar", "prop_id": f"95{i}",
                             "kind": "mortgage", "doc_number": f"b{i}",
                             "month": 8, "year": 2026}], [], {})
    sent = {"n": 0}

    class CountingProvider:
        name = "count"
        def trace_batch(self, reqs):
            from seller_finder.skiptrace.base import SkipTraceResult
            sent["n"] += len(reqs)
            return [SkipTraceResult(matched=True, emails=["a@b.com"],
                                    provider="count") for _ in reqs]

    monkeypatch.setattr(tracer, "get_provider", lambda name="x": CountingProvider())
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "BATCHDATA_API_KEY", "fake-key")
    monkeypatch.setattr(config, "MAX_SKIP_TRACES_PER_RUN", 3)
    stats = tracer.trace_qualified_leads(db)
    assert sent["n"] == 3, "budget must cap the provider request, not just the stats"
    assert stats["traced"] == 3 and stats["budget_skipped"] == 3

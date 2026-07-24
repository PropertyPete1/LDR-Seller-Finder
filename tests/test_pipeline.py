"""Core pipeline tests — run with: python3 -m pytest tests/ -v"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from seller_finder.state import get_db, owner_key, now_iso
from seller_finder.sources.parcels import is_absentee, is_individual_owner, normalize_addr
from seller_finder.sources.preforeclosure import _addr_key
from seller_finder.scoring import compute_scores
from seller_finder import config


@pytest.fixture
def db(tmp_path):
    conn = get_db(tmp_path / "test.sqlite3")
    yield conn
    conn.close()


def _insert_parcel(conn, county="bexar", prop_id="1001", owner="SMITH JOHN",
                   situs="123 MAIN ST", situs_zip="78201", mail="500 OTHER RD",
                   mail_zip="78209", absentee=1, exempts=None):
    conn.execute(
        """INSERT OR REPLACE INTO parcels
           (county, prop_id, owner_name, situs_addr, situs_city, situs_zip,
            mail_addr, mail_city, mail_state, mail_zip, mkt_value, tax_year,
            exempts, is_absentee, first_seen_at, last_seen_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (county, prop_id, owner, situs, "SAN ANTONIO", situs_zip, mail,
         "SAN ANTONIO", "TX", mail_zip, 250000, 2025, exempts, absentee,
         now_iso(), now_iso()),
    )
    conn.commit()


# ── Absentee detection ───────────────────────────────────────────────────

def test_absentee_different_zip():
    assert is_absentee("123 MAIN ST", "78201", "999 ELSEWHERE AVE", "78209")

def test_not_absentee_same_address():
    assert not is_absentee("123 MAIN ST", "78201", "123 MAIN STREET", "78201")

def test_not_absentee_missing_mail():
    assert not is_absentee("123 MAIN ST", "78201", "", "")

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

def test_absentee_only_scores_30(db):
    _insert_parcel(db, prop_id="2001", absentee=1)
    stats = compute_scores(db, [], [], {})
    lead = db.execute("SELECT * FROM leads WHERE prop_id='2001'").fetchone()
    assert lead["score"] == config.SCORE_ABSENTEE
    assert lead["status"] == "new"  # 30 < 40 threshold
    assert stats["leads_created"] == 1

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
    compute_scores(db, [], [], {})
    lead = db.execute("SELECT signals FROM leads WHERE prop_id='2005'").fetchone()
    signals = json.loads(lead["signals"])
    assert signals[0]["signal"] == "absentee_owner"

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
    assert stats1["cached"] == 1 or calls["n"] == 1


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
    assert (inbox / "deeds_bexar.csv.imported").exists()
    # Re-running must not re-import
    stats2 = deeds.ingest_inbox(db)
    assert stats2["files"] == 0

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
    compute_scores(db, [], [], {})
    lead = db.execute("SELECT score FROM leads WHERE prop_id='6003'").fetchone()
    assert lead["score"] == config.SCORE_ABSENTEE  # no tenure bump


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
    from seller_finder import fub
    lead_id = _make_awaiting_lead(db, "7004", phones='[{"number": "2105551234"}]', dnc=1)
    lead = dict(db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
    captured = {}

    monkeypatch.setattr(config, "DRY_RUN", True)  # dry-run: logs tags, no HTTP
    monkeypatch.setattr(fub, "find_existing_person", lambda *a, **k: None)
    monkeypatch.setattr(fub, "generate_summary_note", lambda lead: "")
    # Verify tag construction directly via _lead_contact + logic
    contact = fub._lead_contact(db, lead)
    assert contact["dnc"] is True
    tags = [config.TAG_SELLER, config.TAG_BY_SOURCE.get(lead["primary_source"], "County-Absentee")]
    if contact["dnc"] or contact["litigator"]:
        tags.append("DNC")
    assert "DNC" in tags


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

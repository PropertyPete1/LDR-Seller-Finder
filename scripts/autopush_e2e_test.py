#!/usr/bin/env python3
"""E2E simulation of the auto-push weekly flow (FUB HTTP mocked, everything
else real): seed parcels → score → trace (fake provider) → auto-push → review.

Verifies:
  * contactable leads are auto-pushed with correct tags (incl. DNC)
  * uncontactable leads are held, never pushed
  * dedupe path is exercised (existing person → tag update, no create)
  * pending_leads.csv records pushed/held statuses
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

from seller_finder import config
config.DRY_RUN = False
config.FUB_API_KEY = "test-key"
config.BATCHDATA_API_KEY = "test-key"
config.ANTHROPIC_API_KEY = ""  # skip Claude in this test

from seller_finder.state import get_db, now_iso
from seller_finder.scoring import compute_scores
from seller_finder.skiptrace import tracer
from seller_finder.skiptrace.base import SkipTraceResult
from seller_finder import fub
from seller_finder.review import stage_traced_leads, write_review_files

DB = config.DATA_DIR / "autopush_test.sqlite3"
DB.unlink(missing_ok=True)
conn = get_db(DB)

# 1. Seed 3 parcels: contactable, uncontactable, DNC-flagged
rows = [
    ("9001", "SMITH JOHN", "111 ALPHA ST"),
    ("9002", "JONES MARY", "222 BRAVO AVE"),
    ("9003", "BROWN TED", "333 CHARLIE DR"),
]
for pid, owner, situs in rows:
    conn.execute(
        """INSERT INTO parcels (county, prop_id, owner_name, situs_addr, situs_city,
           situs_zip, mail_addr, mail_city, mail_state, mail_zip, mkt_value, tax_year,
           is_absentee, first_seen_at, last_seen_at)
           VALUES ('bexar',?,?,?,'SAN ANTONIO','78201',?,'AUSTIN','TX','78701',
                   250000,2025,1,?,?)""",
        (pid, owner, situs, f"MAIL {pid} RD", now_iso(), now_iso()))
conn.commit()

fc = [{"county": "bexar", "prop_id": p, "kind": "mortgage", "doc_number": "1",
       "month": 8, "year": 2026} for p, _, _ in rows]
compute_scores(conn, fc, [], {})

# 2. Trace with a fake provider: 9001 full contact, 9002 nothing, 9003 phone + DNC
RESULTS = {
    "SMITH": SkipTraceResult(matched=True, emails=["john@x.com"],
                             phones=[{"number": "2105550001"}], provider="fake"),
    "JONES": SkipTraceResult(matched=True, emails=[], phones=[], provider="fake"),
    "BROWN": SkipTraceResult(matched=True, emails=[],
                             phones=[{"number": "2105550003"}], dnc=True, provider="fake"),
}

class FakeProvider:
    name = "fake"
    def trace_batch(self, reqs):
        return [RESULTS[r.owner_last] for r in reqs]

tracer.get_provider = lambda name="x": FakeProvider()
t = tracer.trace_qualified_leads(conn)
assert t["traced"] == 3, t
stage_traced_leads(conn)

# 3. Auto-push with mocked FUB HTTP
created, tagged, notes = [], [], []

class FakeResp:
    status_code = 200
    def __init__(self, payload): self._p = payload
    def raise_for_status(self): pass
    def json(self): return self._p

class FakeSession:
    def __init__(self): self.auth = None; self.headers = {}
    def get(self, url, **kw):
        if "/people/" in url:
            return FakeResp({"id": 777, "tags": ["Old Tag"]})
        # people search: pretend BROWN already exists in FUB (dedupe path)
        params = kw.get("params", {})
        if params.get("phone") == "2105550003":
            return FakeResp({"people": [{"id": 777}]})
        return FakeResp({"people": []})
    def post(self, url, json=None, **kw):
        if "/people" in url:
            created.append(json); return FakeResp({"id": 100 + len(created)})
        notes.append(json); return FakeResp({})
    def put(self, url, json=None, **kw):
        tagged.append(json); return FakeResp({})

with patch.object(fub.requests, "Session", FakeSession):
    stats = fub.auto_push_leads(conn)

print("\nauto-push stats:", stats)
assert stats["pushed"] == 2, stats            # SMITH created, BROWN deduped+tagged
assert stats["held_no_contact"] == 1, stats   # JONES held
assert len(created) == 1 and created[0]["firstName"] == "John"
assert "DNC" not in created[0]["tags"]        # SMITH not DNC
assert tagged and "DNC" in tagged[0]["tags"]  # BROWN got DNC tag merged
assert len(notes) == 2                        # both pushed leads got notes

# 4. Review CSV records outcomes
out = write_review_files(conn)
csv_text = (config.REVIEW_DIR / "pending_leads.csv").read_text()
assert "pushed" in csv_text and "held_no_contact" in csv_text
print("\nreview stats:", {k: v for k, v in out.items() if k not in ("csv", "summary")})

statuses = {r["prop_id"]: r["status"] for r in conn.execute("SELECT prop_id, status FROM leads")}
print("lead statuses:", statuses)
assert statuses == {"9001": "pushed", "9002": "held_no_contact", "9003": "pushed"}
print("\n=== AUTO-PUSH E2E: ALL ASSERTIONS PASSED ===")
DB.unlink(missing_ok=True)

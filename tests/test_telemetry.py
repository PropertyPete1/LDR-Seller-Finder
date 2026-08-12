"""Seller telemetry tests — run with: python3 -m pytest tests/test_telemetry.py -v

These are written the way the rest of this suite is: each one fails if the
production behaviour regresses, not if an implementation detail moves. The
behaviours under test are the ones that make the published numbers TRUSTWORTHY —
absent-vs-zero, read-only access, atomicity, idempotent merges, and no PII.
"""
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from seller_finder.state import get_db
from seller_finder import telemetry


CT = telemetry.CT


@pytest.fixture
def db_path(tmp_path):
    """A real state DB, created through the production schema."""
    path = tmp_path / "state.sqlite3"
    conn = get_db(path, parcels_cache=tmp_path / "cache.sqlite3")
    conn.close()
    return path


def _at(day: str, hour: int = 6, minute: int = 0) -> str:
    """A CT-local ISO timestamp the way state.now_iso() writes them."""
    date = dt.date.fromisoformat(day)
    return dt.datetime(date.year, date.month, date.day, hour, minute,
                       tzinfo=CT).isoformat(timespec="seconds")


def _record(path: Path, run_type: str, finished: str, stats: dict) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO runs (run_type, started_at, finished_at, stats) VALUES (?,?,?,?)",
        (run_type, finished, finished, json.dumps(stats)),
    )
    conn.commit()
    conn.close()


def _now(day: str, hour: int = 12) -> dt.datetime:
    date = dt.date.fromisoformat(day)
    return dt.datetime(date.year, date.month, date.day, hour, 0, tzinfo=CT)


def _healthy_stats(**overrides) -> dict:
    stats = {
        "dry_run": False,
        "counties": {
            "bexar": {
                "parcels": {"county": "bexar", "rows": 900, "kept": 800, "absentee": 120},
                "preforeclosure": {"notices": 40, "matched": 9},
            },
            "comal": {
                "parcels": {"county": "comal", "rows": 250, "kept": 200, "absentee": 30},
                "preforeclosure": {"notices": 10, "matched": 1},
            },
        },
        "scoring": {"candidates": 1000, "qualified": 12, "warm": 40},
        "skiptrace": {"eligible": 12, "cached": 3, "traced": 9, "matched": 7},
        "fub_push": {"pushed": 5, "held_no_contact": 2, "failed": 0, "total": 7},
    }
    stats.update(overrides)
    return stats


def _write(db_path, day: str, out_dir) -> dict:
    return telemetry.write_seller_telemetry(db_path, out_dir, now=_now(day))


# ── Read-only access to the encrypted state DB ──────────────────────────────

def test_state_db_is_opened_read_only(db_path):
    """Telemetry must not be able to mutate the state the next run resumes from."""
    conn = telemetry.open_readonly(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO runs (run_type, started_at) VALUES ('x','y')")
            conn.commit()
    finally:
        conn.close()


def test_writing_telemetry_does_not_modify_the_state_db(db_path, tmp_path):
    _record(db_path, "daily_pull", _at("2026-08-11"), _healthy_stats())
    before = db_path.read_bytes()

    assert _write(db_path, "2026-08-11", tmp_path / "out")["ok"] is True

    assert db_path.read_bytes() == before, "telemetry mutated the state DB"


# ── THE RULE: absent ≠ zero ─────────────────────────────────────────────────

def test_errored_stage_omits_its_counter_rather_than_publishing_zero(db_path, tmp_path):
    """The whole point of the file. A blocked mirror must not read as 'scanned 0'."""
    stats = _healthy_stats()
    stats["counties"]["bexar"]["parcels"] = {"error": "mirror download 403"}
    stats["counties"]["comal"]["parcels"] = {"error": "mirror download 403"}
    _record(db_path, "daily_pull", _at("2026-08-11"), stats)

    today = _write(db_path, "2026-08-11", tmp_path / "out")["stats"]["today"]

    assert "parcels_scanned" not in today, "an errored stage published a fabricated zero"
    assert today["leads_scored"] == 1000, "unrelated stages must still report"
    assert "parcels" in today["incomplete_stages"]


def test_a_stage_that_ran_and_found_nothing_publishes_a_counted_zero(db_path, tmp_path):
    """Zero is a fact when we looked. It must not be suppressed like unknown."""
    stats = _healthy_stats()
    stats["fub_push"] = {"pushed": 0, "held_no_contact": 0, "failed": 0, "total": 0}
    _record(db_path, "daily_pull", _at("2026-08-11"), stats)

    today = _write(db_path, "2026-08-11", tmp_path / "out")["stats"]["today"]

    assert today["leads_pushed_fub"] == 0


def test_a_keyless_fub_push_is_not_reported_as_a_push_of_nothing(db_path, tmp_path):
    """The disclosed gap, closed. With FUB_API_KEY unset, auto_push_leads pushes
    nothing and reports `skipped_no_api_key` — the leads it left in
    awaiting_approval. That is "we never looked", not "we looked and there was
    nothing to do", so the counter is omitted and the stage is named incomplete.
    """
    stats = _healthy_stats()
    stats["fub_push"] = {"pushed": 0, "held_no_contact": 0, "failed": 0, "total": 0,
                         "skipped_no_api_key": 7}
    _record(db_path, "weekly_pull", _at("2026-08-11"), stats)

    today = _write(db_path, "2026-08-11", tmp_path / "out")["stats"]["today"]

    assert "leads_pushed_fub" not in today, \
        "a stage that never ran published a fabricated zero"
    assert "fub_push" in today["incomplete_stages"]


def test_a_push_that_had_nothing_to_push_still_publishes_its_zero(db_path, tmp_path):
    """The other half of the same distinction: the key IS set, the stage ran, and
    there were no leads awaiting. Zero is a fact, and the marker is 0."""
    stats = _healthy_stats()
    stats["fub_push"] = {"pushed": 0, "held_no_contact": 0, "failed": 0, "total": 0,
                         "skipped_no_api_key": 0}
    _record(db_path, "weekly_pull", _at("2026-08-11"), stats)

    today = _write(db_path, "2026-08-11", tmp_path / "out")["stats"]["today"]

    assert today["leads_pushed_fub"] == 0
    assert "fub_push" not in today.get("incomplete_stages", [])


def test_a_keyless_fub_push_produces_no_push_event(db_path, tmp_path):
    """"0 leads pushed to FUB" in the event log reads as a push that found
    nothing. A skipped stage says nothing at all — and it is not a failure
    either: unsetting the key is how README describes getting a manual approval
    gate back, and a daily "failed" event for a supported configuration is noise
    that trains people to ignore the log."""
    stats = _healthy_stats()
    stats["fub_push"] = {"pushed": 0, "held_no_contact": 0, "failed": 0, "total": 0,
                         "skipped_no_api_key": 7}
    _record(db_path, "weekly_pull", _at("2026-08-11"), stats)

    log = _write(db_path, "2026-08-11", tmp_path / "out")["log"]

    assert not [e for e in log if e["stage"] == "fub_push"]


def test_a_day_with_no_runs_is_empty_not_a_row_of_zeros(db_path, tmp_path):
    """'The cron did not fire' and 'the cron found nothing' are different facts."""
    _record(db_path, "daily_pull", _at("2026-08-11"), _healthy_stats())

    stats = _write(db_path, "2026-08-11", tmp_path / "out")["stats"]

    assert stats["today"], "today had a run and must report"
    assert stats["yesterday"] == {}, "a day with no runs must publish nothing at all"


def test_missing_stage_key_is_unknown_not_zero(db_path, tmp_path):
    """A daily run skips exemptions/divorce; a stage that never ran says nothing."""
    stats = _healthy_stats()
    del stats["skiptrace"]
    _record(db_path, "daily_pull", _at("2026-08-11"), stats)

    today = _write(db_path, "2026-08-11", tmp_path / "out")["stats"]["today"]

    assert "skip_traces" not in today
    assert "skiptrace" not in today.get("incomplete_stages", []), \
        "a stage that never ran is not a failed stage"


def test_partial_day_sums_only_what_is_known_and_says_it_is_partial(db_path, tmp_path):
    """One good run + one broken run = a floor, flagged as such. Never a silent total."""
    good = _healthy_stats()
    broken = _healthy_stats()
    broken["counties"]["bexar"]["parcels"] = {"error": "boom"}
    broken["counties"]["comal"]["parcels"] = {"error": "boom"}
    _record(db_path, "daily_pull", _at("2026-08-11", hour=6), good)
    _record(db_path, "daily_pull", _at("2026-08-11", hour=9), broken)

    today = _write(db_path, "2026-08-11", tmp_path / "out")["stats"]["today"]

    assert today["parcels_scanned"] == 1000, "must sum the runs that did report"
    assert today["incomplete_stages"] == ["parcels"]


def test_unparseable_stats_row_contributes_nothing(db_path, tmp_path):
    """A row we cannot read is not a row of zeros."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO runs (run_type, started_at, finished_at, stats) VALUES (?,?,?,?)",
        ("daily_pull", _at("2026-08-11"), _at("2026-08-11"), "{not json"),
    )
    conn.commit()
    conn.close()

    stats = _write(db_path, "2026-08-11", tmp_path / "out")["stats"]

    assert stats["today"] == {}
    # And it must not be dressed up as a run we understand. Treating the row as
    # an empty-but-valid run leaves today's counters empty either way, so the
    # only place the difference shows is here.
    assert "last_run_type" not in stats, \
        "a run whose stats will not parse must not be published as the last known run"


# ── Counting rules ──────────────────────────────────────────────────────────

def test_counters_sum_across_counties_and_runs(db_path, tmp_path):
    _record(db_path, "daily_pull", _at("2026-08-11", hour=6), _healthy_stats())
    _record(db_path, "daily_pull", _at("2026-08-11", hour=9), _healthy_stats())

    today = _write(db_path, "2026-08-11", tmp_path / "out")["stats"]["today"]

    assert today["parcels_scanned"] == 2000          # (800 + 200) x 2
    assert today["preforeclosure_matches"] == 20     # (9 + 1) x 2
    assert today["preforeclosure_notices"] == 100    # (40 + 10) x 2
    assert today["leads_scored"] == 2000
    assert today["leads_qualified"] == 24
    assert today["skip_traces"] == 18
    assert today["leads_pushed_fub"] == 10


def test_skip_traces_counts_billable_traces_not_cache_hits(db_path, tmp_path):
    """skip_traces maps to spend. Counting `cached` would inflate it for free work."""
    stats = _healthy_stats()
    stats["skiptrace"] = {"eligible": 50, "cached": 41, "traced": 9, "matched": 7}
    _record(db_path, "daily_pull", _at("2026-08-11"), stats)

    today = _write(db_path, "2026-08-11", tmp_path / "out")["stats"]["today"]

    assert today["skip_traces"] == 9


def test_dry_run_intent_is_never_counted_as_delivery(db_path, tmp_path):
    """A dry run pushed nothing. dry_run_would_push records intent, not outcome."""
    stats = _healthy_stats(dry_run=True)
    stats["fub_push"] = {"pushed": 0, "held_no_contact": 0, "failed": 0,
                         "total": 7, "dry_run_would_push": 7}
    stats["skiptrace"] = {"eligible": 12, "cached": 0, "traced": 0, "matched": 0}
    _record(db_path, "daily_pull", _at("2026-08-11"), stats)

    published = _write(db_path, "2026-08-11", tmp_path / "out")["stats"]

    assert published["today"]["leads_pushed_fub"] == 0
    assert published["today"]["skip_traces"] == 0
    assert published["dry_run"] is True, "a dry run must be visibly a dry run"


def test_yesterday_is_bucketed_separately_from_today(db_path, tmp_path):
    yesterday = _healthy_stats()
    yesterday["scoring"] = {"candidates": 7, "qualified": 1}
    _record(db_path, "weekly_pull", _at("2026-08-10"), yesterday)
    _record(db_path, "daily_pull", _at("2026-08-11"), _healthy_stats())

    stats = _write(db_path, "2026-08-11", tmp_path / "out")["stats"]

    assert stats["today"]["leads_scored"] == 1000
    assert stats["yesterday"]["leads_scored"] == 7
    assert stats["yesterday_date"] == "2026-08-10"


def test_runs_from_older_days_are_excluded_from_both_buckets(db_path, tmp_path):
    _record(db_path, "weekly_pull", _at("2026-08-04"), _healthy_stats())

    stats = _write(db_path, "2026-08-11", tmp_path / "out")["stats"]

    assert stats["today"] == {}
    assert stats["yesterday"] == {}


def test_late_night_run_lands_on_the_day_it_finished(db_path, tmp_path):
    """CT-local bucketing — not UTC, which would push a 7pm CT run to tomorrow."""
    _record(db_path, "daily_pull", _at("2026-08-11", hour=19), _healthy_stats())

    stats = _write(db_path, "2026-08-11", tmp_path / "out")["stats"]

    assert stats["today"]["leads_scored"] == 1000, "a 7pm CT run belongs to that CT day"


def test_only_contract_counters_are_published(db_path, tmp_path):
    _record(db_path, "daily_pull", _at("2026-08-11"), _healthy_stats())

    today = _write(db_path, "2026-08-11", tmp_path / "out")["stats"]["today"]

    unexpected = set(today) - set(telemetry.COUNTERS) - {"incomplete_stages"}
    assert not unexpected, f"undeclared keys leaked into the contract: {unexpected}"


# ── The event log ───────────────────────────────────────────────────────────

def test_events_cover_every_stage_and_stay_inside_the_contract(db_path, tmp_path):
    _record(db_path, "daily_pull", _at("2026-08-11"), _healthy_stats())

    log = _write(db_path, "2026-08-11", tmp_path / "out")["log"]

    assert {e["type"] for e in log} <= set(telemetry.EVENT_TYPES)
    assert {e["stage"] for e in log} >= {"parcels", "preforeclosure", "scoring",
                                         "skiptrace", "fub_push"}

    # County-scoped stages must SAY which county. Bexar and Comal scan wildly
    # different volumes; two unattributed "parcels scanned" rows are unreadable,
    # and worse, they dedupe against each other when the counts happen to match.
    by_stage = {}
    for event in log:
        by_stage.setdefault(event["stage"], []).append(event)
    assert {e.get("county") for e in by_stage["parcels"]} == {"bexar", "comal"}
    assert {e.get("county") for e in by_stage["preforeclosure"]} == {"bexar", "comal"}

    # Run-level stages must NOT invent one — omitted means "does not apply".
    for stage in ("scoring", "skiptrace", "fub_push"):
        assert all("county" not in e for e in by_stage[stage]), \
            f"{stage} is run-level and must not claim a county"


def test_failed_stage_produces_a_failure_event(db_path, tmp_path):
    stats = _healthy_stats()
    stats["fub_push"] = {"error": "FUB 503"}
    stats["stage_failures"] = ["fub_push"]
    _record(db_path, "daily_pull", _at("2026-08-11"), stats)

    log = _write(db_path, "2026-08-11", tmp_path / "out")["log"]

    failures = [e for e in log if e["type"] == "failed"]
    assert any(e["stage"] == "fub_push" for e in failures)
    assert any("FUB 503" in e["detail"] for e in failures)


def test_event_log_carries_no_homeowner_pii(db_path, tmp_path):
    """seller_log.json is committed to main. Migration v5 purged PII; this must
    not walk it back out through a status file."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO leads (county, prop_id, owner_name, property_addr, mail_addr, "
        "score, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("bexar", "1001", "SMITH JOHN", "123 MAIN ST", "500 OTHER RD", 60,
         "pushed", _at("2026-08-11"), _at("2026-08-11")),
    )
    conn.commit()
    conn.close()
    _record(db_path, "daily_pull", _at("2026-08-11"), _healthy_stats())

    out = tmp_path / "out"
    _write(db_path, "2026-08-11", out)
    blob = (out / telemetry.LOG_FILENAME).read_text()

    for secret in ("SMITH JOHN", "123 MAIN ST", "500 OTHER RD"):
        assert secret not in blob, f"{secret!r} leaked into the published event log"


def test_rerunning_the_writer_is_a_no_op_not_a_duplication(db_path, tmp_path):
    """A job that loses the push race re-runs on the winner's commit. An append
    would duplicate the winner's events every time."""
    _record(db_path, "daily_pull", _at("2026-08-11"), _healthy_stats())
    out = tmp_path / "out"

    first = _write(db_path, "2026-08-11", out)["log"]
    second = _write(db_path, "2026-08-11", out)["log"]

    assert first == second


def test_merge_preserves_history_this_run_cannot_see(db_path, tmp_path):
    """The on-disk log outlives the window the writer reads out of `runs`."""
    out = tmp_path / "out"
    out.mkdir()
    old = [{"ts": "2026-07-01T11:00:00Z", "type": "pushed", "stage": "fub_push",
            "detail": "3 leads pushed to FUB", "county": "bexar"}]
    (out / telemetry.LOG_FILENAME).write_text(json.dumps(old))
    _record(db_path, "daily_pull", _at("2026-08-11"), _healthy_stats())

    log = _write(db_path, "2026-08-11", out)["log"]

    assert old[0] in log, "a sibling's published entry was dropped"


def test_log_is_capped_newest_first(db_path, tmp_path):
    """The cap has to actually bind: seed well past MAX_LOG_ENTRIES, and make the
    seeded history OLDER than this run's events so the eviction order is visible.

    Only today's and yesterday's runs are read out of the DB, so the on-disk log
    is the only way to get past the cap — an earlier version of this test seeded
    30 entries, never reached 100, and passed with the cap removed entirely.
    """
    _record(db_path, "daily_pull", _at("2026-08-11"), _healthy_stats())
    out = tmp_path / "out"
    out.mkdir()
    seeded = [
        {"ts": f"2026-01-01T{h:02d}:{m:02d}:00Z", "type": "scanned", "stage": "parcels",
         "detail": f"entry {h}-{m}"}
        for h in range(15) for m in range(10)
    ]
    assert len(seeded) > telemetry.MAX_LOG_ENTRIES, "fixture must exceed the cap"
    (out / telemetry.LOG_FILENAME).write_text(json.dumps(seeded))

    log = _write(db_path, "2026-08-11", out)["log"]

    assert len(log) == telemetry.MAX_LOG_ENTRIES
    timestamps = [e["ts"] for e in log]
    assert timestamps == sorted(timestamps, reverse=True)
    assert timestamps[0].startswith("2026-08-11"), "this run's events must survive the cap"
    assert seeded[0] not in log, "the oldest entries must be the ones evicted"


def test_corrupt_log_on_disk_does_not_stop_publishing(db_path, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / telemetry.LOG_FILENAME).write_text("{{{ not json")
    _record(db_path, "daily_pull", _at("2026-08-11"), _healthy_stats())

    result = _write(db_path, "2026-08-11", out)

    assert result["ok"] is True
    assert result["log"], "a corrupt file must be treated as absent, not fatal"


# ── Never fail the run ──────────────────────────────────────────────────────

def test_missing_state_db_returns_failure_without_raising(tmp_path):
    result = telemetry.write_seller_telemetry(tmp_path / "nope.sqlite3", tmp_path / "out")

    assert result["ok"] is False
    assert not (tmp_path / "out" / telemetry.STATS_FILENAME).exists(), \
        "nothing must be published when the DB could not be read"


def test_db_without_a_runs_table_returns_empty_days_not_a_crash(tmp_path):
    path = tmp_path / "bare.sqlite3"
    sqlite3.connect(path).close()

    result = telemetry.write_seller_telemetry(path, tmp_path / "out", now=_now("2026-08-11"))

    assert result["ok"] is True
    assert result["stats"]["today"] == {}


def test_unwritable_output_directory_returns_failure_without_raising(db_path, tmp_path):
    _record(db_path, "daily_pull", _at("2026-08-11"), _healthy_stats())
    blocker = tmp_path / "out"
    blocker.write_text("I am a file, not a directory")

    result = telemetry.write_seller_telemetry(db_path, blocker, now=_now("2026-08-11"))

    assert result["ok"] is False


def test_cli_exit_code_is_nonzero_when_nothing_was_written(tmp_path):
    assert telemetry.main(["--db", str(tmp_path / "nope.sqlite3"),
                           "--out", str(tmp_path / "out")]) == 1


def test_cli_writes_both_files(db_path, tmp_path):
    _record(db_path, "daily_pull", _at("2026-08-11"), _healthy_stats())
    out = tmp_path / "out"

    assert telemetry.main(["--db", str(db_path), "--out", str(out)]) == 0
    assert json.loads((out / telemetry.STATS_FILENAME).read_text())
    assert json.loads((out / telemetry.LOG_FILENAME).read_text()) is not None


# ── Atomicity ───────────────────────────────────────────────────────────────

def test_no_temp_files_survive_a_successful_write(db_path, tmp_path):
    _record(db_path, "daily_pull", _at("2026-08-11"), _healthy_stats())
    out = tmp_path / "out"

    _write(db_path, "2026-08-11", out)

    assert not list(out.glob("*.tmp")), "a temp file was left for the dashboard to read"
    assert sorted(p.name for p in out.iterdir()) == \
        sorted([telemetry.STATS_FILENAME, telemetry.LOG_FILENAME])


def test_failed_write_leaves_no_temp_file_behind(tmp_path, monkeypatch):
    target = tmp_path / "status" / "seller_stats.json"

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(telemetry.json, "dump", boom)
    with pytest.raises(OSError):
        telemetry.atomic_write_json(target, {"a": 1})

    assert not list((tmp_path / "status").glob("*.tmp"))
    assert not target.exists()


def test_the_published_file_is_never_partially_written(tmp_path, monkeypatch):
    """Readers poll these files, so the destination must go from one COMPLETE
    version to the next with no observable state in between.

    That guarantee comes from a same-directory rename, which is atomic because
    the filesystem says so — not from anything single-threaded assertions on the
    final bytes can see. So this checks the mechanism directly: at the instant
    the swap happens the destination must still hold the whole PREVIOUS payload,
    the new bytes must already be complete in the temp file, and both must live
    in the same directory (a cross-filesystem rename degrades to a copy and
    stops being atomic).

    Asserting the final content instead would be tautological: replacing the
    rename with a plain copy — which genuinely does expose a half-written file —
    passes that version of the test.
    """
    target = tmp_path / "seller_stats.json"
    telemetry.atomic_write_json(target, {"date": "2026-08-11", "today": {}})

    observed = {}
    order = []
    real_replace = telemetry.os.replace
    real_fsync = telemetry.os.fsync

    def watched_fsync(fd):
        order.append("fsync")
        return real_fsync(fd)

    def watched_replace(src, dst):
        order.append("replace")
        observed["dest_before_swap"] = Path(dst).read_text()
        observed["src_at_swap"] = Path(src).read_text()
        observed["same_dir"] = Path(src).parent == Path(dst).parent
        return real_replace(src, dst)

    monkeypatch.setattr(telemetry.os, "fsync", watched_fsync)
    monkeypatch.setattr(telemetry.os, "replace", watched_replace)
    telemetry.atomic_write_json(target, {"date": "2026-08-12", "today": {"leads_scored": 3}})

    assert observed, "the destination was written without an atomic rename"
    assert json.loads(observed["dest_before_swap"])["date"] == "2026-08-11", \
        "the old version was disturbed before the swap — readers could see a partial file"
    assert json.loads(observed["src_at_swap"])["date"] == "2026-08-12", \
        "the new version was still incomplete at the moment of the swap"
    assert observed["same_dir"], "temp file is on another filesystem — the rename is not atomic"
    # Durability itself is only observable if the runner dies mid-write, which no
    # in-process test can stage. The ORDERING can be checked, and it is the half
    # that regresses: an fsync after the rename leaves a window where the file is
    # published but its bytes are not on disk.
    assert order == ["fsync", "replace"], \
        f"temp file must be fsynced before it is published, got {order}"
    assert json.loads(target.read_text())["date"] == "2026-08-12"


# ── Stats file shape ────────────────────────────────────────────────────────

def test_stats_file_reports_which_run_produced_it(db_path, tmp_path):
    _record(db_path, "weekly_pull", _at("2026-08-11", hour=6), _healthy_stats())
    _record(db_path, "daily_pull", _at("2026-08-11", hour=9), _healthy_stats())

    stats = _write(db_path, "2026-08-11", tmp_path / "out")["stats"]

    assert stats["last_run_type"] == "daily_pull", "must name the most recent run"
    assert stats["date"] == "2026-08-11"
    assert stats["last_run_iso"].endswith("Z")


def test_incomplete_stages_is_stable_across_identical_runs(db_path, tmp_path):
    """An unordered set here would produce a spurious commit on every run."""
    stats = _healthy_stats()
    stats["counties"]["bexar"]["parcels"] = {"error": "a"}
    stats["scoring"] = {"error": "b"}
    stats["skiptrace"] = {"error": "c"}
    _record(db_path, "daily_pull", _at("2026-08-11"), stats)
    out = tmp_path / "out"

    first = _write(db_path, "2026-08-11", out)["stats"]
    second = _write(db_path, "2026-08-11", out)["stats"]

    assert first["today"]["incomplete_stages"] == second["today"]["incomplete_stages"]
    assert first["today"]["incomplete_stages"] == ["parcels", "scoring", "skiptrace"]

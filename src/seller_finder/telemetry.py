"""telemetry.py — the finder reporting on itself, for LIFESTYLE ("THE FLOOR").

Writes two files under `status/` so the dashboard has live numbers instead of a
blank panel:

    status/seller_stats.json   today's + yesterday's counters
    status/seller_log.json     the 100 most recent stage-level events

READ-ONLY BY CONSTRUCTION. The state DB is opened with SQLite's `mode=ro` URI,
so this module *cannot* write to it even by accident. Every number below is
derived from rows the pipeline already wrote. This module never scans, scores,
traces or pushes; a bug in it can make a dashboard wrong, never a lead — and
never the state the next run resumes from.

─────────────────────────────────────────────────────────────────────────────
THE RULE: NEVER INVENT A NUMBER

A key that is PRESENT is a counted fact. A key that is ABSENT is "we do not
know". Zero means we looked and counted zero. Those three states are not
interchangeable, and the difference is the whole point of this file — a panel
that confidently prints "parcels scanned: 0" on a day the mirror download was
blocked is worse than one that prints nothing.

This maps exactly onto a convention the pipeline already has. Every stage in
run_daily_pull.py / run_weekly_pull.py is wrapped in try/except and stores
`{"error": ...}` in place of its counts. So:

    stage recorded counts   → the counter is present (a real number, maybe 0)
    stage recorded {"error"}→ the counter is ABSENT for that run
    stage never ran at all  → the counter is ABSENT

A day can mix the two: the Tue daily run scans Bexar fine, the Wed one 500s.
Summing only what we know would publish a confident total that is quietly
short. So a day whose stages did not all report lists them in
`incomplete_stages`, and the sum beside it is explicitly a floor, not a total.
The dashboard can render "100,000+" instead of a wrong "100,000".

─────────────────────────────────────────────────────────────────────────────
WHERE EACH NUMBER COMES FROM  (all of it out of `runs.stats`, the JSON blob
record_run() writes at the end of every run)

parcels_scanned — sum of counties[*].parcels.kept: rows that survived the
    owner/address filters and entered the candidate universe. NOT `rows` (the
    raw download count), because `kept` is the number the rest of the pipeline
    actually works from, and `kept == 0` with rows > 0 is the canonical silent
    failure this repo watches for (see health.collect_stage_errors).

preforeclosure_matches — sum of counties[*].preforeclosure.matched: notices
    that matched a parcel. `preforeclosure_notices` beside it is the raw feed
    count. Matched, not notices, is the lead-bearing number; both are kept
    because a big gap between them is how a broken address matcher shows up.

leads_scored — scoring.candidates: every parcel put through compute_scores.
    leads_qualified — scoring.qualified, the slice that crossed
    SCORE_THRESHOLD. Qualified is a SUBSET of scored, not a separate bucket;
    adding them together double-counts.

skip_traces — skiptrace.traced: NEW, billable traces. Deliberately excludes
    `cached` (already paid for) — this is the number that maps to spend, and
    skiptrace.run_cost_usd is derived from exactly this field upstream.
    Under DRY_RUN the tracer sets traced = 0 on purpose, so a dry run reports
    a true, counted zero rather than the work it would have done.

    A run with no BATCHDATA_API_KEY keeps its zero here too, and that is not
    the same call as the one made for leads_pushed_fub below: this counter
    measures SPEND, and zero traces really did cost zero. The tracer reports
    `skipped_no_api_key` beside it (how many leads advanced without contact
    info), which is what the diagnostics table reads.

leads_pushed_fub — fub_push.pushed: leads that reached Follow Up Boss and came
    back with a person id. Under DRY_RUN this stays 0 while the run records
    `dry_run_would_push` separately, which is NOT read here — intent is not
    delivery. Same reason the nurture bot's panel refuses to count dry_run_sent.

    A RUN THAT NEVER LOOKED PUBLISHES NO COUNTER. When FUB_API_KEY is unset,
    auto_push_leads pushes nothing and reports `skipped_no_api_key` — the
    number of leads left in awaiting_approval — the way the tracer already
    reports a missing BATCHDATA_API_KEY. This used to be a disclosed gap: the
    keyless run returned all zeros, which was indistinguishable from "there
    were no leads awaiting push", and both published 0. Now the keyless run
    OMITS `leads_pushed_fub` and names `fub_push` in `incomplete_stages`, which
    is THE RULE above applied to a stage that did not run. A run that pushed
    nothing because there was nothing to push still publishes a counted zero.

─────────────────────────────────────────────────────────────────────────────
NO PII IN THE EVENT LOG, ON PURPOSE

seller_log.json is committed to `main`, and this repo holds homeowner PII.
Events here are STAGE-LEVEL (county + stage + counts), never lead-level: no
owner names, no property addresses, no phone numbers. Migration v5 in state.py
already purged stored consumer profiles on exactly this reasoning — publishing
the same data back out through a status file would walk it straight back in.

─────────────────────────────────────────────────────────────────────────────
Both files are written temp-then-rename, in the destination directory, with an
fsync before the rename: a reader that catches a half-written file must get
invalid JSON, not a partial number.

DO NOT WRITE THESE INTO THE ACTIONS CHECKOUT. `--out` exists because the state
push used to run `git checkout state` and `git rm -rf .`; a modified tracked
file under the checkout stopped it switching branches, and the state DB then
silently stopped being persisted. That cost LDR-Automation-Clean sixteen hours
of unpersisted state on 2026-08-11. state_sync.py builds its commit with git
plumbing now and does not care about the tree — but the publish-telemetry action
still generates into a temp dir and commits the same way, because writing status
files into a checkout that other steps read from is how that class of failure
starts.
"""
import argparse
import datetime as dt
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

LOGGER = logging.getLogger("telemetry")

STATS_FILENAME = "seller_stats.json"
LOG_FILENAME = "seller_log.json"

MAX_LOG_ENTRIES = 100
DETAIL_MAX_CHARS = 160

CT = dt.timezone(dt.timedelta(hours=-6))  # replaced by zoneinfo below when available
try:  # pragma: no cover - zoneinfo is stdlib on 3.9+, this is belt and braces
    from zoneinfo import ZoneInfo

    CT = ZoneInfo("America/Chicago")
except Exception:  # pragma: no cover
    LOGGER.warning("zoneinfo unavailable — falling back to fixed UTC-6 for day bucketing")

# The dashboard contract for an event. Six types, no others.
EVENT_TYPES = ("scanned", "matched", "scored", "traced", "pushed", "failed")

# The dashboard contract for the counters. Absent is always legal; a key
# outside this set never appears.
COUNTERS = (
    "parcels_scanned",
    "preforeclosure_notices",
    "preforeclosure_matches",
    "leads_scored",
    "leads_qualified",
    "skip_traces",
    "leads_pushed_fub",
)

# Stage names as they appear in `incomplete_stages` — the pipeline's own
# vocabulary, so a name here matches what collect_stage_errors reports.
STAGE_PARCELS = "parcels"
STAGE_PREFORECLOSURE = "preforeclosure"
STAGE_SCORING = "scoring"
STAGE_SKIPTRACE = "skiptrace"
STAGE_FUB = "fub_push"


# ── Time ────────────────────────────────────────────────────────────────────

def today_in_ct(now: dt.datetime | None = None) -> str:
    """Central-local YYYY-MM-DD. The pipeline's crons are CT, so days are CT."""
    return (now or dt.datetime.now(dt.timezone.utc)).astimezone(CT).date().isoformat()


def iso_z(value: dt.datetime | None = None) -> str:
    """UTC ISO 8601 with a Z suffix and no microseconds."""
    moment = value or dt.datetime.now(dt.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_ts(value) -> dt.datetime | None:
    """Parse a stored timestamp. Unparseable → None, never a guessed date."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CT)
    return parsed


def day_of(value) -> str | None:
    """Central-local calendar day of a stored timestamp, or None."""
    parsed = parse_ts(value)
    return parsed.astimezone(CT).date().isoformat() if parsed else None


def clip(text, limit: int = DETAIL_MAX_CHARS) -> str:
    flat = " ".join(str(text if text is not None else "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


# ── Reading the state DB (read-only, always) ────────────────────────────────

def open_readonly(db_path) -> sqlite3.Connection:
    """Open the state DB so that writing to it is impossible.

    `mode=ro` is enforced by SQLite itself, not by our discipline: any INSERT
    or UPDATE through this handle raises. That is the guarantee this module
    makes to the rest of the pipeline — telemetry shares a database with the
    thing that decides who gets called, and it must not be able to touch it.
    """
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"no state DB at {path}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def load_runs(conn: sqlite3.Connection, days: tuple[str, ...]) -> list[dict]:
    """Runs that FINISHED on one of `days`, oldest first.

    Bucketed by finished_at, not started_at: the counts in `stats` are what the
    run ended up with, so they belong to the day they were final. A run that
    starts 23:55 and finishes 00:05 counts on the later day, once.

    Reading every row and filtering in Python rather than in SQL is deliberate:
    timestamps are stored as CT-local ISO strings with an offset, and SQLite
    string comparison on those is wrong across a DST boundary.
    """
    try:
        rows = conn.execute(
            "SELECT run_type, started_at, finished_at, stats FROM runs "
            "ORDER BY id DESC LIMIT 400"
        ).fetchall()
    except sqlite3.Error as exc:
        LOGGER.warning("runs table unreadable (%s) — no runs to report", exc)
        return []

    out = []
    for run_type, started_at, finished_at, raw in rows:
        stamp = finished_at or started_at
        if day_of(stamp) not in days:
            continue
        try:
            stats = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            # A row whose stats will not parse is a row we know nothing about.
            # It is not a zero, so it contributes neither counts nor events.
            LOGGER.warning("run %s at %s has unparseable stats — skipped", run_type, stamp)
            continue
        if not isinstance(stats, dict):
            continue
        out.append({"run_type": run_type or "unknown", "finished_at": stamp, "stats": stats})
    out.reverse()
    return out


# ── stats JSON → counters ───────────────────────────────────────────────────

def _errored(section) -> bool:
    return isinstance(section, dict) and bool(section.get("error"))


def _skipped_no_api_key(section) -> bool:
    """True when a stage did nothing because its optional secret is unset.

    Not a failure — unsetting FUB_API_KEY is how README says to get a manual
    approval gate back — but not a zero either: the stage never looked. Its
    counter is omitted and the stage is named in `incomplete_stages`.
    """
    return isinstance(section, dict) and bool(section.get("skipped_no_api_key"))


def _count(section, key: str):
    """A real integer from a stage result, or None for 'we do not know'."""
    if not isinstance(section, dict) or section.get("error"):
        return None
    value = section.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def counters_from_run(stats: dict) -> tuple[dict, set]:
    """One run's counters, plus the stages that did not report.

    Returns (counters, incomplete). A counter is omitted entirely when the
    stage behind it errored or never ran — see THE RULE.
    """
    counters: dict = {}
    incomplete: set = set()

    counties = stats.get("counties") or {}
    if not isinstance(counties, dict):
        counties = {}

    # Per-county stages: a stage is complete only if EVERY county reported it.
    # One county erroring makes the day's parcel total a floor, and the caller
    # needs to know that even though the other counties' numbers are real.
    parcels_total = 0
    parcels_seen = False
    notices_total = 0
    matched_total = 0
    preforeclosure_seen = False

    for _county, sections in counties.items():
        if not isinstance(sections, dict):
            continue

        parcels = sections.get(STAGE_PARCELS)
        kept = _count(parcels, "kept")
        if kept is None:
            if parcels is not None:
                incomplete.add(STAGE_PARCELS)
        else:
            parcels_total += kept
            parcels_seen = True

        pref = sections.get(STAGE_PREFORECLOSURE)
        matched = _count(pref, "matched")
        if matched is None:
            if pref is not None:
                incomplete.add(STAGE_PREFORECLOSURE)
        else:
            matched_total += matched
            preforeclosure_seen = True
            notices = _count(pref, "notices")
            if notices is not None:
                notices_total += notices

    if parcels_seen:
        counters["parcels_scanned"] = parcels_total
    if preforeclosure_seen:
        counters["preforeclosure_matches"] = matched_total
        counters["preforeclosure_notices"] = notices_total

    # Run-level stages.
    scoring = stats.get(STAGE_SCORING)
    candidates = _count(scoring, "candidates")
    if candidates is None:
        if scoring is not None:
            incomplete.add(STAGE_SCORING)
    else:
        counters["leads_scored"] = candidates
        qualified = _count(scoring, "qualified")
        if qualified is not None:
            counters["leads_qualified"] = qualified

    skiptrace = stats.get(STAGE_SKIPTRACE)
    traced = _count(skiptrace, "traced")
    if traced is None:
        if skiptrace is not None:
            incomplete.add(STAGE_SKIPTRACE)
    else:
        counters["skip_traces"] = traced

    fub = stats.get(STAGE_FUB)
    pushed = _count(fub, "pushed")
    if pushed is None or _skipped_no_api_key(fub):
        if fub is not None:
            incomplete.add(STAGE_FUB)
    else:
        counters["leads_pushed_fub"] = pushed

    return counters, incomplete


def counters_for_day(runs: list[dict]) -> dict:
    """Sum a day's runs. Absent stays absent; partial days say so.

    A day with no runs at all returns {} — not a row of zeros. "The cron did
    not fire" and "the cron fired and found nothing" are different facts, and
    the second one is the only one that deserves a zero.
    """
    if not runs:
        return {}

    totals: dict = {}
    incomplete: set = set()
    for run in runs:
        counters, missing = counters_from_run(run["stats"])
        incomplete |= missing
        for key, value in counters.items():
            totals[key] = totals.get(key, 0) + value

    day = {key: totals[key] for key in COUNTERS if key in totals}
    if incomplete:
        # Sorted so the file is stable across runs — an unordered set here
        # would produce a spurious diff (and a spurious commit) every time.
        day["incomplete_stages"] = sorted(incomplete)
    return day


# ── stats JSON → events ─────────────────────────────────────────────────────

def _event(ts: str, type_: str, stage: str, detail: str, county: str | None = None) -> dict:
    event = {"ts": ts, "type": type_, "stage": stage, "detail": clip(detail)}
    # county is omitted, never guessed, when the fact is not county-specific —
    # omitted means "does not apply", exactly like an absent counter.
    if county:
        event["county"] = county
    return event


def events_from_run(run: dict) -> list[dict]:
    """Stage-level events for one run. No PII — see the module docstring."""
    ts = iso_z(parse_ts(run.get("finished_at")))
    stats = run.get("stats") or {}
    run_type = run.get("run_type") or "unknown"
    events: list[dict] = []

    counties = stats.get("counties") or {}
    if isinstance(counties, dict):
        for county, sections in sorted(counties.items()):
            if not isinstance(sections, dict):
                continue

            parcels = sections.get(STAGE_PARCELS)
            if _errored(parcels):
                events.append(_event(ts, "failed", STAGE_PARCELS,
                                     f"parcel sync failed: {parcels['error']}", county))
            else:
                kept = _count(parcels, "kept")
                if kept is not None:
                    absentee = _count(parcels, "absentee")
                    extra = f", {absentee} absentee" if absentee is not None else ""
                    events.append(_event(ts, "scanned", STAGE_PARCELS,
                                         f"{kept} parcels scanned{extra}", county))

            pref = sections.get(STAGE_PREFORECLOSURE)
            if _errored(pref):
                events.append(_event(ts, "failed", STAGE_PREFORECLOSURE,
                                     f"pre-foreclosure failed: {pref['error']}", county))
            else:
                matched = _count(pref, "matched")
                if matched is not None:
                    notices = _count(pref, "notices")
                    seen = f" from {notices} notices" if notices is not None else ""
                    events.append(_event(ts, "matched", STAGE_PREFORECLOSURE,
                                         f"{matched} pre-foreclosure matches{seen}", county))

    scoring = stats.get(STAGE_SCORING)
    if _errored(scoring):
        events.append(_event(ts, "failed", STAGE_SCORING, f"scoring failed: {scoring['error']}"))
    else:
        candidates = _count(scoring, "candidates")
        if candidates is not None:
            qualified = _count(scoring, "qualified")
            tail = f", {qualified} qualified" if qualified is not None else ""
            events.append(_event(ts, "scored", STAGE_SCORING,
                                 f"{candidates} leads scored{tail}"))

    skiptrace = stats.get(STAGE_SKIPTRACE)
    if _errored(skiptrace):
        events.append(_event(ts, "failed", STAGE_SKIPTRACE,
                             f"skip-trace failed: {skiptrace['error']}"))
    else:
        traced = _count(skiptrace, "traced")
        if traced is not None:
            matched = _count(skiptrace, "matched")
            cached = _count(skiptrace, "cached")
            bits = [f"{traced} skip-traces"]
            if matched is not None:
                bits.append(f"{matched} matched")
            if cached:
                bits.append(f"{cached} served from cache")
            events.append(_event(ts, "traced", STAGE_SKIPTRACE, ", ".join(bits)))

    fub = stats.get(STAGE_FUB)
    if _errored(fub):
        events.append(_event(ts, "failed", STAGE_FUB, f"FUB push failed: {fub['error']}"))
    elif _skipped_no_api_key(fub):
        # No event: the stage did not run, and "0 leads pushed to FUB" would
        # read as a push that found nothing. incomplete_stages carries the fact.
        pass
    else:
        pushed = _count(fub, "pushed")
        if pushed is not None:
            held = _count(fub, "held_no_contact")
            failed = _count(fub, "failed")
            bits = [f"{pushed} leads pushed to FUB"]
            if held:
                bits.append(f"{held} held (no contact info)")
            if failed:
                bits.append(f"{failed} failed")
            events.append(_event(ts, "pushed", STAGE_FUB, ", ".join(bits)))

    # The run's own verdict. collect_stage_errors already names the stages that
    # failed, including the SILENT ones (every trace errored, zero parcels
    # kept) that no single section above reports as an error.
    failures = stats.get("stage_failures")
    if isinstance(failures, list) and failures:
        events.append(_event(ts, "failed", "run",
                             f"{run_type} finished with failed stage(s): "
                             f"{', '.join(str(f) for f in failures)}"))

    return events


# ── Rolling log ─────────────────────────────────────────────────────────────

def valid_event(event) -> bool:
    if not isinstance(event, dict):
        return False
    if event.get("type") not in EVENT_TYPES:
        return False
    if not isinstance(event.get("stage"), str) or not event["stage"]:
        return False
    return parse_ts(event.get("ts")) is not None


def _key_of(event: dict) -> str:
    """Identity for dedupe — every contract field, so a rerun is a no-op."""
    return "\x1f".join([
        str(event.get("ts", "")), str(event.get("type", "")), str(event.get("stage", "")),
        str(event.get("county", "")), str(event.get("detail", "")),
    ])


def _public_event(event: dict) -> dict:
    out = {
        "ts": str(event["ts"]),
        "type": str(event["type"]),
        "stage": str(event["stage"]),
        "detail": str(event.get("detail", "")),
    }
    if event.get("county"):
        out["county"] = str(event["county"])
    return out


def merge_seller_log(existing, fresh, limit: int = MAX_LOG_ENTRIES) -> list[dict]:
    """Union of what is on disk and what this run can see — NOT an append.

    Both scheduled workflows publish here, and a job that loses the push race
    rebuilds on the winner's commit and tries again. An append would duplicate
    the winner's events every time; a merge makes the retry a no-op. It also
    means history outlives the 400-row window load_runs() reads.
    """
    merged: dict = {}
    for event in list(existing or []) + list(fresh or []):
        if valid_event(event):
            merged[_key_of(event)] = event
    ordered = sorted(
        merged.values(),
        key=lambda e: (parse_ts(e["ts"]), _key_of(e)),
        reverse=True,
    )
    return [_public_event(e) for e in ordered[:limit]]


# ── Writing ─────────────────────────────────────────────────────────────────

def read_json_or(path, fallback):
    """Whatever is on disk. Unreadable or wrong-shaped is treated as absent —
    a corrupt file must not stop this run from publishing."""
    try:
        with open(path, encoding="utf-8") as handle:
            parsed = json.load(handle)
        return fallback if parsed is None else parsed
    except (OSError, ValueError):
        return fallback


def atomic_write_json(path, payload) -> None:
    """Temp file in the destination directory, then rename.

    Same directory so the rename is same-filesystem and therefore atomic;
    fsync before it so a killed runner cannot leave a renamed-but-empty file.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{int(dt.datetime.now().timestamp())}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass  # the temp file may never have existed
        raise


def build_stats(today_runs, yesterday_runs, *, date: str, yesterday: str,
                now: dt.datetime | None = None) -> dict:
    """The published counters. `today`/`yesterday` may be {} — see THE RULE."""
    stats = {
        "date": date,
        "today": counters_for_day(today_runs),
        "yesterday": counters_for_day(yesterday_runs),
        "last_run_iso": iso_z(now),
    }
    if today_runs:
        last = today_runs[-1]
        stats["last_run_type"] = last["run_type"]
        stats["last_run_finished_iso"] = iso_z(parse_ts(last["finished_at"]))
        dry = last["stats"].get("dry_run")
        if isinstance(dry, bool):
            stats["dry_run"] = dry
    stats["yesterday_date"] = yesterday
    return stats


def write_seller_telemetry(db_path, out_dir, now: dt.datetime | None = None) -> dict:
    """Write both status files. Returns what was written, for logs and tests.

    NEVER RAISES. This runs at the tail of a live pipeline job, after money has
    already been spent on skip-traces and leads have already reached FUB.
    Turning a successful run red over a dashboard number — or worse, aborting
    before the state DB is pushed — is never worth it. Failure is a return
    value here, not an exception.
    """
    try:
        now = now or dt.datetime.now(dt.timezone.utc)
        date = today_in_ct(now)
        yesterday = (dt.date.fromisoformat(date) - dt.timedelta(days=1)).isoformat()

        conn = open_readonly(db_path)
        try:
            runs = load_runs(conn, (date, yesterday))
        finally:
            conn.close()

        today_runs = [r for r in runs if day_of(r["finished_at"]) == date]
        yesterday_runs = [r for r in runs if day_of(r["finished_at"]) == yesterday]

        stats = build_stats(today_runs, yesterday_runs, date=date, yesterday=yesterday, now=now)

        fresh: list[dict] = []
        for run in runs:
            fresh.extend(events_from_run(run))
        fresh = [e for e in fresh if valid_event(e)]

        out = Path(out_dir)
        stats_path = out / STATS_FILENAME
        log_path = out / LOG_FILENAME
        merged = merge_seller_log(read_json_or(log_path, []), fresh)

        atomic_write_json(stats_path, stats)
        atomic_write_json(log_path, merged)

        return {"ok": True, "stats": stats, "log": merged,
                "paths": [str(stats_path), str(log_path)]}
    except BaseException as exc:  # noqa: BLE001 - see the docstring
        LOGGER.warning("Telemetry not written: %s", exc)
        return {"ok": False, "error": str(exc)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Write seller telemetry status files.")
    parser.add_argument("--db", required=True, help="Path to the decrypted state DB")
    parser.add_argument("--out", required=True,
                        help="Directory to write seller_stats.json / seller_log.json into. "
                             "NOT the Actions checkout — see the module docstring.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = write_seller_telemetry(args.db, args.out)
    if not result["ok"]:
        LOGGER.warning("Telemetry skipped: %s", result.get("error"))
        return 1
    LOGGER.info("Telemetry written: %s", json.dumps(result["stats"], sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

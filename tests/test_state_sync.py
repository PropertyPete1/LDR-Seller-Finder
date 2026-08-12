"""The state DB has to survive two workflows running at once.

Run with: python3 -m pytest tests/test_state_sync.py -v

The old push encrypted whatever the job happened to hold, checked out the orphan
`state` branch and committed it as a whole-file blob. Two defects in one step,
both of which have already fired in LDR-Automation-Clean (issue #9 → PR #10):

  * no compare-and-swap — a job that pulled before a sibling's push and finished
    after it discarded everything the sibling wrote, and BOTH pushes reported
    success;
  * `git checkout state` refuses over one modified tracked file, so a dirty tree
    stopped state being persisted at all. That cost the sibling repo sixteen
    hours, and the runs afterwards published a confident zero for a day that had
    really sent 150 emails.

So these are not unit tests of a merge function — they drive the real protocol
against real git repositories with real openssl encryption, in the exact order
the incident happened in:

    A pulls, B pulls, A writes X and pushes, B writes Y and pushes

and demand that both X and Y are on the branch afterwards. The one outcome that
must be impossible is the one that happened: a push that reports success while
throwing away rows it never saw.

test_the_old_whole_file_push_is_what_lost_the_rows pins the old behaviour on the
same fixture, so the defect and the fix stay legible side by side.

This repo also spends real money per skip-trace and holds homeowner PII, so two
of these go further than the sibling's: a merge may never lose a paid trace, and
it may never attach one owner's lead to another owner's phone number (the
surrogate ids the two lineages hand out independently).
"""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest
import yaml

from seller_finder import state_sync
from seller_finder.state import get_db, now_iso, owner_hash

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
STATE_ACTION = REPO_ROOT / ".github" / "actions" / "state-sync" / "action.yml"
KEY = "test-state-encryption-key"

#: The workflows that share the committed state DB. All three hold the same
#: concurrency group today (see test_the_state_writers_still_share_a_group);
#: the protocol under test is what makes that defence in depth rather than the
#: only thing standing between two runs and a lost day.
STATE_WORKFLOWS = ("weekly-pull.yml", "daily-pull.yml", "push-approved.yml")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"},
    )
    return proc.stdout.strip()


class Job:
    """One workflow run: its own checkout, its own DB, its own pull baseline.

    Runs are deliberately independent the way GitHub jobs are — separate
    machines, separate clones, and no way to see a sibling's file.
    """

    def __init__(self, origin: Path, workspace: Path, name: str):
        self.name = name
        self.repo = workspace / name
        _git(workspace, "clone", "--quiet", str(origin), name)
        self.db = self.repo / state_sync.DB_PATH
        self.baseline = workspace / f"{name}.baseline"

    def pull(self) -> int:
        return state_sync.pull(self.repo, self.db, self.baseline)

    def push(self, attempts: int = state_sync.PUSH_ATTEMPTS, **kwargs) -> int:
        return state_sync.push(self.repo, self.db, self.baseline,
                               attempts=attempts, **kwargs)

    def ensure_db(self):
        """The DB the pipeline would create for itself on a fresh runner."""
        self.db.parent.mkdir(parents=True, exist_ok=True)
        conn = get_db(self.db)
        conn.close()

    def connect(self) -> sqlite3.Connection:
        self.ensure_db()
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        return conn

    def write_run(self, marker: int, run_type: str = "weekly_pull") -> None:
        """One finished run, written the way state.record_run writes it.

        `runs` is this repo's audit_log: telemetry.py derives every published
        counter from it, so a lost row here is a day that publishes zero for
        work that really happened.
        """
        conn = self.connect()
        with conn:
            conn.execute(
                "INSERT INTO runs (run_type, started_at, finished_at, stats) VALUES (?,?,?,?)",
                (run_type, f"2026-08-11T13:{marker % 60:02d}:00-05:00",
                 f"2026-08-11T13:{marker % 60:02d}:30-05:00", json.dumps({"marker": marker})),
            )
        conn.close()

    def write_traced_lead(self, prop_id: str, owner: str, email: str,
                          county: str = "bexar") -> int:
        """A qualified lead plus the paid skip trace it is attached to.

        Both are written the way the pipeline writes them — including the
        autoincrement `skip_traces.id` that each lineage hands out on its own,
        which is exactly what must not be copied across a merge.
        """
        conn = self.connect()
        with conn:
            cur = conn.execute(
                "INSERT INTO skip_traces (owner_key, provider, matched, emails, phones, "
                "dnc, litigator, traced_at) VALUES (?,?,1,?,?,0,0,?)",
                (f"{owner}|78201", "batchdata", json.dumps([email]),
                 json.dumps([{"number": "2105550000"}]), now_iso()),
            )
            trace_id = cur.lastrowid
            conn.execute(
                "INSERT INTO leads (county, prop_id, owner_name, property_addr, mail_addr, "
                "score, signals, primary_source, status, skip_trace_id, created_at, updated_at) "
                "VALUES (?,?,?,?,?,55,'[]','absentee','awaiting_approval',?,?,?)",
                (county, prop_id, owner, f"{prop_id} MAIN ST, SAN ANTONIO TX 78201",
                 "500 OTHER RD, SAN ANTONIO TX 78209", trace_id, now_iso(), now_iso()),
            )
        conn.close()
        return trace_id

    def write_first_seen(self, prop_id: str, owner: str, first_seen_at: str,
                         county: str = "bexar") -> None:
        conn = self.connect()
        with conn:
            conn.execute(
                "INSERT INTO owners_first_seen (county, prop_id, owner_hash, first_seen_at) "
                "VALUES (?,?,?,?) ON CONFLICT (county, prop_id) DO UPDATE SET "
                "owner_hash=excluded.owner_hash, first_seen_at=excluded.first_seen_at",
                (county, prop_id, owner_hash(owner), first_seen_at),
            )
        conn.close()


def markers_in(db: Path) -> set:
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT stats FROM runs").fetchall()
    conn.close()
    return {json.loads(row[0])["marker"] for row in rows}


def contact_by_owner(db: Path) -> dict:
    """Every lead's owner → the emails its skip_trace_id actually resolves to.

    A join, on purpose: the whole risk of merging two lineages that assign
    surrogate ids independently is that this join starts returning the wrong
    person's contact details.
    """
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT l.owner_name, t.owner_key, t.emails FROM leads l "
        "LEFT JOIN skip_traces t ON t.id = l.skip_trace_id").fetchall()
    conn.close()
    return {owner: (key, json.loads(emails or "[]")) for owner, key, emails in rows}


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch):
    """The runner hands the passphrase to openssl through the environment; so do
    the tests, which is also how the real key stays out of any argv."""
    monkeypatch.setenv("STATE_KEY", KEY)


@pytest.fixture()
def origin(tmp_path):
    """A bare repo standing in for GitHub, with main published.

    main carries .github/ and status/ on purpose: the old push checked out the
    `state` branch, which holds neither, and both of those absences caused real
    incidents in the sibling repo.
    """
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "--quiet", "--bare", "--initial-branch=main", str(bare))
    seed = tmp_path / "seed-checkout"
    _git(tmp_path, "init", "--quiet", "--initial-branch=main", str(seed))
    (seed / ".github" / "actions" / "state-sync").mkdir(parents=True)
    (seed / ".github" / "actions" / "state-sync" / "action.yml").write_text("name: state-sync\n")
    (seed / "status").mkdir()
    (seed / "status" / "seller_stats.json").write_text('{"date": "2026-08-11"}\n')
    (seed / ".gitignore").write_text("data/*.sqlite3\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "--quiet", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(bare))
    _git(seed, "push", "--quiet", "origin", "main")
    return bare


@pytest.fixture()
def job(origin, tmp_path):
    workspace = tmp_path / "runs"
    workspace.mkdir()

    def factory(name: str) -> Job:
        return Job(origin, workspace, name)

    return factory


@pytest.fixture()
def seeded(job):
    """A `state` branch that already holds one run — the lineage both racing
    writers start from."""
    first = job("seed-run")
    assert first.pull() == 0
    first.write_run(1)
    assert first.push() == 0
    return first


def state_blob(origin: Path) -> bytes:
    return subprocess.run(
        ["git", "cat-file", "blob", f"refs/heads/state:{state_sync.BLOB_PATH}"],
        cwd=str(origin), capture_output=True, check=True).stdout


# ── THE acceptance case: two overlapping writers ─────────────────────────────


def test_two_overlapping_writers_cannot_discard_each_others_rows(job, seeded):
    """The incident sequence exactly: A pulls, B pulls, A writes and pushes, B
    writes and pushes. Both rows must be on the branch afterwards.

    A push that reports success while dropping A's row is the outcome that has
    to be impossible — everything telemetry publishes is derived from `runs`, so
    a dropped row is a day that reports zero for work that really happened.
    """
    a, b = job("weekly-pull"), job("push-approved")
    assert a.pull() == 0
    assert b.pull() == 0                       # B's lineage predates A's write

    a.write_run(101)
    assert a.push() == 0
    b.write_run(202, run_type="push_approved")
    assert b.push() == 0, "the second writer must not have to fail to be correct"

    after = job("verifier")
    assert after.pull() == 0
    assert markers_in(after.db) == {1, 101, 202}


def test_the_old_whole_file_push_is_what_lost_the_rows(job, seeded):
    """The same fixture, driven by the push this repo shipped before.

    That push encrypted whatever the job happened to hold and committed it as a
    whole-file blob on the current tip — no compare-and-swap, no merge. It
    succeeded, and A's row was gone. This is what the test above would look like
    against the old implementation, kept so the defect and the fix stay legible
    side by side.
    """
    a, b = job("weekly-pull"), job("daily-pull")
    a.pull()
    b.pull()
    a.write_run(101)
    assert a.push() == 0
    b.write_run(202)
    assert _whole_file_push(b) == 0, "the old push reported success"

    after = job("verifier")
    after.pull()
    survivors = markers_in(after.db)
    assert survivors == {1, 202}, "B's whole file, exactly as B held it"
    assert 101 not in survivors, "this is the defect the port fixes"


def _whole_file_push(run: Job) -> int:
    """The old algorithm: encrypt this job's file, commit it on the current tip,
    push. Kept to one helper, used only by the test above."""
    enc = run.repo / "whole-file.enc"
    state_sync.encrypt(run.db, enc)
    tip = state_sync.fetch_state(run.repo)
    commit = state_sync._commit_blob(run.repo, enc, tip, "state: whole-file overwrite")
    rc, _ = state_sync.git(run.repo, "push", "origin", f"{commit}:refs/heads/state", check=False)
    enc.unlink()
    return rc


def test_a_three_way_pileup_keeps_every_row(job, seeded):
    """Three runs can be in flight at once — a 120-minute weekly run trivially
    overlaps a daily run and a hand-triggered push."""
    runs = [job("weekly-pull"), job("daily-pull"), job("push-approved")]
    for run in runs:
        assert run.pull() == 0
    for n, run in enumerate(runs, start=1):
        run.write_run(100 * n)
        assert run.push() == 0

    after = job("verifier")
    after.pull()
    assert markers_in(after.db) == {1, 100, 200, 300}


def test_a_push_that_loses_the_race_at_the_last_instant_still_keeps_both(job, seeded, monkeypatch):
    """Compare-and-swap cannot be only a check before the push: the branch can
    move between the fetch and the push itself. Git's fast-forward rejection is
    what catches that window, and the retry re-merges on top of the newer state.

    The sibling here pushes AFTER our fetch has already read the old tip, which
    is precisely that window.
    """
    a, b = job("weekly-pull"), job("daily-pull")
    a.pull()
    b.pull()
    a.write_run(101)

    real_fetch = state_sync.fetch_state
    fired = []

    def fetch_then_let_the_sibling_in(repo):
        tip = real_fetch(repo)
        if repo == a.repo and not fired:
            fired.append(True)
            b.write_run(202)
            assert b.push() == 0
        return tip

    with monkeypatch.context() as patched:
        patched.setattr(state_sync, "fetch_state", fetch_then_let_the_sibling_in)
        assert a.push() == 0
    assert fired, "the race was never triggered — the test proves nothing"

    after = job("verifier")
    after.pull()
    assert markers_in(after.db) == {1, 101, 202}


def test_a_push_that_cannot_land_fails_loudly_instead_of_forcing(job, seeded, origin, capsys):
    """A push that cannot land must fail the step rather than force the branch.

    A pre-receive hook that rejects everything stands in for "the branch keeps
    moving": the run's rows are lost either way, but now something says so.
    """
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)

    a = job("weekly-pull")
    a.pull()
    a.write_run(101)
    assert a.push(attempts=2) == 1

    out = capsys.readouterr().out
    assert "::error::" in out and "NOT persisted" in out
    hook.unlink()

    survivor = job("verifier")
    survivor.pull()
    assert markers_in(survivor.db) == {1}, "the branch must be exactly as it was"


def test_a_push_that_cannot_read_the_branch_does_not_replace_it(job, seeded, monkeypatch, capsys):
    """A commit built without the branch's tip as its parent can only land by
    forcing. If the branch cannot be read, the answer is to fail — not to push a
    file that was never reconciled with it."""
    a = job("weekly-pull")
    a.pull()
    a.write_run(101)

    with monkeypatch.context() as patched:
        patched.setattr(state_sync, "fetch_state", lambda repo: None)
        assert a.push(attempts=2) == 1
    assert "does not descend from it" in capsys.readouterr().out

    after = job("verifier")
    after.pull()
    assert markers_in(after.db) == {1}


def test_no_push_path_ever_forces_or_switches_branches(job, seeded, monkeypatch):
    """Guard on every git command the push actually runs.

    --force is the silent-loss path itself. `checkout`/`switch`/`reset` are the
    other half: the old push checked out the 'state' branch, which is why one
    modified tracked file could refuse the whole thing and why the publish step
    had to be ordered ahead of it.
    """
    seen: list[tuple] = []
    real_git = state_sync.git

    def recording(repo, *args, **kwargs):
        seen.append(args)
        return real_git(repo, *args, **kwargs)

    monkeypatch.setattr(state_sync, "git", recording)
    a = job("weekly-pull")
    a.pull()
    a.write_run(101)
    assert a.push() == 0

    pushes = [args for args in seen if args and args[0] == "push"]
    assert pushes, "nothing was pushed"
    for args in seen:
        assert not any(a in ("--force", "-f", "--force-with-lease") for a in args), args
        assert args[0] not in ("checkout", "switch", "reset", "clean", "rm", "config"), args


# ── Money and PII: what a merge may never do ─────────────────────────────────


def test_two_writers_cannot_lose_a_paid_skip_trace(job, seeded):
    """Every row in skip_traces was billed at SKIP_TRACE_COST_USD. Losing one
    does not just lose data — the next run re-traces that owner and pays for the
    same record twice."""
    a, b = job("weekly-pull"), job("daily-pull")
    a.pull()
    b.pull()
    a.write_traced_lead("1001", "OWNER-A", "a@example.com")
    b.write_traced_lead("2002", "OWNER-B", "b@example.com")
    assert a.push() == 0
    assert b.push() == 0

    after = job("verifier")
    after.pull()
    conn = sqlite3.connect(after.db)
    keys = {row[0] for row in conn.execute("SELECT owner_key FROM skip_traces")}
    conn.close()
    assert keys == {"OWNER-A|78201", "OWNER-B|78201"}


def test_a_merged_lead_keeps_its_own_owners_contact_info(job, seeded):
    """The surrogate-id trap, end to end through encrypt/decrypt.

    Both lineages hand out `skip_traces.id` = 1 for different owners, and
    `leads.skip_trace_id` points at it. Copying their id across the merge would
    join OWNER-B's lead to OWNER-A's phone number and email — a wrong-person
    push into a live CRM, from a repo that holds homeowner PII.
    """
    a, b = job("weekly-pull"), job("daily-pull")
    a.pull()
    b.pull()
    assert a.write_traced_lead("1001", "OWNER-A", "a@example.com") == 1
    assert b.write_traced_lead("2002", "OWNER-B", "b@example.com") == 1, \
        "both lineages must really collide on id 1, or this test proves nothing"
    assert a.push() == 0
    assert b.push() == 0

    after = job("verifier")
    after.pull()
    assert contact_by_owner(after.db) == {
        "OWNER-A": ("OWNER-A|78201", ["a@example.com"]),
        "OWNER-B": ("OWNER-B|78201", ["b@example.com"]),
    }


def test_a_reference_that_cannot_be_translated_becomes_null(job, seeded):
    """Their lead points at a skip trace their file no longer holds (the tracer
    expires stale no-matches). It must land with no trace rather than with
    whatever row happens to hold that id here."""
    a, b = job("weekly-pull"), job("daily-pull")
    a.pull()
    b.pull()
    a.write_traced_lead("1001", "OWNER-A", "a@example.com")
    b.write_traced_lead("2002", "OWNER-B", "b@example.com")
    conn = b.connect()
    with conn:  # the expiry the tracer performs on a stale no-match
        conn.execute("DELETE FROM skip_traces")
    conn.close()

    assert a.push() == 0
    assert b.push() == 0

    after = job("verifier")
    after.pull()
    assert contact_by_owner(after.db) == {
        "OWNER-A": ("OWNER-A|78201", ["a@example.com"]),
        "OWNER-B": (None, []),
    }


def test_a_conflicting_push_never_invents_tenure(job, seeded):
    """owners_first_seen.first_seen_at pays out +20 for "owned 10+ years", and
    parcels.py resets it when the owner changes. A merge that kept the older
    stamp across an owner change would invent tenure the new owner does not
    have, lift a 30-point absentee lead over the 40-point trace threshold, and
    spend money on it."""
    a, b = job("weekly-pull"), job("daily-pull")
    a.pull()
    b.pull()
    a.write_first_seen("1001", "OLD OWNER", "2014-01-01T06:00:00-06:00")
    b.write_first_seen("1001", "NEW OWNER", "2026-08-11T06:00:00-05:00")
    assert b.push() == 0
    assert a.push() == 0                       # A lands second, with the old owner

    after = job("verifier")
    after.pull()
    conn = sqlite3.connect(after.db)
    row = conn.execute(
        "SELECT owner_hash, first_seen_at FROM owners_first_seen "
        "WHERE county='bexar' AND prop_id='1001'").fetchone()
    conn.close()
    assert row[0] == owner_hash("NEW OWNER")
    assert row[1] == "2026-08-11T06:00:00-05:00"


def test_a_push_whose_merge_fails_does_not_overwrite_the_branch(job, seeded, origin, monkeypatch, capsys):
    """A merge that cannot be completed must fail the step, not fall back to the
    old behaviour. Pushing our unreconciled file is exactly the bug."""
    a, b = job("weekly-pull"), job("daily-pull")
    a.pull()
    b.pull()
    a.write_run(101)
    assert a.push() == 0
    b.write_run(202)

    def refuse(*args, **kwargs):
        raise state_sync.MergeError("leads: the two schemas share no columns")

    monkeypatch.setattr(state_sync, "merge_databases", refuse)
    assert state_sync.main(["push", "--repo", str(b.repo), "--db", str(b.db),
                            "--baseline", str(b.baseline)]) == 1
    assert "::error::" in capsys.readouterr().out

    after = job("verifier")
    after.pull()
    assert markers_in(after.db) == {1, 101}, "A's push must still be what is on the branch"


def test_an_oversized_encrypted_db_is_refused_before_it_is_committed(job, seeded, origin, capsys):
    """The guard the old push step carried, kept at the push site.

    state.check_state_size() runs inside the Python process, but this step is
    `if: always()` — a run that failed that guard, or crashed before it, still
    gets here. GitHub hard-rejects blobs over 100 MB, which would fail the push
    after the commit exists and leave the branch needing manual repair.
    """
    before = state_blob(origin)
    a = job("weekly-pull")
    a.pull()
    a.write_run(101)
    assert a.push(max_bytes=64) == 1

    out = capsys.readouterr().out
    assert "::error::" in out and "REFUSING to push" in out
    assert state_blob(origin) == before, "the branch keeps its last good version"


# ── What the branch is allowed to contain ───────────────────────────────────


def test_the_state_branch_holds_nothing_but_the_encrypted_blob(job, seeded, origin):
    """Encrypted at rest, and only the DB: no working tree, no .github/, no
    plaintext. A DB pushed in the clear would put every homeowner's name,
    address, email and phone number in a branch of a public repo."""
    seeded.write_traced_lead("1001", "PRIVATEOWNER", "private@example.com")
    assert seeded.push() == 0

    paths = _git(origin, "ls-tree", "-r", "--name-only", "refs/heads/state").splitlines()
    assert paths == [state_sync.BLOB_PATH]

    raw = state_blob(origin)
    assert not raw.startswith(b"SQLite format 3"), "the state DB is on the branch in the clear"
    for secret in (b"PRIVATEOWNER", b"private@example.com", b"2105550000"):
        assert secret not in raw


def test_what_is_pushed_decrypts_back_to_the_same_rows(job, seeded, origin):
    """The round trip, through openssl both ways — the pull side has to be able
    to read what the push side wrote."""
    reader = job("verifier")
    assert reader.pull() == 0
    assert markers_in(reader.db) == {1}
    assert reader.baseline.read_text().strip() == _git(
        origin, "rev-parse", f"refs/heads/state:{state_sync.BLOB_PATH}")


def test_the_push_never_switches_branches_or_dirties_the_tree(job, seeded):
    """The hazard this port removes, stated as a test.

    The old push did `git checkout state`, which refuses over a modified tracked
    file and leaves the job standing on a branch holding only the encrypted blob
    — no .github/, so no local composite action could load after it. This one
    builds the commit with plumbing and touches neither HEAD nor the tree, which
    is also why `Publish telemetry` no longer HAS to be ordered ahead of it.
    """
    a = job("weekly-pull")
    a.pull()
    a.write_run(101)

    head_before = _git(a.repo, "rev-parse", "HEAD")
    branch_before = _git(a.repo, "rev-parse", "--abbrev-ref", "HEAD")
    # A modified tracked file is exactly what refused the old checkout.
    (a.repo / "status" / "seller_stats.json").write_text('{"date": "2026-08-12"}\n')

    assert a.push() == 0

    assert _git(a.repo, "rev-parse", "HEAD") == head_before
    assert _git(a.repo, "rev-parse", "--abbrev-ref", "HEAD") == branch_before == "main"
    assert (a.repo / ".github" / "actions" / "state-sync" / "action.yml").exists()
    assert json.loads((a.repo / "status" / "seller_stats.json").read_text())["date"] \
        == "2026-08-12"
    assert a.db.exists(), "the decrypted DB must survive its own push"


def test_the_push_writes_nothing_into_the_checkout(job, seeded):
    """The pull baseline and the ciphertext both live outside the working tree:
    an untracked file under the checkout is one `git status --porcelain` away
    from breaking the next thing that reads it."""
    a = job("weekly-pull")
    a.pull()
    a.write_run(101)
    before = _git(a.repo, "status", "--porcelain")
    assert a.push() == 0
    assert _git(a.repo, "status", "--porcelain") == before
    assert a.baseline.exists() and not a.baseline.is_relative_to(a.repo)


# ── Pulling ─────────────────────────────────────────────────────────────────


def test_the_first_ever_run_starts_fresh_and_creates_the_branch(job, origin):
    a = job("weekly-pull")
    assert a.pull() == 0
    assert not a.db.exists(), "nothing to decrypt, and nothing invented"
    a.write_run(101)
    assert a.push() == 0
    assert _git(origin, "rev-parse", "--verify", "refs/heads/state")


def test_the_branch_as_the_old_shell_wrote_it_is_still_readable(job, origin, tmp_path):
    """The live `state` branch was written by the old shell step. This port must
    be drop-in: same blob path, same `openssl enc -aes-256-cbc -pbkdf2` format,
    same branch layout — or the first run after it starts from nothing.
    """
    legacy = job("legacy-writer")
    legacy.write_run(42)
    enc = tmp_path / "legacy.enc"
    state_sync.encrypt(legacy.db, enc)
    # The old action's push, in the order it ran it.
    _git(legacy.repo, "checkout", "--orphan", "state")
    _git(legacy.repo, "rm", "-rf", "--cached", ".")
    (legacy.repo / "data").mkdir(exist_ok=True)
    (legacy.repo / state_sync.BLOB_PATH).write_bytes(enc.read_bytes())
    _git(legacy.repo, "add", state_sync.BLOB_PATH)
    _git(legacy.repo, "commit", "--quiet", "-m", "state: update encrypted SQLite DB")
    _git(legacy.repo, "push", "--quiet", "origin", "state")

    reader = job("weekly-pull")
    assert reader.pull() == 0
    assert markers_in(reader.db) == {42}
    reader.write_run(43)
    assert reader.push() == 0

    after = job("verifier")
    after.pull()
    assert markers_in(after.db) == {42, 43}


def test_a_pull_that_cannot_reach_an_existing_branch_refuses_to_start_fresh(job, seeded, monkeypatch):
    """"Start fresh" is only correct when there is demonstrably nothing there.

    A run that starts from an empty DB has no skip-trace cache and no lead
    statuses: it re-traces owners this system has already paid for and re-pushes
    leads that are already in FUB. That must be a failed step, not a fresh start.
    """
    monkeypatch.setattr(state_sync, "fetch_state", lambda repo: None)
    a = job("weekly-pull")
    assert state_sync.main(["pull", "--repo", str(a.repo), "--baseline", str(a.baseline)]) == 1
    assert not a.db.exists()


def test_a_push_with_no_pull_baseline_merges_instead_of_overwriting(job, seeded):
    """The push step runs with `if: always()`, so it can run after a failed pull.

    With no record of what this run started from, the branch has to be treated as
    moved — the alternative is overwriting a lineage this job may never have seen,
    which is the bug.
    """
    orphan = job("daily-pull")
    orphan.write_run(202)                      # a DB that never pulled anything
    assert not orphan.baseline.exists()
    assert orphan.push() == 0

    after = job("verifier")
    after.pull()
    assert markers_in(after.db) == {1, 202}


def test_a_repeated_push_of_the_same_file_is_a_no_op_for_the_rows(job, seeded):
    """Pushing twice (a rerun, or a retried job) must not duplicate the day.
    `runs` has no unique key, so this is not free."""
    a = job("weekly-pull")
    a.pull()
    a.write_run(101)
    assert a.push() == 0
    assert a.push() == 0

    after = job("verifier")
    after.pull()
    conn = sqlite3.connect(after.db)
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
    conn.close()


# ── The wiring the workflows actually use ───────────────────────────────────


def test_the_action_runs_the_module_the_way_production_will(job, seeded):
    """The command in action.yml, in a clean interpreter with only PYTHONPATH and
    the step's env set."""
    a = job("weekly-pull")
    a.pull()
    a.write_run(101)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update({"PYTHONPATH": str(REPO_ROOT / "src"), "STATE_KEY": KEY,
                "RUNNER_TEMP": str(a.baseline.parent)})
    proc = subprocess.run(
        [sys.executable, "-m", "seller_finder.state_sync", "push"],
        cwd=str(a.repo), env=env, capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr[-3000:]

    after = job("verifier")
    after.pull()
    assert markers_in(after.db) == {1, 101}


def test_the_module_syncs_the_file_the_workflows_export(job, seeded, monkeypatch, tmp_path):
    """DATABASE_PATH is the one place the path is configured — config.DB_PATH
    reads it too, so the sync and the pipeline cannot end up on different files."""
    a = job("weekly-pull")
    a.pull()
    a.write_run(101)
    moved = a.repo / "data" / "elsewhere.sqlite3"
    a.db.rename(moved)
    monkeypatch.setenv("DATABASE_PATH", "data/elsewhere.sqlite3")
    assert state_sync.main(
        ["push", "--repo", str(a.repo), "--baseline", str(a.baseline)]) == 0

    after = job("verifier")
    after.pull()
    assert markers_in(after.db) == {1, 101}


@pytest.mark.parametrize("mode", ["pull", "push"])
def test_the_action_yaml_and_the_module_cannot_drift(mode):
    action = yaml.safe_load(STATE_ACTION.read_text())
    steps = action["runs"]["steps"]
    step = next(s for s in steps if f"inputs.mode == '{mode}'" in s["if"])
    assert f"seller_finder.state_sync {mode}" in step["run"]
    assert "PYTHONPATH=src" in step["run"]
    # The passphrase reaches openssl through the environment, never argv.
    assert step["env"]["STATE_KEY"] == "${{ inputs.encryption_key }}"


def test_the_action_no_longer_touches_the_working_tree():
    """The shell that broke state persistence for sixteen hours in the sibling
    repo is gone from this action, not just unreachable."""
    body = STATE_ACTION.read_text()
    for banned in ("git checkout", "git rm -rf", "--orphan", "git config user"):
        assert banned not in body.split("runs:", 1)[1], (
            f"state-sync still runs `{banned}`")


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _state_sync_modes(workflow: dict) -> list[str]:
    modes = []
    for job_def in workflow["jobs"].values():
        for step in job_def["steps"]:
            if step.get("uses") == "./.github/actions/state-sync":
                modes.append(step["with"]["mode"])
    return modes


@pytest.mark.parametrize("name", STATE_WORKFLOWS)
def test_every_workflow_still_pulls_before_it_pushes(name):
    """A workflow that pushes without pulling would push a DB built from nothing.
    The merge would save the branch's rows, but the run would still have made
    every decision — including what to trace and what to push — blind."""
    modes = _state_sync_modes(_workflow(name))
    assert modes, f"{name} no longer syncs the state DB at all"
    assert modes[0] == "pull"
    assert "push" in modes


@pytest.mark.parametrize("name", STATE_WORKFLOWS)
def test_every_workflow_exports_the_database_path(name):
    """The action has no path input on purpose — it syncs $DATABASE_PATH."""
    assert _workflow(name)["env"]["DATABASE_PATH"] == "data/seller_finder.sqlite3"


def test_the_state_writers_still_share_a_concurrency_group():
    """Serialisation is defence in depth here, not the guarantee.

    All three state writers hold `ldr-seller-state`, so today they queue rather
    than overlap. That is worth keeping — and it is also exactly the assumption
    that stops being true the moment a fourth workflow, a matrix job or a
    templated group appears. The protocol above is what makes the overlap
    survivable when it does; this test is here so a change to the group is a
    deliberate one.
    """
    groups = {name: _workflow(name)["concurrency"]["group"] for name in STATE_WORKFLOWS}
    assert set(groups.values()) == {"ldr-seller-state"}, groups
    for name, group in groups.items():
        assert "${{" not in group, f"{name}: a templated group can collide unpredictably"

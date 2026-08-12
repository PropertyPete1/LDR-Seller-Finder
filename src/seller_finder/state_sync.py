"""state_sync.py — pull/push the encrypted state DB, with compare-and-swap.

Every workflow in .github/workflows pulls this file at the start of a job and
pushes it back at the end. The push used to be an unconditional whole-file
overwrite performed by checking out the orphan `state` branch: no
compare-and-swap, no merge, and a hard dependency on a clean working tree. Both
halves of that were live hazards, and both have already fired in the sibling
repo (LDR-Automation-Clean, its issue #9 → PR #10):

  * A run that pulled before a sibling's push and finished after it silently
    threw away everything the sibling had written, and BOTH pushes reported
    success. On 2026-08-11 that happened twice in one day over there — 150 pond
    sends, then the repair that rebuilt them.
  * `git checkout state` refuses over one modified tracked file, and the
    `|| git checkout --orphan state` fallback then dies with "a branch named
    'state' already exists" under `bash -e`. Two workflows failed at that step
    for sixteen hours. The bots kept running; none of their state was persisted;
    every later run recomputed the day from a DB that had never seen the work
    and published a confident zero.

This module is the seller-side port of the fix.

─────────────────────────────────────────────────────────────────────────────
THE PROTOCOL

pull   fetch `state`, remember the BLOB SHA the DB came from (the baseline),
       decrypt to the DB path.

push   1. fetch `state` and read the blob SHA on it NOW.
       2. if it differs from the baseline, the branch moved while this job was
          running: decrypt what is there and merge it into our file
          (state_merge.py), then adopt that SHA as the baseline.
       3. build the commit with plumbing against a temporary index and push it
          with the fetched tip as its parent. Git's own fast-forward check is
          the compare-and-swap: if anything landed between step 1 and the push,
          the push is REJECTED and we go round again — re-fetching, re-merging
          on top of the newer state, rebuilding.
       4. out of attempts → exit 1, loudly. Never a force push: overwriting the
          branch is the bug, not the recovery.

Two properties follow, and tests/test_state_sync.py asserts both against real
git repositories: a push either carries the rows that were on the branch or it
fails, and a run's own rows are never dropped by a sibling's push.

The commit is built with `git hash-object` / `git write-tree` / `git commit-tree`
— the same plumbing publish-telemetry uses — so HEAD, the current branch and the
working tree are never touched. A dirty tree can no longer refuse the push, and
no step after a push needs `actions/checkout` again to get `.github/` back.

The size guard stays AT THE PUSH SITE. state.check_state_size() runs inside the
Python process, but every workflow runs the push step with `if: always()`, so a
run that failed that guard (or crashed before it) still reaches here. GitHub
hard-rejects blobs over 100 MB, which would fail the push after the commit
exists; refusing before committing leaves the previous good state on the branch
for the next run to resume from.

The encryption is unchanged: AES-256-CBC through openssl with PBKDF2, keyed by
SQLITE_ENCRYPTION_KEY, read from the environment as STATE_KEY so the passphrase
never appears in a command line or in a run log. Only the encrypted blob is ever
written to the branch, and the plaintext DB — which holds homeowner PII — never
leaves the runner.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Sequence

from .state_merge import MergeError, format_summary, merge_databases

#: Where the encrypted DB lives on the `state` branch. The branch holds this and
#: nothing else.
BLOB_PATH = "data/seller_finder.sqlite3.enc"
#: The decrypted DB, relative to the repository root (the workflows' cwd). The
#: workflows also export DATABASE_PATH; main() prefers it so the module and
#: config.DB_PATH cannot disagree about which file the run is using.
DB_PATH = "data/seller_finder.sqlite3"
STATE_BRANCH = "state"
BASELINE_FILENAME = "state-sync-baseline"
#: Six attempts at ~one round trip each. A push loses the race only to a
#: sibling that pushed in the last second or two; needing seven in a row would
#: mean something is badly wrong and a loud failure is the right answer.
PUSH_ATTEMPTS = 6
#: GitHub's hard limit is 100 MB. state.check_state_size() guards the plaintext
#: file at 90 MB inside the run; this is the backstop on the bytes that would
#: actually be committed.
MAX_ENCRYPTED_BYTES = 95 * 1000 * 1000

ENCRYPT_ARGS = ("-aes-256-cbc", "-pbkdf2")


class StateSyncError(RuntimeError):
    """Anything that must stop a push rather than push the wrong bytes."""


def log(message: str) -> None:
    print(f"[state-sync] {message}", flush=True)


def _why(output: str) -> str:
    """The one line of a git failure worth putting in a run log."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    rejections = [line for line in lines if "reject" in line.lower()]
    return (rejections or lines or ["no output"])[-1]


# ── git ──────────────────────────────────────────────────────────────────────


def git(repo: Path, *args: str, check: bool = True, binary: bool = False,
        index: Optional[Path] = None):
    """Run git in `repo`. Returns (returncode, output).

    On success `output` is stdout; on failure it is stderr, because the only
    thing a caller wants from a failed git command is the reason — a rejected
    push says why on stderr and nothing at all on stdout.
    """
    env = dict(os.environ)
    # commit-tree needs an identity and must not depend on repo config, which
    # this module deliberately never writes.
    env.setdefault("GIT_AUTHOR_NAME", "github-actions[bot]")
    env.setdefault("GIT_AUTHOR_EMAIL", "github-actions[bot]@users.noreply.github.com")
    env["GIT_COMMITTER_NAME"] = env["GIT_AUTHOR_NAME"]
    env["GIT_COMMITTER_EMAIL"] = env["GIT_AUTHOR_EMAIL"]
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    proc = subprocess.run(
        ["git", *args], cwd=str(repo), env=env, capture_output=True, check=False,
    )
    if check and proc.returncode != 0:
        raise StateSyncError(
            f"git {' '.join(args)} failed ({proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    if binary:
        return proc.returncode, proc.stdout
    stream = proc.stdout if proc.returncode == 0 else (proc.stderr or proc.stdout)
    return proc.returncode, stream.decode("utf-8", "replace").strip()


def fetch_state(repo: Path) -> Optional[str]:
    """Fetch the state branch. Returns its commit SHA, or None if it has none.

    Forced into a remote-tracking ref of its own: a local `state` branch would
    be refused on divergence, and this must never depend on the checkout's
    refspec (actions/checkout fetches only the branch the run started from).
    """
    rc, _ = git(
        repo, "fetch", "--no-tags", "origin",
        f"+refs/heads/{STATE_BRANCH}:refs/remotes/origin/{STATE_BRANCH}",
        check=False,
    )
    if rc != 0:
        return None
    rc, sha = git(repo, "rev-parse", "--verify",
                  f"refs/remotes/origin/{STATE_BRANCH}", check=False)
    return sha if rc == 0 and sha else None


def state_blob_sha(repo: Path, commit: Optional[str]) -> Optional[str]:
    """The SHA of the encrypted DB blob on `commit`, or None if it has none."""
    if not commit:
        return None
    rc, sha = git(repo, "rev-parse", "--verify", f"{commit}:{BLOB_PATH}", check=False)
    return sha if rc == 0 and sha else None


def remote_state_exists(repo: Path) -> Optional[bool]:
    """Does origin have a `state` branch? None = the remote could not be asked."""
    rc, out = git(
        repo, "ls-remote", "--heads", "origin", f"refs/heads/{STATE_BRANCH}", check=False)
    if rc != 0:
        return None
    return bool(out)


# ── openssl ──────────────────────────────────────────────────────────────────


def _openssl(*args: str) -> None:
    if "STATE_KEY" not in os.environ:
        raise StateSyncError("STATE_KEY is not set — cannot encrypt or decrypt the state DB")
    proc = subprocess.run(
        ["openssl", "enc", *ENCRYPT_ARGS, *args, "-pass", "env:STATE_KEY"],
        capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise StateSyncError(
            "openssl enc failed: " + proc.stderr.decode("utf-8", "replace").strip())


def encrypt(db_path: Path, out_path: Path) -> None:
    _openssl("-in", str(db_path), "-out", str(out_path))


def decrypt(enc_path: Path, out_path: Path) -> None:
    _openssl("-d", "-in", str(enc_path), "-out", str(out_path))


# ── baseline bookkeeping ─────────────────────────────────────────────────────


def default_baseline_path() -> Path:
    """Outside the checkout, deliberately.

    A file under the working tree would be either a modified tracked file or an
    untracked one, and a dirty tree is what broke state persistence for sixteen
    hours in the sibling repo. RUNNER_TEMP is per-job on GitHub-hosted runners.
    """
    base = os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()
    return Path(base) / BASELINE_FILENAME


def read_baseline(path: Path) -> Optional[str]:
    """The blob SHA this run's DB was pulled from.

    None means "unknown" — no pull recorded one, e.g. the pull step failed and
    the `if: always()` push still ran. Unknown is treated as a conflict, so the
    branch is merged in rather than overwritten by a file that may never have
    contained it.
    """
    try:
        recorded = path.read_text().strip()
    except OSError:
        return None
    return recorded or None


def write_baseline(path: Path, blob_sha: Optional[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{blob_sha or ''}\n")


# ── pull ─────────────────────────────────────────────────────────────────────


def pull(repo: Path, db_path: Path, baseline_path: Path) -> int:
    commit = fetch_state(repo)
    if commit is None and remote_state_exists(repo) is not False:
        # "Start fresh" is only ever correct when there is demonstrably nothing
        # there. A run that starts from an empty DB has no skip-trace cache, so
        # it re-traces owners this system has already paid for and re-pushes
        # leads that are already in FUB.
        raise StateSyncError(
            f"'{STATE_BRANCH}' could not be fetched and cannot be shown not to exist — "
            f"refusing to run against an empty state DB")

    blob = state_blob_sha(repo, commit)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if blob is None:
        log(f"No encrypted state DB on '{STATE_BRANCH}' — starting fresh")
        write_baseline(baseline_path, None)
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        enc = Path(tmp) / "state.enc"
        _, payload = git(repo, "cat-file", "blob", blob, binary=True)
        enc.write_bytes(payload)
        decrypt(enc, db_path)

    # Recorded only after a successful decrypt: a baseline for a DB we do not
    # actually hold would let a later push claim it had seen that state.
    write_baseline(baseline_path, blob)
    log(f"Pulled and decrypted {db_path} from {blob[:12]} "
        f"({db_path.stat().st_size} bytes)")
    return 0


# ── push ─────────────────────────────────────────────────────────────────────


def _commit_blob(repo: Path, enc_path: Path, parent: Optional[str], message: str) -> str:
    """Build a commit holding exactly one file, without touching HEAD.

    The temporary index starts EMPTY (never read-tree of the parent), so the
    tree is the encrypted blob and nothing else — which is what keeps the state
    branch free of the repository it lives in, and free of the plaintext DB.
    """
    _, blob = git(repo, "hash-object", "-w", "--", str(enc_path))
    with tempfile.TemporaryDirectory() as tmp:
        index = Path(tmp) / "index"
        git(repo, "update-index", "--add", "--cacheinfo",
            f"100644,{blob},{BLOB_PATH}", index=index)
        _, tree = git(repo, "write-tree", index=index)
    args = ["commit-tree", tree, "-m", message]
    if parent:
        args += ["-p", parent]
    _, commit = git(repo, *args)
    return commit


def push(
    repo: Path,
    db_path: Path,
    baseline_path: Path,
    attempts: int = PUSH_ATTEMPTS,
    max_bytes: int = MAX_ENCRYPTED_BYTES,
) -> int:
    if not db_path.exists():
        log(f"No state DB at {db_path} — nothing to push")
        return 0

    baseline = read_baseline(baseline_path)
    if baseline is None:
        log("No pull baseline recorded for this run — treating the branch as "
            "moved and merging it in before pushing")

    for attempt in range(1, attempts + 1):
        parent = fetch_state(repo)
        if parent is None and remote_state_exists(repo) is not False:
            # The branch is (or may be) there and we could not read it. A commit
            # built without it as its parent can only be pushed by forcing, and
            # forcing is the bug. Try again; run out of attempts and fail loudly.
            log(f"Could not fetch '{STATE_BRANCH}' (attempt {attempt}) — refusing to build a "
                f"commit that does not descend from it")
            continue
        current = state_blob_sha(repo, parent)

        if current != baseline:
            if current is None:
                # The branch is gone (or has no DB yet). There is nothing to
                # merge and nothing to lose by creating it.
                log(f"'{STATE_BRANCH}' holds no state DB — this push creates it")
            else:
                log(f"CONFLICT: '{STATE_BRANCH}' moved to {current[:12]} since this run "
                    f"pulled {(baseline or 'nothing')[:12]} — merging it in rather than "
                    f"overwriting it")
                _merge_branch_into(repo, current, db_path)
            baseline = current

        with tempfile.TemporaryDirectory() as tmp:
            enc = Path(tmp) / "state.enc"
            encrypt(db_path, enc)
            size = enc.stat().st_size
            if size > max_bytes:
                # Refuse BEFORE committing: the branch keeps its last good
                # version and the next run resumes from it.
                log(f"::error::Encrypted state DB is {size} bytes (> {max_bytes} guard, "
                    f"GitHub's hard limit is 100 MB). REFUSING to push — the "
                    f"'{STATE_BRANCH}' branch keeps its last good version. Investigate "
                    f"table growth (likely warm_leads or skip_traces) before the next run.")
                return 1
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            commit = _commit_blob(
                repo, enc, parent, f"state: update encrypted SQLite DB [{stamp}]")
            log(f"Encrypted state DB ({size} bytes) → {commit[:12]}")

        rc, output = git(
            repo, "push", "origin", f"{commit}:refs/heads/{STATE_BRANCH}", check=False)
        if rc == 0:
            write_baseline(baseline_path, state_blob_sha(repo, commit))
            log(f"Pushed to '{STATE_BRANCH}' (attempt {attempt}): {commit[:12]}")
            return 0
        log(f"Push rejected (attempt {attempt}) — the branch moved again: {_why(output)}")

    # Deliberately not a force push. The run's writes are still in its local
    # file and are lost either way, but a loud failure is what tells anyone that
    # they are — the silence is what made 2026-08-11 permanent.
    log(f"::error::State DB push failed after {attempts} attempts. This run's rows "
        f"were NOT persisted. Nothing was overwritten.")
    return 1


def _merge_branch_into(repo: Path, blob: str, db_path: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        enc = Path(tmp) / "theirs.enc"
        theirs = Path(tmp) / "theirs.sqlite3"
        _, payload = git(repo, "cat-file", "blob", blob, binary=True)
        enc.write_bytes(payload)
        decrypt(enc, theirs)
        try:
            summary = merge_databases(str(db_path), str(theirs))
        except MergeError as exc:
            raise StateSyncError(str(exc)) from exc
    log(f"Merged: {format_summary(summary)}")


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: Optional[Sequence[str]] = None) -> int:
    """`python -m seller_finder.state_sync pull|push` — used by
    .github/actions/state-sync.

    Exit codes are the contract with that action:
      0 — pulled, or pushed (including "nothing to push").
      1 — the state DB could not be pulled or could not be persisted. Loud on
          purpose: a run whose writes did not land must not look like a run
          that succeeded.
    """
    parser = argparse.ArgumentParser(
        description="Sync the encrypted SQLite state DB with the 'state' branch.")
    parser.add_argument("mode", choices=["pull", "push"])
    parser.add_argument("--repo", default=".", help="Repository root (default: cwd).")
    parser.add_argument(
        "--db", default=None,
        help="Decrypted DB path, relative to --repo. Defaults to $DATABASE_PATH, "
             f"then {DB_PATH}.")
    parser.add_argument(
        "--baseline", default=None,
        help="File holding the blob SHA this run pulled. Defaults to "
             f"$RUNNER_TEMP/{BASELINE_FILENAME}, outside the checkout.")
    parser.add_argument("--attempts", type=int, default=PUSH_ATTEMPTS)
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    db_path = Path(args.db or os.environ.get("DATABASE_PATH") or DB_PATH)
    if not db_path.is_absolute():
        db_path = repo / db_path
    baseline_path = Path(args.baseline) if args.baseline else default_baseline_path()

    try:
        if args.mode == "pull":
            return pull(repo, db_path, baseline_path)
        return push(repo, db_path, baseline_path, attempts=args.attempts)
    except StateSyncError as exc:
        log(f"::error::{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

# Production Review — Findings

Full engineering review of LDR-Seller-Finder, July 2026. Baseline: `b0936b1`
(67 tests green). Result: **115 tests green**, 6 commits, no pushes.

Every defect below was **reproduced before it was fixed** and has a regression
test that fails without the fix. Where I checked something and it was already
correct, I say so — those are in [§4](#4-verified-correct-no-change-needed),
and they are as much a part of the review as the bugs.

---

## Severity summary

| # | Finding | Severity | Status |
|---|---|---|---|
| 1 | ArcGIS HTTP 200 + `{"error"}` read as "no notices" / "no exemptions" | **Critical** | Fixed |
| 2 | FUB tag `PUT` unchecked → lead marked pushed with no **DNC** tag | **Critical** | Fixed |
| 3 | BatchData paid matches cached as no-matches on address mismatch | **High** | Fixed |
| 4 | Total skip-trace / FUB / parcel failure exits 0 and pings healthcheck green | **High** | Fixed |
| 5 | `run_push_approved.py` resets the shared dead-man's switch | **High** | Fixed |
| 6 | Unmatched divorce cases re-billed to Claude every run, forever | **High** | Fixed |
| 7 | State size guard cannot stop the push it exists to prevent | **Medium** | Fixed |
| 8 | Full provider consumer profiles (DOB, relatives) stored, never read | **Medium** | Fixed |
| 9 | `DRY_RUN` billed Anthropic once per lead | **Medium** | Fixed |
| 10 | Test suite passed green with a dead production entry point | **Medium** | Fixed |
| 11 | Documented mirror-refresh process could never run on macOS | **Medium** | Fixed |
| 12 | `install_workflows.sh` would silently revert the live workflow | **Medium** | Fixed |
| 13 | Two tautological tests + one `or` assertion + order-dependent pollution | **Medium** | Fixed |
| 14 | README promised a manual approval gate that does not exist | **Medium** | Fixed |
| 15 | Assorted robustness/consistency (divorce `float()`, `INBOX_DIR`, FUB key parity) | Low | Fixed |
| 16 | FUB address dedupe can match an unrelated person | Low | **Flagged** |
| 17 | Warm-tier rows never expire | Low | **Flagged** |
| 18 | DST drift: cron is 6 AM CDT, 5 AM CST | Low | **Flagged** |

---

## 1. Critical — ArcGIS reports failures as HTTP 200

**Where:** `sources/preforeclosure.py`, `sources/exemptions.py`
**Commit:** `8d768eb`

ArcGIS Server does not use HTTP status codes for query failures. A bad field
name, a rebuilt layer, an expired service, or a server-side exception all
return **HTTP 200** with an error object in the body:

```json
{"error": {"code": 400, "message": "Unable to complete operation",
           "details": ["Invalid field: ADDRESS"]}}
```

`resp.raise_for_status()` is happy with that, and `data.get("features", [])`
turns it into `[]`. Reproduced:

```
PROBE1 preforeclosure: returned 0 notices, NO exception raised
PROBE2 exemptions:     returned 0 rows,    NO exception raised
```

Two distinct consequences:

- **Foreclosures.** The module already had a `FeedUnavailable` guard, and it
  is good — but it only counts transport-level failures. A body-level error
  incremented nothing, so `failed == attempted` never held and the run
  recorded a clean "0 notices". Bexar is the only live foreclosure feed in the
  system; losing it silently removes the single highest-value timeliness
  signal, and the run still reports success.
- **Exemptions — worse.** An empty pull is *diffed against the previous
  snapshot*. That is precisely the mass-homestead-removed scenario the
  truncation guard was written to prevent. The guard saves you when `prev` is
  non-empty, but it is the second line of defence catching a failure the first
  line never reported. Homestead-removed is +10, and absentee (30) + 10 = 40,
  exactly the trace threshold — so this converts directly into BatchData spend
  and FUB pushes on parcels whose exemption never changed.

**Fix.** New `src/seller_finder/arcgis.py`; every ArcGIS call goes through
`query()`, which raises `ArcGISError` on body-level errors. Transport failures
retry 3×; body errors do not, because a malformed query fails identically every
time. A genuinely empty page still means zero — covered by an over-correction
test.

This is the same defect class as the two the repo has already shipped
(BatchData 403 → 64 cached no-matches, then the HTTP-200-with-error-body layer
under it). The brief said to hunt variants; these were two of four.

## 2. Critical — the DNC tag silently failed to reach FUB

**Where:** `fub.py:push_lead`
**Commit:** `8d768eb`

On the existing-person path, the `PUT` that applies tags had its response
discarded:

```python
session.put(f"{FUB_BASE}/people/{existing_id}", json={"tags": merged}, timeout=60)
person_id = existing_id          # ← returned regardless of outcome
```

Reproduced with a 500 on the PUT: `push_lead` returned `'555'`, the lead was
marked `pushed`, and nothing in the logs indicated failure.

Applying tags **is** the entire job on that path, and one of those tags is
`DNC` — the flag LDR-Automation-Clean's nurture system reads to suppress
texting and calling. A dropped 4xx/5xx therefore produces a lead in FUB that
looks like every other lead, on a do-not-call owner. That is TCPA exposure
created by an unchecked return value.

**Fix.** `raise_for_status()` on the PUT. Failure returns `None`, so the lead
stays `awaiting_approval` and retries next run — safe because tags are merged,
not appended. Tested both directions (failure is not success; success still
tags and returns the id).

## 3. High — paid BatchData matches cached as no-matches

**Where:** `skiptrace/batchdata.py`
**Commit:** `8d768eb`

Provider results are mapped back to our requests by normalized street + ZIP.
When BatchData normalizes differently than we do, the lookup misses and the
request falls through to the no-match branch. Reproduced:

```
provider meta says matchCount=2 (billed for 2)
our result: matched=[False, False] errors=[None, None]
-> paid matches recorded as no-match AND cached for 90 days
```

Three costs stack: the money is spent, the contact info is discarded, and the
no-match cache then blocks a re-trace for `no_match_retrace_days` (90). At the
limit — a provider-side format change — this silently zeroes the match rate
while the diagnostics table shows a healthy "0 errors".

**Fix.** `meta.matchCount` is ground truth. If we align fewer results than the
provider says it matched, the whole chunk becomes an error: never cached,
retried next run, and loud in the summary. Guarded against over-correction with
tests for aligned matches and for a normal partial-match chunk (1 match + 1
genuine no-match).

## 4. High — total failures exited 0 and pinged the switch green

**Where:** `health.py:collect_stage_errors`
**Commit:** `014194f`

`collect_stage_errors` only looked for stages that *recorded* an exception.
Every stage in the runners is wrapped in `try/except` so one bad county cannot
kill a run — which means the failures that matter most are the ones where
everything "succeeded" and produced nothing. Reproduced:

```
PROBE3 all 50 skip traces returned HTTP 403 -> failures: []
  -> exit code 0, healthcheck GREEN
PROBE4 all 42 FUB pushes failed             -> failures: []
PROBE5 zero parcel rows for every county    -> failures: []
```

The first of those is not hypothetical — it is the incident this repo already
had, the one that poisoned 64 cache entries. It would have gone green again.

**Fix.** The verdict now covers silent failure: every trace erroring, every
push failing, a parcel sync keeping zero rows, a tripped truncation guard, an
unreadable inbox CSV. Each fails the run and pings `/fail`. A partial error
rate stays a warning (over-correction test included). The run summary now
**leads** with the verdict rather than burying it under twelve diagnostic rows,
and both cron runners ping FAIL from a top-level handler so a crash outside the
per-stage guards trips the switch immediately instead of leaving healthchecks.io
waiting a week.

## 5. High — the manual workflow reset the dead-man's switch

**Where:** `run_push_approved.py`, `.github/workflows/push-approved.yml`
**Commit:** `014194f`

`run_push_approved.py` called `ping_healthcheck()` unconditionally — no failure
argument, no separate URL. It shares `HEALTHCHECK_URL` with both cron runs.

So the sequence "weekly cron silently stops firing → Peter notices leads have
gone quiet → triggers Push Approved by hand" **resets the very timer that was
about to alert him.** The workflow also returned `0` unconditionally, so a run
where every push failed reported success, and it skipped the state size guard
the other two runners run.

**Fix.** It no longer pings anything (secret removed from the workflow too, with
the reasoning in a comment), returns 1 on failure, and runs the size guard. A
test asserts `ping_healthcheck` stays absent from that file.

## 6. High — unbounded recurring Claude spend on divorce matching

**Where:** `sources/divorce.py`
**Commit:** `b611e75`

`match_filings_to_owners` selected `WHERE matched_prop_id IS NULL`, and matching
costs one Claude call **per party name**. A case whose parties own no property
in our counties never matches — the common case for any county-wide filing
list — so every such case was re-sent to the API on every weekly run, forever.

The bill is monotonic in the number of filings ever ingested. Nothing bounded
it. This has not hurt yet only because the divorce source is still stubbed; it
would begin the week the first vendor CSV lands, which is exactly what the
roadmap recommends doing.

**Fix.** Capped at `MAX_MATCH_ATTEMPTS` (3) via new `match_attempts` /
`last_attempt_at` columns (migration v4 — `CREATE TABLE IF NOT EXISTS` does not
alter an existing table). The attempt is counted *before* the call so a mid-loop
crash cannot grant a free retry. Three attempts still covers the only real
reasons to retry: a quarterly parcel refresh or a deed import changing the
candidate set.

## 7. Medium — the size guard could not stop the push

**Where:** `.github/actions/state-sync/action.yml`
**Commit:** `402ff25`

`state.check_state_size()` raises inside the Python process. Every workflow runs
the state-sync push with `if: always()`. So a run that failed the guard — or
crashed before reaching it — pushed anyway. GitHub hard-rejects blobs over
100 MB, which fails the push *after* the commit exists, leaving the state branch
needing manual repair. The guard could not prevent the outcome it was written
for.

**Fix.** The action checks the encrypted file and refuses to commit above 95 MB,
so the last good state stays on the branch and the next run resumes from it.

*Since:* the push moved out of shell and into `state_sync.push` when the
concurrent-safe protocol was ported from LDR-Automation-Clean. The guard moved
with it — same 95 MB, still ahead of the commit — and now has a test
(`test_an_oversized_encrypted_db_is_refused_before_it_is_committed`).

## 8. Medium — provider consumer profiles stored and never read

**Where:** `skiptrace/tracer.py`, `state.py`
**Commit:** `402ff25`

`skip_traces.raw` stored the entire BatchData person record, truncated at
100 KB: relatives, address history, age/DOB, associated identities. I grepped
every read of that table — **no code path has ever read the column.** The
pipeline uses the `emails` / `phones` / `dnc` / `litigator` columns beside it.

Two problems. First, data minimisation: this is unread third-party identity
data about homeowners who have not contacted us, in a repo whose entire
retention story is "encrypted state branch". Second, size: at up to 100 KB per
owner, roughly 900 traced owners would breach the 100 MB committed-DB ceiling
on this column alone.

**Fix.** Stores non-identifying provenance only (provider, matched, counts, and
response *field names* — never values). Migration v5 purges existing payloads
and VACUUMs. Cached match results and contact info are untouched, so no owner is
re-traced or re-billed. Writing the re-runnability test for this caught v5
running `VACUUM` inside an open transaction before it shipped.

## 9. Medium — `DRY_RUN` spent money

**Where:** `fub.py:push_lead`, `sources/divorce.py`
**Commit:** `8d768eb`, `b611e75`

The Claude note call sat *above* the `DRY_RUN` bail-out in `push_lead`, so a run
documented as "no skip-trace spend, no FUB push" billed one Anthropic completion
per qualified lead. Reproduced: `DRY_RUN=True -> Anthropic summary calls: 1`.
Divorce matching had no `DRY_RUN` gate at all.

Both fixed. The dedupe lookup is deliberately kept in dry runs — it is a free
read and proves the dedupe path works, which is the point of a dry run.

## 10. Medium — the suite passed with a dead entry point

**Where:** `tests/test_pipeline.py`
**Commit:** `881c275`

`tests/` opens with its own `sys.path.insert` into `src/` — a convenience the
runners do not get. They use a CWD-relative `sys.path.insert(0, "src")` and are
invoked as `python3 run_weekly_pull.py` from the repo root. Nothing exercised
that. Demonstrated by breaking an import in `run_weekly_pull.py`:

```
=== production entry point now broken ===
ImportError: cannot import name 'this_does_not_exist' from 'seller_finder.fub'
=== but the test suite says ===
98 passed
```

The Monday run would have died on the first line of the job.

**Fix.** Four tests, no `conftest.py` and no `PYTHONPATH` — if production needs
a path fixup, production must provide it. Each entry point imports in a fresh
interpreter from the repo root (loaded under a non-`__main__` name, so imports
run but `main()` does not); each imports with **every secret set to `""`**,
which is what Actions passes for an unset secret; every module in the package
imports (`arcgis.py` had no importer until this); and workflows only invoke
runners that exist. All four verified to fail when the corresponding thing is
broken.

## 11. Medium — the mirror refresh could never run on Peter's Mac

**Where:** `scripts/refresh_parcel_mirror.sh`
**Commit:** `402ff25`

Line 22 used `declare -A`, a bash 4 feature. macOS ships bash 3.2:

```
GNU bash, version 3.2.57(1)-release (arm64-apple-darwin25)
declare: -A: invalid option
```

The README instructs running this from your own computer quarterly, precisely
because TxGIO blocks datacenter IPs. It would have failed immediately, every
time. Rewritten portably (verified under `/bin/bash` 3.2), with a tool-presence
preflight and an optional per-county filter.

## 12. Medium — the documented installer would revert the live workflow

**Where:** `workflows-pending/`, `scripts/install_workflows.sh`
**Commit:** `402ff25`

`workflows-pending/` duplicated `.github/workflows/` — two files byte-identical,
one differing only in a stale comment. Ambiguous source of truth, and the README
still pointed at it. Worse, `install_workflows.sh` copies that directory *over*
`.github/workflows/` and pushes to `main`, so following the documented
instructions today would silently revert the live push-approved workflow. Both
removed; the workflows have been installed since `2bf5f1d`.

## 13. Medium — tautological tests

**Where:** `tests/test_pipeline.py`
**Commit:** `881c275`

- `test_dnc_flag_adds_dnc_tag` built the tag list **in the test body** and
  asserted its own list contained `"DNC"`. Production code never ran; it would
  have passed if `push_lead` did nothing at all. Note this is the test
  nominally covering finding #2. Now captures the payload `push_lead` actually
  sends, with an over-correction guard that a clean owner is *not* tagged DNC
  (which would suppress outreach to every lead we push).
- `test_parcel_snapshot_meta_written` inserted the row itself, then asserted it
  equalled a module global the test had also set. Replaced with three tests that
  run `sync_county` for real and cover the light-sync rule properly — including
  that a **changed** asset key must *not* light-sync, or an owner change
  arriving in a quarterly refresh is missed forever.
- `assert stats1["cached"] == 1 or calls["n"] == 1` — an `or` lets either half
  carry the assertion. Both now asserted.
- `_LAST_ASSET_KEY` is a module global written by two tests, one asserting a key
  is absent: order-dependent. Now saved/restored by fixture.

## 14. Medium — README promised an approval gate that does not exist

**Where:** `README.md` Compliance section
**Commit:** `3b40293`

> "**Human in the loop.** Nothing reaches FUB without you triggering the push
> workflow after reviewing the CSV."

False since `fed9e8f` removed the gate. Every run auto-pushes contactable leads
into a live CRM. Wrong documentation about an automated outbound write is worse
than none — and this sits in the section someone would read to answer a
compliance question. Rewritten to describe what happens, what the audit record
is, and how to get a real gate back (unset `FUB_API_KEY` on the crons).

Also corrected: test count (48 → actual), retention (30d weekly / 14d daily),
the removed staging directory, and the size-guard/diagnostics descriptions.
Four docs tests now enforce this — two of which caught items I had missed in
that same commit.

## 15. Low — robustness and consistency

All in `8d768eb` / `b611e75`:

- **`divorce.py`** parsed the model's reply *outside* the `try`, so a
  non-numeric confidence (`"high"`) raised `ValueError` out of the whole
  function and took the entire divorce stage down.
- **`divorce.INBOX_DIR`** was a module-level constant resolved at import, so
  this module read a different inbox than `deeds.py` and `preforeclosure.py`
  whenever `DATA_DIR` was overridden. Now resolved per call, like its siblings.
- **`push_approved_leads`** had no `FUB_API_KEY` guard while `auto_push_leads`
  did, so a missing secret surfaced as N per-lead failures instead of one named
  error.
- **Missing `ANTHROPIC_API_KEY`** in divorce matching was reported as a client
  construction failure rather than a named optional-secret skip.
- **`_diagnostics_md`** now surfaces exemption truncation and deed-file parse
  errors, and the `DRY_RUN` tracer path reports month-to-date spend again (its
  early return skipped it, so dry runs showed `$0.00` MTD — exactly when you
  would check a budget).

---

## 4. Verified correct — no change needed

Things the brief asked me to re-verify that were already right. Several now have
tests they lacked.

**Idempotency.** Exercised four run patterns against one lead — Monday weekly,
Tuesday daily with the notice still in the feed, Wednesday daily after it
rotates out, and a hand-triggered same-day re-run:

```
run 1 (weekly, Mon)                api_calls=1 pushes=1 status=pushed
run 2 (daily, Tue - same notice)   api_calls=1 pushes=1 status=pushed
run 3 (daily, Wed - notice gone)   api_calls=1 pushes=1 status=pushed
run 4 (re-run same day)            api_calls=1 pushes=1 status=pushed
```

Exactly one billable trace and one FUB person. **Crash mid-run** also holds: a
run that dies after tracing keeps the paid result (the state push is
`if: always()`), and the recovery run reuses it and completes the push without
re-billing. Now covered by four tests that did not exist.

**Trace budget at the API-call site.** Correct. The cap bounds what enters
`to_trace`, which is exactly what is handed to `provider.trace_batch()`. The
existing `test_budget_is_enforced_at_the_api_call_site` asserts on the request
count the provider received, not on the stats — the right way to test it. I
added that budget-deferred leads are picked up by the next run.

**Warm-tier leads never trace.** Correct and structurally enforced: warm rows
carry no `leads` row, and the eligibility query reads only `leads`. The
existing `ExplodingProvider` test is a good rail.

**Dedupe against FUB.** Correct, and notably well-built: `FubSearchError` means
a failed lookup can never be read as "no match found", which is the bug that
creates duplicate CRM records during an outage. Search order email → phone →
address, plus `deduplicate=true` as a second net. (One narrow caveat in #16.)

**120-day event persistence.** Correct. Signals carry their own `observed_at`
and expire from *that* date, not from `updated_at` (which `compute_scores`
rewrites every run — the bug a previous audit fixed). Both directions tested.

**Cache invalidation on mirror checksum change.** Correct. `asset_key` is
`{asset_id}:{size}`, and `gh release upload --clobber` mints a new asset id, so
a refresh always invalidates. The failure path is also right: `_LAST_ASSET_KEY`
is cleared when a mirror download fails, so a TxGIO-fallback run cannot record
the mirror's identity against different data. Light-sync behaviour now has real
tests (see #13).

**Migration re-runnability.** Correct, including the two I added. Now covered
end-to-end for v2–v5, with matched (already-paid-for) traces asserted to survive
every migration.

**Secrets never logged.** Verified by grep across all logging call sites. The
only `f"Bearer {…}"` occurrences are header construction. Failures log HTTP
status and response body, never the key.

**Scoring performance at scale.** Checked because `_owned_ten_plus_years` runs
two `SELECT`s per candidate — an N+1 over ~180K absentee parcels. Measured:
~7 seconds extrapolated to the full three-county candidate set. Both lookups hit
`WITHOUT ROWID` primary keys. **Not worth optimising**; I mention it so the next
reader does not re-derive it.

---

## Flagged, not fixed

Deliberately left alone — each is a judgment call I did not want to make
unilaterally. All three are in RECOMMENDATIONS.md with effort estimates.

**16. FUB address dedupe can match an unrelated person.** The third dedupe pass
searches FUB with `q=<street>`, a general full-text query. It can return a
person whose record merely *contains* that string, and we would then tag a
stranger as a seller lead for that property. Narrow — it only fires when email
and phone both miss — but the blast radius is a wrong contact in a live CRM. I
did not change it because tightening the match risks the opposite failure
(duplicate people), and picking that trade-off is a business call about which
error Peter would rather have.

**17. Warm-tier rows never expire.** A warm lead created from a signal that
later stops being observed (e.g. a foreclosure notice matched to a
non-absentee parcel, 30 points) is never re-scored to zero and never deleted,
because `compute_scores` skips candidates scoring 0. Rows accumulate slowly.
Pure bloat, no correctness impact, and the table is deliberately tiny (~40
bytes/row) — but it grows monotonically and the DB has a hard ceiling.

**18. DST drift.** Both crons are hardcoded `0 11` UTC = 6 AM CDT, which
becomes 5 AM CST in November. The workflows document this and tell you to
change it manually. It is a real (if minor) recurring operational chore.

---

## Changed vs. flagged

**Changed:** 15 defects fixed across 6 commits — 1,721 insertions, 378
deletions, 24 files. Two files deleted (`workflows-pending/`,
`install_workflows.sh`), one added (`arcgis.py`). Two new state migrations
(v4, v5), both tested re-runnable.

**Tests:** 67 → 115. Two tautological tests replaced with real ones, one weak
`or` assertion tightened, one source of order-dependent pollution removed. New
coverage for idempotency, crash recovery, migration re-runnability, entry-point
imports, PII minimisation, and documentation drift.

**Flagged:** 3 items above.

**Not run, per the brief:** nothing touched BatchData, FUB, SMTP, or
healthchecks.io. Every external boundary is mocked. No commits were pushed; the
branch is local on `main`.

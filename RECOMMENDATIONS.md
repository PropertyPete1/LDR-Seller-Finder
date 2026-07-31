# Upgrade Roadmap

Prioritized after the July 2026 production review (see FINDINGS.md). Effort is
**S** (< half a day), **M** (1–3 days), **L** (a week+). Every item states what
Peter has to do or pay for.

Ordering is deliberate. The single highest-leverage upgrade — deed dates — is
**P1, not P0**, because activating it before the scoring rework in R3 would bury
the pipeline. That dependency is the most important thing in this document.

| # | Item | Effort | Impact | Peter pays / does |
|---|---|---|---|---|
| **R1** | Month-to-date spend ceiling | S | Cost: caps worst case | Nothing — config only |
| **R2** | Household-level skip-trace dedupe | M | Cost: up to ~4× on affected owners | Nothing |
| **R3** | Score decay + threshold rework | M | Volume: makes R4 usable | Nothing |
| **R4** | Deed dates at scale | M | **Volume: biggest single win** | One free email |
| **R5** | FUB outcome feedback loop | M | Quality: tunes scoring on real conversions | Nothing |
| **R6** | Dallas / Tarrant / Harris | M | Volume: ~3× parcel universe | ~$0–500/mo if paid feeds |
| **R7** | Divorce signal | S–L | Volume: +25 signal goes live | $0–100+/mo |
| **R8** | DST-stable cron | S | Reliability: no seasonal drift | Nothing |
| **R9** | The three flagged findings | S–M | Correctness / hygiene | Nothing (one decision) |
| **R10** | Provider abstraction hardening | S | Reliability: cheaper provider swap | Nothing |

---

## P0 — Do before scaling anything

### R1. Month-to-date spend ceiling — **S**

**Problem.** Budgets are **per run only**. `config.py` says so plainly:
"PER-RUN only — no month-to-date ceiling is enforced anywhere." The scheduled
worst case (~705 traces ≈ $105/mo) is fine, but nothing enforces it:

- every manual `workflow_dispatch` grants a **fresh** budget, so N hand-triggered
  runs cost N × 75 traces;
- a scoring change, a new county, or R4 raises the eligible pool and the budget
  is consumed to the cap every run instead of occasionally;
- `mtd_cost_usd` is already computed and displayed — it is **reporting only**,
  never a gate.

**Fix.** Add `skip_trace_monthly_ceiling` to settings.yaml and enforce it in
`tracer.trace_qualified_leads` alongside the per-run budget, using the
month-to-date count `_add_spend_stats` already derives from `skip_traces`.
Surface `mtd_traces / ceiling` in the diagnostics table, and treat hitting the
ceiling as a **warning, not a stage failure** — it is the system working.

**Impact.** Converts a soft assumption into a hard cap. Low upside, but it is
the cheapest insurance in this document and a precondition for R4 and R6, both
of which enlarge the eligible pool.

**Peter does:** nothing. Pick a number — $150/mo is ~2× the current scheduled
worst case and leaves headroom for manual runs.

### R2. Household-level skip-trace dedupe — **M**

**Problem.** The owner cache keys on the **raw** owner string plus mail ZIP.
Measured against real CAD name formats:

```
SMITH JOHN & MARY     -> key "SMITH JOHN & MARY|78209"    sends JOHN SMITH
SMITH MARY & JOHN     -> key "SMITH MARY & JOHN|78209"    sends MARY SMITH
SMITH JOHN A & MARY   -> key "SMITH JOHN A & MARY|78209"  sends JOHN SMITH
SMITH JOHN            -> key "SMITH JOHN|78209"           sends JOHN SMITH
=> 4 distinct cache keys for what is plausibly ONE household
```

Every distinct key is a separate BatchData bill for the same people. This fires
whenever a couple owns more than one parcel and the CAD recorded the names
inconsistently across records — common, because the two rows are often entered
years apart. The README's central cost claim ("the same owner is **never**
billed twice") is therefore true of the *string*, not of the household.

**Fix.** Key on a normalized household identity: surname + sorted set of given
first-initials + mail ZIP. Keep the raw string in a column for auditing.

**The trade-off, which is why I flagged rather than fixed it.** Loosening the
key risks the opposite error — merging two genuinely different `SMITH JOHN`s in
one ZIP, which would attach one person's phone number to another's property.
Cost saving vs. wrong-contact risk is a business call. My recommendation:
require surname + **all** first-initials + ZIP to match (so `SMITH J&M` never
merges with `SMITH J`), which captures the reordering and middle-initial cases
— the common ones — while refusing the ambiguous merges.

**Impact.** Direct, recurring reduction in the only per-unit cost in the system.
Magnitude depends on multi-property-owner density; measurable up front by
grouping `pc.parcels` on the proposed key and counting the collapse. **Do that
measurement first** — it is a 10-line query and tells you whether this is worth
the M.

**Peter does:** nothing, beyond approving the merge rule.

### R3. Score decay, recency weighting, threshold rework — **M**

**Problem, and why this blocks R4.** Deed dates promote absentee owners with 10+
years of tenure from 30 to 50 — over the threshold. Using the README's own
measured figure of ~155K Bexar absentee owners:

```
if 50% have 10+yr tenure -> 77,500 newly QUALIFIED at score 50
monthly trace ceiling today: 705 traces
years to work through them: 9
```

The queue is ordered `score DESC`, so genuinely urgent leads (absentee +
foreclosure = 60) still win — the mechanism is sound. But the *reporting*
collapses: "qualified" becomes a five-figure number that is 99.9% aspirational,
the digest's score bands stop meaning anything, and there is no way to see
whether this week's high-intent leads actually got traced. You would be flying
blind at exactly the moment volume arrives.

**Fix, in order:**

1. **Recency weighting.** A foreclosure notice filed this week and one from four
   months ago both score +30 today; retention is binary (120 days, then gone).
   Decay event signals linearly over the retention window so fresh notices
   outrank stale ones inside the same score band.
2. **Tenure banding.** 10 years and 30 years should not score identically. Band
   it (10–15 / 15–25 / 25+) so tenure discriminates instead of flattening 77K
   leads onto one number.
3. **Then raise `skip_trace_threshold`** so "qualified" again means "will
   plausibly be traced this quarter" — with tenure banded, absentee + long
   tenure alone should sit *below* it, and tenure should act as a tiebreaker
   among leads that have a second signal.
4. **Report queue depth.** Add "qualified but never traced, oldest N days" to
   the diagnostics table. Without it, a growing backlog is invisible.

All four are `scoring.py` and settings.yaml. No new data, no new spend.

**Impact.** No direct lead-volume change on its own — this is the work that
makes R4's volume *usable* rather than a number in a table.

**Peter does:** nothing.

---

## P1 — Highest leverage

### R4. Activate deed dates at scale — **M** (mostly waiting)

**Problem.** The tenure signal (+20) is fully wired and completely dormant: the
`deed_dates` table is empty, so the fallback is `owners_first_seen`, which needs
**ten years of this system's own history** to fire. It will contribute nothing
until 2036 without imported data.

**What to do.** Email **openrecords@bcad.org** requesting the current-year
appraisal data export (free, delivered via their FTP). Extract
`county, prop_id, deed_date`, drop it in `data/inbox/deeds_bexar.csv`, commit.
The next weekly run ingests it — genuinely zero code changes, and the
exactly-once ledger plus the four accepted date formats are already tested.
Comal: open-records request via comalad.org. Travis: TCAD equivalent.

**Sequencing — the important part.** Land **R3 first**, or at minimum R3.4
(queue-depth reporting) plus R1 (spend ceiling). Importing 700K deed dates into
today's configuration produces a five-figure qualified queue, a meaningless
digest, and a per-run budget that is silently the only thing standing between
you and the ceiling.

**Impact.** The largest single increase in qualified-lead volume available, and
the signal is genuinely predictive — long-tenure absentee owners are the classic
motivated-seller profile. Also the cheapest: one email.

**Peter does:** send one email; extract three columns from the response; commit
one CSV. Free.

### R5. FUB outcome feedback loop — **M**

**Problem.** The system has no idea which leads convert. Scoring weights (30 /
25 / 30 / 20 / 10) are reasonable priors that have never been checked against a
single outcome. Every other recommendation is guesswork until this exists.

**Architecture note — poll, don't webhook.** The brief suggests a webhook, but
there is no server anywhere in this design: it is GitHub Actions cron plus an
encrypted SQLite file. A webhook needs a public HTTPS endpoint, which means new
infrastructure, a new secret, and a new thing that can break silently. **Poll
instead.** The Monday run already talks to FUB; add a step that pulls people
tagged `Seller Lead` updated since the last run and records their current stage.
Same information, no new infrastructure, and it fits the existing failure model.

**Fix.** New `lead_outcomes` table (`fub_person_id`, `stage`, `stage_at`,
`checked_at`). In the weekly run, page `GET /v1/people` filtered by tag and
`updatedAfter`, upsert stages, then join back to `leads.signals` to produce a
conversion table by **signal combination** in the digest:

```
absentee + preforeclosure   142 pushed   11 responded   3 appointments
absentee + tenure           380 pushed    9 responded   1 appointment
```

That table is what turns scoring from opinion into measurement — and it directly
answers whether R4's volume is worth its spend.

**Impact.** No immediate volume change; compounding quality gains. It is what
lets you raise the weight on signals that convert and cut spend on ones that do
not. Do it *before* R6/R7 so expansion decisions have evidence behind them.

**Peter does:** nothing — reuses the existing `FUB_API_KEY`. One decision: which
FUB stages count as "converted".

---

## P2 — Expansion

### R6. Enable Dallas, Tarrant, Harris — **M** (per county, mostly operational)

Parcels are the easy half and already scaffolded (`enabled: false`, FIPS codes
present). Absentee detection, warm tier, and scoring work in **any** Texas
county via TxGIO with no code changes.

Per county: add the name to `counties`, add it to `COUNTIES` in
`scripts/refresh_parcel_mirror.sh`, run that script **from home** (it now works
on macOS — see FINDINGS #11), which uploads `{county}_parcels.zip` to the
release mirror.

Foreclosures are the hard half, and each county differs:

| County | Foreclosure reality | Recommendation |
|---|---|---|
| **Dallas** | `dallas.tx.publicsearch.us` FC department, client-side JS rendering | CSV inbox monthly, or paid feed |
| **Tarrant** | Same platform, same limitations | Same |
| **Harris** | `cclerk.hctx.net FRCL_R.aspx` — anonymous access, ASP.NET WebForms, scrapeable but fragile viewstate paging | Same. **Do not build the scraper** |

I'd hold the line on the existing no-fragile-scraper policy. A viewstate scraper
against a county clerk is a maintenance liability that breaks silently — and
this codebase's whole failure history is silent failures. The CSV inbox already
handles any vendor list with zero code changes.

**Sequencing.** Harris alone roughly doubles the parcel universe. Land R1 + R3
first or the trace budget becomes the binding constraint on three new counties
at once.

**Impact.** Roughly 3× the absentee/warm universe. Foreclosure timeliness only
arrives with a feed.

**Peter pays:** $0 for parcels/absentee/tenure. For foreclosures everywhere at
once: ATTOM (~$500+/mo), PropertyRadar, or per-county Foreclosures Daily CSVs
(cheaper). **Start with the CSV inbox and one county** to measure conversion
before committing to a subscription — R5 is what makes that measurable.

### R7. Divorce signal — **S to L depending on route**

Matching is fully built and live; only the feed is stubbed. Three routes:

| Route | Effort | Cost | Notes |
|---|---|---|---|
| **Standing open-records request** to the Bexar District Clerk | **S** — one email, then a weekly CSV drop | Free / small copy fee | **Start here.** Zero code. Many TX clerks fulfil standing requests as spreadsheets |
| **Vendor CSV** (CourthouseDirect, Foreclosures Daily) | **S** — drop in `data/inbox/` | Per-list | Same path, no integration |
| **UniCourt API** | **M** — implement `fetch()` | ~$100+/mo | Only worth it once volume justifies it |

**Do R1 and the FINDINGS #6 fix first** — the attempt cap is already committed,
and without it the first real filing list would have started an unbounded
recurring Claude bill.

**Peter does:** send one records request. Free, and it tests the signal's value
before anyone pays a vendor.

---

## P3 — Hygiene

### R8. DST-stable cron — **S**

Both crons are hardcoded `0 11` UTC = 6 AM CDT, silently becoming **5 AM CST**
in November. The workflows tell you to edit them twice a year, which is a chore
that will eventually be forgotten.

**Fix.** Schedule at **both** `0 11` and `0 12` UTC, and make the first step a
guard:

```yaml
- name: Skip unless it is 6 AM in Central Time
  run: |
    [ "$(TZ=America/Chicago date +%H)" = "06" ] || { echo "not 6 AM CT — skipping"; exit 0; }
```

Exactly one run per day year-round, no seasonal edits. The existing
`ldr-seller-state` concurrency group already prevents overlap. Note the guard
must `exit 0` and the remaining steps need an `if:` condition, or use a job-level
`if` on a computed output — a non-zero exit would look like a failure.

**Peter does:** nothing.

### R9. The three flagged findings — **S to M**

- **FUB address dedupe (`q=<street>`)** — a general full-text search that can
  match a person whose record merely contains that string, tagging a stranger as
  a seller lead. Narrow (only fires when email and phone both miss) but the
  blast radius is a wrong contact in a live CRM. **Needs your decision:**
  tightening it risks the opposite error (duplicate people). My recommendation
  is to drop the address pass entirely once R5 shows how rarely it is the
  deciding match — `deduplicate=true` on create is already a second net.
- **Warm-tier rows never expire** — **S.** Add a `TTL` sweep: delete warm rows
  not re-scored in N days. Pure bloat today, but the committed DB has a hard
  ceiling and the table only grows.
- **Scoring N+1** — **no action.** Measured at ~7s for the full three-county
  candidate set. Documented in FINDINGS so nobody re-derives it.

### R10. Provider abstraction hardening — **S**

`SkipTraceProvider` is a clean interface, but `PROVIDERS` has one entry and
`get_provider()` is called with a hardcoded default. The alignment guard from
FINDINGS #3 is BatchData-specific and lives in the provider — correct, but it
means a second provider silently starts without that protection.

**Fix.** Move the "results must align with requests" contract into `base.py` as a
shared post-condition helper, so any future provider inherits it. Cheap now,
and it is the difference between swapping providers in a day and rediscovering
this class of bug a third time.

---

## Suggested sequence

```
R1 (spend ceiling, S)  ─┐
R8 (DST cron, S)        ├─ this week, all cheap, all reduce risk
R9 warm-tier TTL (S)   ─┘
        │
R3 (scoring rework, M) ── unblocks R4
        │
R4 (deed dates, M) ────── send the BCAD email NOW; it is free and has latency
        │
R5 (FUB feedback, M) ──── makes every later decision evidence-based
        │
R2 (household dedupe, M) ─ measure the collapse first; skip if small
        │
R6 / R7 (expansion) ───── with conversion data in hand
```

Send the BCAD open-records email and the Bexar District Clerk records request
**today**, regardless of where the code work sits — both are free, both have
multi-week turnaround, and both are pure upside sitting in someone else's queue.

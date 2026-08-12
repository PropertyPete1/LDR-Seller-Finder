# LDR-Seller-Finder

Seller lead generation system for Lifestyle Design Realty. It pulls public property and court data for **Bexar, Comal, and Travis counties**, identifies homeowners who are statistically likely to sell, scores them, skip-traces the qualified ones for contact information, and **auto-pushes contactable leads straight into Follow Up Boss** — tagged, deduped, and annotated. Leads with no email and no phone are held (never pushed), and DNC/litigator-flagged owners carry a `DNC` tag so the nurture system suppresses texting/calling.

Two schedules share one state DB, one healthcheck, and the same diagnostics table:

| Run | Schedule | Scope |
|---|---|---|
| **Weekly** (`run_weekly_pull.py`) | Monday 6 AM CT | Full pipeline: mirror parcel refresh + owner-change detection, exemption diffs, divorce/deeds inbox, scoring, tracing (75-trace budget), FUB push, digest email |
| **Daily** (`run_daily_pull.py`) | Tue–Sat 6 AM CT | Fast path: light parcel sync (skips owner-change bookkeeping when the mirror asset checksum is unchanged), NEW pre-foreclosure notices, scoring, tracing (15-trace budget), FUB push. No digest/exemptions/divorce/deeds |

> The spec said "daily Mon–Sat": Monday is covered by the full weekly run, so the daily cron runs Tue–Sat to avoid a duplicate same-day run (and double spend).

**This repo finds and stages leads. It never contacts them.** All outreach (emails, texts, campaigns) stays in [LDR-Automation-Clean](https://github.com/PropertyPete1/LDR-Automation-Clean), which handles CAN-SPAM compliance, unsubscribe handling, and send-time rules.

---

## How It Works

```
         ┌────────────────────────────────────────────────────────────────┐
         │  Weekly (Mon 6 AM CT, full)  ·  Daily (Tue–Sat 6 AM CT, fast) │
         └────────────────────────────────────────────────────────────────┘
  TxGIO parcels (Bexar+Comal+Travis) ─┐
  Bexar exemptions (ArcGIS, weekly) ──┤
  Pre-foreclosures (Bexar live,     ──┼──► Scoring (0–100) ─┬─► score ≥ 40?
     Travis CSV inbox)                │                     │
  Divorce filings (stubbed)  ────────┘    30–39 = WARM TIER ◀┘ (stored, zero
                                          spend; auto-promotes on new signals)
                                                           │ ≥ 40
                                                           ▼
                                              Skip trace (BatchData, cached,
                                              budget-capped, never re-billed)
                                                           │
                                                           ▼
                                       Contactable (email or phone found)?
                                        │ yes                    │ no
                                        ▼                        ▼
                 ┌──────────────────────────────┐      HELD (never pushed)
                 │ AUTO-PUSH → Follow Up Boss   │      status=held_no_contact
                 │ tags + notes, deduped;       │
                 │ DNC tag for flagged owners   │
                 └──────────────────────────────┘
                                        │
                                        ▼
                    pending_leads.csv artifact (push record)
                    + run summary + digest email (weekly only)
```

The **Push Approved Leads** workflow still exists as a manual fallback: it retries leads that failed to push and re-checks held leads for newly-found contact info.

### State persistence — two databases, one committed

State is split across two SQLite files (GitHub rejects committed files over 100 MB, and a full two-county parcel snapshot alone is 200+ MB):

| File | Contents | Lifecycle |
|---|---|---|
| `data/seller_finder.sqlite3` | Leads (qualified only), skip-trace cache, run history, divorce cases, deed dates, plus compact derived attributes: `owners_first_seen` (tenure baseline + owner-change detection for absentee parcels) and `exempt_parcels` (homestead diff snapshot) | AES-256 encrypted and synced to the orphan `state` branch — the exact same pattern as LDR-Automation-Clean. Stays ~10 MB for two counties, well under 100 MB even at 5+ counties. A size guard runs in two places: `state.check_state_size()` fails the run at 90 MB, and the state-sync action **refuses to commit** an encrypted DB over 95 MB — the push step is `if: always()`, so the in-process guard alone could not stop a run that crashed before reaching it |
| `data/parcels_cache.sqlite3` | The full raw parcel snapshot (`pc.parcels`) | **Ephemeral and gitignored** — rebuilt from the release mirror download on every weekly run and ATTACHed to the main connection, so cross-table joins work transparently. Never committed |

Sub-threshold candidates (e.g. absentee-only at 30 points) are recomputed from the parcel cache each run and are *not* stored — only qualified leads (40+) persist. Opening a pre-split state DB triggers an automatic one-time migration that salvages the compact attributes, drops the heavy parcels table, and VACUUMs.

Every run's job summary **opens with a health verdict** — either `✅ All pipeline stages healthy` or a list of the stages that failed — followed by a **Pipeline diagnostics** table (parcel rows/kept/absentee per county, foreclosure notices fetched/matched, scoring funnel, skip-trace and push outcomes).

The verdict is not cosmetic: it drives the exit code and the healthchecks.io ping. Because every stage is wrapped in `try/except` so one bad county cannot kill a run, the process would otherwise exit 0 after a total failure. `health.collect_stage_errors()` catches both recorded exceptions **and** silent failures that raise nothing — every skip-trace erroring (revoked token / empty PayGo wallet), every FUB push failing, a parcel sync keeping zero rows, a truncated exemption feed, an unreadable inbox CSV. Any of those fails the run and pings `/fail`.

---

## Scoring Model

| Signal | Points | Status | How it's detected |
|---|---|---|---|
| Absentee owner | +30 | ✅ Live | Owner mailing address ≠ property address (normalized street + ZIP comparison) |
| Pre-foreclosure notice | +30 | ✅ Live (Bexar) | Notice of Trustee Sale from the County Clerk's public feed, matched to the parcel by address |
| Divorce filing match | +25 | ⚠️ Stubbed (see below) | Party name fuzzy-matched to parcel owner using Claude (`claude-sonnet-4-6`) |
| Owned 10+ years | +20 | ⚠️ Wired — auto-ingests from `data/inbox/` (see below) | Deed date from the free BCAD export (one open-records email), or 10 years of unchanged ownership in our own history |
| Homestead exemption removed | +10 | ✅ Live (Bexar) | `HS` code present in the previous pull, missing in the current one |

Leads scoring **40 or higher** are skip-traced and auto-pushed (if contactable). Weights, thresholds, and budgets are tunable in `config/settings.yaml` — no code changes needed.

Score examples: absentee alone = 30 (warm tier — stored, never traced). Absentee + pre-foreclosure = 60 (traced). Absentee + divorce = 55 (traced). Absentee + homestead removed = 40 (traced).

### Warm tier (30–39) — zero-spend pipeline

Leads scoring `warm_tier_min` (30) up to threshold−1 — today that's ~180K absentee-only owners — are scored and stored in the compact `warm_leads` table (county, prop_id, score, signal names only; full details are joined from the parcel cache at runtime, keeping 150K+ rows to a few MB). Warm leads are **never skip-traced and never pushed** — zero spend. When a later run adds a signal (deed date import, divorce match, new foreclosure notice), the lead crosses the threshold, **auto-promotes to qualified**, and flows through tracing and push like any other lead. Warm counts (scored this run / total stored / promoted) appear in every run's diagnostics table.

---

## Data Sources — What's Real, What's Stubbed, and Why

This is the honest state of public data access as of July 2026. Every claim below was verified by actually hitting the endpoints during development.

### 1. Parcel + owner data (Bexar, Comal & Travis) — ✅ CLEAN, FREE, BULK

No CAD in our footprint offers a self-serve bulk download on their website. Instead we use the **TxGIO StratMap Land Parcels program** (Texas Geographic Information Office / TWDB), which collects every CAD's certified roll, normalizes it, and republishes per-county FileGDB downloads under a **CC0 license** — this is what makes new counties a 3-line config change:

- API: `https://api.tnris.org/api/v1/collections?search=land+parcels` → newest "Land Parcels" collection → `/resources?collection_id=…&area_type_name=Bexar` returns the zip URL.
- Verified: Comal file = 103,537 parcels (tax year 2025) with `OWNER_NAME`, `SITUS_*`, `MAIL_*`, `MKT_VALUE`. Bexar ≈ 710K parcels.
- Refresh cadence is roughly annual. That's fine: parcels change slowly, and the signals that make leads *timely* (foreclosure, divorce, exemption changes) come from feeds that update monthly/weekly.

#### Parcel data mirror — why and how (IMPORTANT)

**TxGIO's CloudFront blocks GitHub Actions datacenter IPs at the IP level** — confirmed in live Actions runs (403 on both county zips regardless of user agent or retries; the same download works from residential networks). No official alternate endpoint exists: the api.tnris.org resource URLs all point at the same CloudFront distribution, and no public S3 origin is exposed.

So the pipeline downloads parcel zips in this order:

1. **PRIMARY — GitHub Release mirror**: the same CC0 zips are attached as assets to this repo's [`parcel-data-2025` release](https://github.com/PropertyPete1/LDR-Seller-Finder/releases/tag/parcel-data-2025) (`bexar_parcels.zip`, `comal_parcels.zip`, `travis_parcels.zip`). github.com is always reachable from Actions runners. Auth uses `GITHUB_TOKEN`/`GH_TOKEN` if set, otherwise the credentials `actions/checkout` persists in `.git/config` — so **no workflow changes were needed**. **This path is private-repo-safe**: every request (release metadata lookup by tag *and* the `Accept: application/octet-stream` asset download via `asset.url`) goes through `api.github.com` with a Bearer token — nothing uses unauthenticated `browser_download_url` or raw URLs. The default `GITHUB_TOKEN` on Actions always has read access to its own repo's releases, so the mirror keeps working after the repo is flipped to private (verified with an authenticated end-to-end download).
2. **FALLBACK — TxGIO direct**: the original download path (browser UA + retries), which works from residential/office IPs and covers the case where a mirror asset is missing.

**Quarterly refresh process** (parcel data changes slowly — set a calendar reminder for Jan/Apr/Jul/Oct, or just refresh when TxGIO publishes a new StratMap vintage):

```bash
# From your own computer (home/office network — NOT a datacenter/VPS):
git clone https://github.com/PropertyPete1/LDR-Seller-Finder && cd LDR-Seller-Finder
bash scripts/refresh_parcel_mirror.sh   # downloads fresh zips from TxGIO, replaces the release assets
```

The script auto-detects the newest TxGIO Land Parcels collection, verifies each zip, and uploads with `--clobber`. When TxGIO publishes a new vintage (e.g. StratMap 2026), bump `MIRROR_TAG` in `src/seller_finder/sources/parcels.py` and `TAG` in the script to `parcel-data-2026`, create the new release, and run the script. Adding a new county? Add its zip to the same release as `{county}_parcels.zip` (the refresh script picks up counties from its `COUNTIES` map).

If you ever want CAD-direct data (fresher, includes deed dates): email **openrecords@bcad.org** — BCAD provides its full appraisal export via FTP for free (current year). Comal AD takes open-records requests at their office/email; no standing export exists.

### 2. Homestead exemptions — ✅ LIVE for Bexar, ❌ not available free for Comal

Bexar County's public parcel layer exposes an `Exempts` field (verified: 405,824 parcels carry `HS`):

- `https://maps.bexar.org/arcgis/rest/services/Parcels/MapServer/0` — paged at 1,000 records, joined to TxGIO parcels on `PropID`.

The module compares each pull to the previous one and emits **homestead-removed** events. Comal has no free exemption feed; the module skips counties without an `exemption_sources` entry and logs it.

### 3. Pre-foreclosure notices — ✅ LIVE for Bexar, CSV inbox for Travis

The Bexar County Clerk publishes the current month's Notice of Trustee Sale filings through the public ArcGIS service that powers the county's [Foreclosure Map](https://maps.bexar.org/foreclosures/):

- Mortgage: `https://maps.bexar.org/arcgis/rest/services/CC/ForeclosuresProd/MapServer/0`
- Tax: `.../MapServer/1`
- Fields: `ADDRESS, DOC_NUMBER, YEAR, MONTH, TYPE, CITY, ZIP` (verified: 457 notices for the August 2026 sale).

The feed has no owner name, so notices are matched to parcels by normalized address + ZIP to attach the owner.

Counties **without** a clean feed use the **CSV inbox**: drop `data/inbox/foreclosures_<county>_YYYY-MM.csv` with columns `address, zip, doc_number[, city, sale_date]`, commit, and the next run ingests + matches it exactly like a live feed (files rename to `.done` after processing, so nothing double-ingests).

#### County foreclosure sources — status + what's needed to flip each on

Researched July 2026 (every endpoint below was actually probed):

| County | Parcels/absentee | Foreclosure notices | To activate foreclosures |
|---|---|---|---|
| **Bexar** ✅ live | TxGIO | ✅ ArcGIS feed (auto) | — already live |
| **Comal** ✅ live | TxGIO | ❌ Courthouse posting only, no digital feed | CSV inbox works if you ever transcribe postings; realistically needs a paid API |
| **Travis** ✅ live | TxGIO (mirror asset uploaded) | ⚠️ CSV inbox. tccsearch.org (Aumentum) is CAPTCHA + login — no automation. travis.tx.publicsearch.us has an FC department but it returns "No documents to search" / query errors (empty/not migrated) | Monthly: export Notice-of-Trustee-Sale list from tccsearch.org manually → drop CSV in inbox. Or a paid API (below) |
| **Dallas** ⚠️ scaffolded | TxGIO 48113 (config present, `enabled: false`) | dallas.tx.publicsearch.us FC department (notices since 2026-02); client-side JS rendering — fragile to scrape | Add `dallas` to `counties`, upload parcel zip via refresh script; foreclosures via CSV inbox or paid API |
| **Tarrant** ⚠️ scaffolded | TxGIO 48439 | tarrant.tx.publicsearch.us — same platform/limitations as Dallas | Same as Dallas |
| **Harris** ⚠️ scaffolded | TxGIO 48201 | cclerk.hctx.net `FRCL_R.aspx` (anonymous OK, ASP.NET WebForms — scrapeable but fragile viewstate pagination) | Same pattern; the ASP.NET search is the least-bad free option if we ever accept scraper risk |

**Paid options that solve foreclosures everywhere at once:** ATTOM (pre-foreclosure API, ~$500+/mo), PropertyRadar, or Foreclosures Daily CSV subscriptions (per-county, cheaper). The CSV inbox means any vendor list drops straight in with zero code changes.

> Note: absentee + warm-tier scoring works in **every** Texas county via TxGIO regardless of foreclosure feed status — a county without foreclosure data still produces absentee leads that auto-promote when deed dates land.

### 4. Divorce filings — ⚠️ STUBBED (no clean access exists)

Bexar County court records were consolidated into the **Tyler Odyssey portal** (`portal-txbexar.tylertech.cloud`) after search.bexar.org was retired. Verified during development:

- The portal sits behind a CAPTCHA (blocked our first request).
- Smart Search **requires a record number or party name** — you cannot list "all divorce cases filed last week."
- There is no public API or bulk export; registered access is reserved for justice partners.

Per project policy, we **stub rather than build a fragile scraper**. However, the entire downstream half is built and tested: drop a CSV into `data/inbox/divorce_YYYY-MM-DD.csv` with columns `case_number, filed_date, petitioner, respondent` and the pipeline ingests it, fuzzy-matches party names to parcel owners with Claude, and scores the matches. Three ways to light this up:

| Option | Effort | Cost | Notes |
|---|---|---|---|
| **UniCourt API** (or ATTOM pre-divorce leads) | Low — implement `fetch()` against their API | ~$100+/mo | True API access to TX district court dockets; cleanest option |
| **Standing open-records request** to the Bexar District Clerk ([records request page](https://www.bexar.org/3500/Public-Records-Requests)) for a weekly list of new family-case filings | One email + weekly CSV drop | Free/small copy fee | Many Texas clerks fulfill standing requests as spreadsheets |
| **Lead vendors** (CourthouseDirect, Foreclosures Daily) | CSV drop only | Per-list | Same CSV inbox path |

### 5. "Owned 10+ years" — ⚠️ WIRED, awaiting deed data (one free email unlocks it)

**Exhaustively researched (Jul 2026): no free bulk source with deed/purchase dates is directly downloadable.** Ruled out: TxGIO parcels (`DATE_ACQ` is the data-collection date, not deed date), the Bexar County ArcGIS parcel layer (no deed/sale fields — full field list verified), the Texas Comptroller EARS/EPTS appraisal rolls (inbound-only secured SFTP, never published per county), and the County Clerk's deed search at bexar.tx.publicsearch.us (interactive/CAPTCHA-gated site whose bulk export requires an account — and it only lists *new* transfers, the wrong shape for tenure anyway).

**The unlock — one free email:** send an open-records request to **openrecords@bcad.org** asking for the *current appraisal data export* (BCAD delivers it via their FTP, free for current-year data). It contains deed dates for all ~710K Bexar parcels. Comal AD equivalent: open-records request via [comalad.org](https://comalad.org/open-records-request/).

**The pipeline is already fully wired — no code changes needed when the data arrives.** Two activation paths:

1. **Inbox auto-ingest (preferred):** extract `county, prop_id, deed_date` from the export into a CSV named `deeds_*.csv`, drop it in `data/inbox/`, and commit. The next weekly run imports it automatically (files are renamed `*.imported` so they process exactly once), and every parcel with a 10+-year-old deed date immediately scores +20. Dates are accepted as `YYYY-MM-DD`, `MM/DD/YYYY`, `YYYYMMDD`, or bare `YYYY`. Alternatively run `python3 scripts/import_deed_dates.py deed_dates.csv` locally.
2. **Owner-history proxy (automatic fallback):** every weekly run logs owner changes in `owner_history`; unchanged ownership across 10+ years of accumulated history scores automatically.

**Impact preview (measured on live data):** ~155K Bexar absentee owners currently score 30 — just below the 40 threshold. Deed data promotes every absentee owner with 10+ years of tenure to 50 (qualified), so this one email is the single highest-leverage upgrade available.

---

## Repository Layout

```
LDR-Seller-Finder/
├── run_weekly_pull.py            # Monday cron entry point (full pipeline)
├── run_daily_pull.py             # Tue–Sat cron entry point (fast: new foreclosures → trace → push)
├── run_push_approved.py          # Manual fallback entry point (retry pushes to FUB)
├── config/settings.yaml          # Scoring weights, county sources, budgets — edit freely
├── src/seller_finder/
│   ├── config.py                 # Env secrets + settings loader
│   ├── arcgis.py                 # Shared ArcGIS client (200-with-error-body guard)
│   ├── state.py                  # Split-DB SQLite schema, legacy migration, size guard
│   ├── scoring.py                # 0–100 scoring engine
│   ├── fub.py                    # Follow Up Boss push: dedupe, tags, notes
│   ├── review.py                 # Review CSV, run summary, weekly digest email
│   ├── health.py                 # Dead-man's switch + run health verdict
│   ├── telemetry.py              # status/*.json writer for THE FLOOR (read-only, never fails a run)
│   ├── sources/
│   │   ├── parcels.py            # TxGIO bulk parcel loader (mirror-first, light sync on daily)
│   │   ├── exemptions.py         # Bexar homestead exemptions + removal detection
│   │   ├── preforeclosure.py     # Bexar live feed + CSV inbox (Travis et al)
│   │   ├── deeds.py              # Deed-date inbox auto-ingest (tenure signal)
│   │   └── divorce.py            # STUB + CSV inbox + Claude fuzzy matching (live)
│   └── skiptrace/
│       ├── base.py               # Provider interface (SkipTraceProvider)
│       ├── batchdata.py          # BatchData implementation
│       └── tracer.py             # Cache-aware, budget-capped orchestration
├── scripts/
│   ├── import_deed_dates.py      # One-off deed-date import (or use data/inbox/)
│   ├── refresh_parcel_mirror.sh  # Quarterly parcel-mirror refresh (run from home network;
│   │                             #   POSIX sh-compatible, works on macOS bash 3.2)
│   ├── e2e_live_test.py          # Live smoke test (dry-run, no spend)
│   └── live_bexar_test.py        # Full live Bexar E2E (real parcels + foreclosures)
├── tests/                        # Suite total is stated once, under Testing — and enforced
│   ├── test_pipeline.py          # Core pipeline: scoring, budgets, dedupe, migrations
│   └── test_telemetry.py         # Telemetry: absent≠zero, read-only, atomicity, no PII
├── status/                       # Published by the crons, read by LIFESTYLE ("THE FLOOR")
│   ├── seller_stats.json         # Today's + yesterday's counters
│   └── seller_log.json           # 100 most recent stage-level events
└── .github/
    ├── workflows/weekly-pull.yml     # Mon 6 AM CT cron (full)
    ├── workflows/daily-pull.yml      # Tue–Sat 6 AM CT cron (fast)
    ├── workflows/push-approved.yml   # Manual fallback (retry failed/held pushes)
    ├── workflows/jarvis-build.yml    # `jarvis-build` label on an issue → Claude implements it → PR
    ├── actions/state-sync/action.yml        # Encrypted SQLite ⇄ `state` branch
    └── actions/publish-telemetry/action.yml # status/*.json → main (never touches the working tree)
```

---

## Workflows

| Workflow | Trigger | What it does |
|---|---|---|
| **Weekly Data Pull** | Cron `0 11 * * 1` (Mon 6:00 AM CDT) + manual | Full pipeline **including auto-push to FUB**. Uploads `pending-leads` artifact (permanent push record: pushed / held / awaiting-retry), writes the job summary, emails the digest, pings healthchecks.io |
| **Daily Data Pull** | Cron `0 11 * * 2-6` (Tue–Sat 6:00 AM CDT) + manual | Fast path: light parcel sync from the mirror (owner-change bookkeeping skipped when the mirror asset is unchanged), new pre-foreclosure notices, scoring, tracing (15-trace budget), auto-push. Same state DB, healthcheck, and diagnostics table; no digest email |
| **Push Approved Leads** | `workflow_dispatch` (manual fallback) | Retries `awaiting_approval` (failed pushes / runs before FUB key existed) and `held_no_contact` leads. Optional `exclude_ids` input skips specific leads. Supports `dry_run` |
| **jarvis-build** | `jarvis-build` label applied to an issue | Hands the issue body to Claude Code as a build order; it implements the spec on a new branch and opens a PR. Ported from `lifestyle-brain`. **This costs money per run** — the label is the trigger, so applying it is the spend decision |

Both cron workflows share the `ldr-seller-state` concurrency group, so a long weekly run can never race a daily run on the state DB.

> **Editing workflow files:** all three workflows are live in `.github/workflows/`. A token without the `workflows` permission cannot push changes to them — if `git push` is rejected for that reason, edit the file through the GitHub web UI, or push with a PAT that carries the `workflow` scope. (The old workflows-pending staging directory and its install_workflows.sh helper were removed: the workflows have been installed since `2bf5f1d`, and running that installer would have copied the stale staged copies back over the live ones.)

> **DST note:** GitHub cron is UTC-only. `0 11` = 6:00 AM CDT (summer). When daylight saving ends in November, change to `0 12` to stay at 6:00 AM CST.

### Telemetry — what the crons publish about themselves

Both scheduled runs end by publishing two files to `main` for LIFESTYLE ("THE FLOOR"):

| File | Contents |
|---|---|
| `status/seller_stats.json` | Today's and yesterday's counters: `parcels_scanned`, `preforeclosure_notices`, `preforeclosure_matches`, `leads_scored`, `leads_qualified`, `skip_traces`, `leads_pushed_fub` |
| `status/seller_log.json` | The 100 most recent stage-level events (`scanned`, `matched`, `scored`, `traced`, `pushed`, `failed`) |

Everything is derived from the `runs` table — the JSON blob `record_run()` already writes at the end of every run — through a **read-only** (`mode=ro`) connection, so telemetry cannot touch the state the next run resumes from.

**A missing key means "we don't know", never zero.** Every stage in the runners stores `{"error": ...}` instead of its counts when it fails, and a stage that errored publishes *no* counter rather than a `0` — a blocked parcel mirror must not read as "scanned 0 parcels". A day whose stages did not all report lists them in `incomplete_stages`, and the totals beside it are a floor, not a total. A day with no runs at all publishes `{}`, because "the cron did not fire" and "the cron found nothing" are different facts.

Events are **stage-level and carry no PII** — county, stage and counts only. No owner names, addresses or phone numbers reach `status/`, for the same reason migration v5 purged stored consumer profiles.

> **Ordering is load-bearing.** The `Publish telemetry` step must stay *ahead* of `Push state DB`: that step checks out the orphan `state` branch and runs `git rm -rf .`, which takes the decrypted DB with it. The publish action also never writes under the checkout — it generates into a temp dir and commits with git plumbing — because a modified tracked file stops `git checkout state` from switching branches, and the state DB then silently stops being persisted. That exact failure cost the sibling repo sixteen hours of lost state on 2026-08-11.

### The auto-push flow, step by step

1. Monday morning: the weekly run pulls data, scores, and skip-traces qualified leads (≥ 40).
2. **Auto-push runs at the end of the same run.** For every traced lead:
   - **No email AND no phone from BatchData → HELD** (`held_no_contact`) — uncontactable records never enter FUB.
   - **DNC or litigator flag → extra `DNC` tag** so the nurture system suppresses texting/calling.
   - Otherwise the lead is pushed, tagged `Seller Lead` + `County-Absentee` / `Divorce-Filing` / `Pre-Foreclosure`, with property address, score, signal breakdown, and a short Claude-written motivation summary in a note.
3. The **pending-leads artifact** on the run is the permanent record: every row shows `status` (`pushed` / `held_no_contact` / `awaiting_approval`) and the FUB person id for pushed leads. The digest email carries the same stats.
4. Push failures stay `awaiting_approval` and retry automatically next Monday — or immediately via the **Push Approved Leads** fallback workflow. Held leads are re-traced (cache-safe, no double billing) if they still lack a skip trace, and re-checked for contact info on every fallback push.

Duplicate protection: before creating anyone, the pusher searches FUB by email, then phone, then property address; existing contacts just get the new tags and note (never a duplicate record). Creation also uses FUB's `deduplicate=true` flag as a second net.

---

## Cost Controls (skip tracing)

Skip tracing is the only per-unit cost in the system, so it is triple-guarded:

1. **Threshold gate** — only leads scoring ≥ 40 are ever traced (absentee alone doesn't qualify).
2. **Owner-level cache** — results are stored by normalized owner name + mailing ZIP in `skip_traces`. The same owner is **never billed twice**, across runs, properties, or counties. Within a single run, multi-property owners are traced once and the result attached to all their leads.
3. **Per-run budget by mode** — `skip_trace_budget: {weekly: 75, daily: 15}` in settings.yaml. Worst case = 75 + 5×15 = 150 traces/week. Over a calendar month that is at most 5 weekly runs + 22 daily runs = **705 traces ≈ $105.75/month** at the configured `skip_trace_cost_usd` ($0.15); in practice much lower because the owner cache never re-bills. Leads that miss the budget stay `qualified` and are picked up next run (highest scores first). `MAX_SKIP_TRACES_PER_RUN` env overrides both.
4. **Spend visibility** — every run's diagnostics table shows *traces this run × cost* and the month-to-date total (counted from the `skip_traces` cache timestamps); the Monday digest includes "Skip-trace spend this month: ~$X".

BatchData filters TCPA-blacklisted numbers by default, and we store the `dnc` and `litigator` flags on every trace — flagged leads are pushed with a `DNC` tag (so nurture suppresses calls/texts) and the flags remain visible in the push-record CSV.

### Error vs no-match discipline

The provider distinguishes **API errors** from **genuine no-matches** — they are handled completely differently:

| Outcome | Cached? | Lead status | Next run |
|---|---|---|---|
| **Matched** (contact info returned) | Forever — already paid for | `traced` → pushed/held | Uses cache |
| **No-match** (API succeeded, no data found) | Yes, but expires after `no_match_retrace_days` (90) | `held_no_contact` | Re-traced after expiry as provider data improves |
| **API error** (401/402/403/422/429/5xx, network) | **Never** | stays `qualified` | Retried automatically |
| **HTTP 200 with a failure in the body** (`status.code >= 400`, or `errorCount` covering the whole chunk) | **Never** | stays `qualified` | Retried automatically |
| **Address-alignment failure** (provider reports `matchCount` higher than the results we can map back to our requests) | **Never** | stays `qualified` | Retried automatically |

Every request logs the raw HTTP status and response body to the Actions log (the API key is never logged), and 429/5xx responses are retried with exponential backoff. The run-summary diagnostics table shows the matched / no-match / error breakdown with the top error message, so a failing token is visible at a glance instead of masquerading as "0% match rate."

**Why the last two rows exist.** This repo has shipped the "a failed lookup got recorded as a successful negative" bug twice, so the rule is now enforced at every network boundary rather than trusted per call site:

- *Body-level errors.* BatchData can return HTTP 200 while reporting the real status inside the body. Without the check, the chunk fell through to the no-match path and was cached — the same shape as the original 403-became-64-no-matches incident, one layer down.
- *Address alignment.* Provider results are mapped back to our requests by normalized street + ZIP. When BatchData normalizes differently (`123 Main Street` vs our `123 MAIN ST`) the lookup misses, and a **paid** match was written out as a genuine no-match: money spent, contact info discarded, and the no-match cache then blocked a re-trace for 90 days. `meta.matchCount` is the ground truth we check against.
- *The same rule applies to the free feeds.* ArcGIS Server (Bexar foreclosures **and** Bexar exemptions) does not use HTTP status codes for query failures — a bad field, a rebuilt layer, or a server exception all come back as HTTP 200 with `{"error": {...}}`. `raise_for_status()` passes and `data["features"]` is absent, so a broken feed read as "no foreclosure notices this month" and a broken exemption layer read as "nobody has a homestead" — the latter feeding straight into homestead-removed detection, which is worth +10, exactly enough to lift an absentee lead over the 40 trace threshold. Every ArcGIS call now goes through `arcgis.query()`, which raises on body-level errors; a genuinely empty page still means zero.

> **BatchData 403 Forbidden?** Per BatchData's own troubleshooting guide, this means the API token lacks the **Property Skip Trace** endpoint permission or the PayGo wallet is empty. Fix: BatchData dashboard → **API Tokens** (key icon) → your token → **View/Update** → check the *Property Skip Trace* permission, and confirm your wallet has balance. Erred leads re-trace automatically on the next run — no manual cleanup needed.

To swap providers later, implement `SkipTraceProvider.trace_batch()` in a new module and register it in `skiptrace/__init__.py`.

---

## Setup

### Required secrets (the system runs fully with just these three)

Add at **Settings → Secrets and variables → Actions**:

| Secret | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude for divorce name-matching and FUB note summaries |
| `FUB_API_KEY` | Follow Up Boss API (same account as LDR-Automation-Clean) |
| `SQLITE_ENCRYPTION_KEY` | Passphrase encrypting the state DB on the `state` branch (e.g. `openssl rand -hex 32`). **Never lose it** — the state DB is unrecoverable without it |

Everything else is **optional** and skipped gracefully when absent — no run will ever fail because an optional secret is missing (verified by `scripts/minimal_secrets_test.py`).

### Upgrade path — what each optional secret unlocks

| Secret(s) | What it unlocks | Without it |
|---|---|---|
| `BATCHDATA_API_KEY` | Skip tracing: owner phone numbers + emails on every qualified lead, with DNC/litigator flags, cached so no owner is billed twice. Sign up at [batchdata.io](https://batchdata.io) (pay-as-you-go, ~$0.07–0.12/match, budget-capped per run: 75 weekly / 15 daily) | Qualified leads still appear in the review CSV with full scores and signals, but with no contact info they are **held, not pushed to FUB** (uncontactable records are never pushed). They are retraced and pushed automatically once the key is added |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_FROM` | Monday digest email to peter@lifestyledesignrealty.com (new leads, pushed/held counts, score breakdown). Reuse the exact same values from LDR-Automation-Clean's secrets (smtp.gmail.com / 587 / Gmail app password) | Same stats appear in the Actions run **job summary**; the lead list is in the `pending-leads` artifact |
| `HEALTHCHECK_URL` | Dead-man's switch: healthchecks.io emails you if a weekly run silently stops. You already have an account (LDR-Automation-Clean uses it) — add a check with period = 1 week, grace = 2 days, copy its `https://hc-ping.com/…` URL | GitHub still emails you on workflow *failures*; you just won't be alerted if the schedule itself silently stops firing |

### settings.yaml tunables (non-secret)

Every key in `config/settings.yaml`, including the ones added by the volume upgrade:

| Key | Default | What it does |
|---|---|---|
| `counties` | bexar, comal, travis | Counties processed each run |
| `scoring.*` | see file | Signal point values |
| `scoring.skip_trace_threshold` | 40 | Minimum score to spend money on a trace |
| `scoring.warm_tier_min` | 30 | Floor of the warm tier (stored, never traced/pushed) |
| `skip_trace_budget` | weekly 75 / daily 15 | Max NEW owners traced per run, by `RUN_MODE` |
| `skip_trace_cost_usd` | 0.15 | Cost estimate used for spend reporting only |
| `no_match_retrace_days` | 90 | How long a cached NO-MATCH blocks a re-trace |
| `event_signal_retention_days` | 120 | How long a preforeclosure/divorce signal is carried forward after it was last observed |
| `exemption_min_snapshot_ratio` | 0.5 | Skip homestead-removed detection if an exemption pull returns less than this fraction of the previous snapshot (truncation guard) |
| `min_market_value` / `max_market_value` | 40000 / 2500000 | Parcel value band considered |
| `parcel_sources` / `exemption_sources` / `foreclosure_sources` | see file | Per-county feed configuration |

Non-secret environment overrides used by the workflows: `RUN_MODE` (`daily`\|`weekly`), `DRY_RUN`, `MAX_SKIP_TRACES_PER_RUN` (overrides `skip_trace_budget`), `LLM_MODEL`, `DATABASE_PATH`, `DATA_DIR`, `SETTINGS_PATH`, `REVIEW_DIR`.



Adding any of these later requires **zero code changes** — add the secret and the next run picks it up.

### First run

Trigger **Weekly Data Pull** manually with `dry_run: true` once to verify data sources, then run it for real. The first run downloads both county parcel files (~10 min) and establishes the ownership/exemption baseline — homestead-removed and owner-change signals begin firing from the *second* run onward.

---

## Adding a New County (Dallas, Tarrant, Harris are pre-scaffolded)

1. Add the county name to `counties` in `config/settings.yaml` (for the scaffolded three, the `parcel_sources` block already exists — just remove it from the scaffold comment area / it is picked up automatically once listed in `counties`).
2. For a brand-new county, add a `parcel_sources` block with its TxGIO `area_type_name` and FIPS. TxGIO covers all Texas counties, so parcels + absentee detection + warm tier work everywhere with zero code changes.
3. Upload the county's parcel zip to the mirror: uncomment/add the county in `COUNTIES` inside `scripts/refresh_parcel_mirror.sh` and run it **from a home/office network** — it downloads from TxGIO and uploads `{county}_parcels.zip` to the `parcel-data-2025` release.
4. Foreclosures: add a `foreclosure_sources` entry if the county has a live feed (see the per-county table above), or rely on the CSV inbox.
5. Scoring, tracing, staging, and FUB push need no changes.

---

## Compliance

- **Public records only.** Every automated source is a government-published dataset or service: TxGIO StratMap (CC0), Bexar County ArcGIS services, and County Clerk foreclosure postings (Texas Property Code §51.002 requires public posting). No Zillow, Realtor.com, or other ToS-violating scraping — the county-first approach exists precisely to avoid that.
- **No automated outreach from this repo.** It writes to FUB (tags + notes) and emails *you* a digest. Contacting leads happens in LDR-Automation-Clean under its CAN-SPAM/unsubscribe rules.
- **Automated push, reviewable after the fact.** Qualified, contactable leads are **auto-pushed to FUB at the end of every run** — there is no approval gate in front of it (that gate was removed in commit `fed9e8f`). What you get instead is a complete record: the `pending-leads` artifact lists every lead pushed, held, or awaiting retry, and the Monday digest carries the same counts. Use the **Push Approved Leads** workflow's `exclude_ids` input to suppress specific leads on a retry. If you want a true human gate, unset `FUB_API_KEY` on the cron workflows — leads then accumulate in `awaiting_approval` and only the manual workflow pushes them.
- **TCPA awareness.** DNC and litigator flags from skip tracing are stored and surfaced in the review CSV. If you cold-call, scrub against the federal DNC registry; TCPA-restricted numbers are excluded by the provider by default.
- **Privacy / data minimisation.** Owner data stays inside the AES-256 encrypted state DB on the `state` branch. The review CSV — which does contain owner names, property addresses, emails and phones — lives only in Actions artifacts, retained **30 days (weekly) / 14 days (daily)**, then deleted by GitHub. The skip-trace cache stores only what the pipeline uses (emails, phones, DNC/litigator flags) plus a non-identifying provenance stub; the provider's full consumer profile (relatives, address history, DOB) is **never stored** — see migration v5. Secrets are never logged: failures log HTTP status and response body, never the key.

---

## Testing

```bash
pip install -r requirements.txt pytest
python3 -m pytest tests/ -v          # 147 unit tests (scoring, warm tier, budgets, dedupe,
                                     # idempotency, migrations, feed/API error discipline,
                                     # PII minimisation, entry-point imports, telemetry
                                     # honesty/atomicity)
DRY_RUN=true python3 scripts/e2e_live_test.py   # live smoke test, zero spend
```

Verified at build time: Comal TxGIO sync (82,353 individual-owner parcels kept, 30,447 absentee), live Bexar foreclosure fetch (457 notices), scoring, dry-run tracing, staging, and review artifacts.

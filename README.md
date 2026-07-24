# LDR-Seller-Finder

Seller lead generation system for Lifestyle Design Realty. Every week it pulls public property and court data for the San Antonio metro (Bexar and Comal counties), identifies homeowners who are statistically likely to sell, scores them, skip-traces the qualified ones for contact information, and stages them for **your manual approval** before anything is pushed to Follow Up Boss.

**This repo finds and stages leads. It never contacts them.** All outreach (emails, texts, campaigns) stays in [LDR-Automation-Clean](https://github.com/PropertyPete1/LDR-Automation-Clean), which handles CAN-SPAM compliance, unsubscribe handling, and send-time rules.

---

## How It Works

```
                 ┌────────────────────────────────────────────────┐
                 │        Weekly Data Pull  (Mon 6:00 AM CT)      │
                 └────────────────────────────────────────────────┘
  TxGIO parcels (Bexar+Comal) ─┐
  Bexar exemptions (ArcGIS)  ──┤
  Bexar pre-foreclosures     ──┼──► Scoring (0–100) ──► score ≥ 40?
  Divorce filings (stubbed)  ──┘                           │
                                                           ▼
                                              Skip trace (BatchData, cached,
                                              budget-capped, never re-billed)
                                                           │
                                                           ▼
                                          Staged as "awaiting_approval"
                                          + review CSV artifact + digest email
                                                           │
                                     ═════ YOU REVIEW & APPROVE (manual) ═════
                                                           │
                                                           ▼
                 ┌────────────────────────────────────────────────┐
                 │   Push Approved Leads (manual trigger only)    │
                 │   → Follow Up Boss with tags + notes, deduped  │
                 └────────────────────────────────────────────────┘
```

State (parcels, leads, skip-trace cache, run history) lives in a SQLite database that is AES-256 encrypted and synced to the orphan `state` branch — the exact same pattern used by LDR-Automation-Clean.

---

## Scoring Model

| Signal | Points | Status | How it's detected |
|---|---|---|---|
| Absentee owner | +30 | ✅ Live | Owner mailing address ≠ property address (normalized street + ZIP comparison) |
| Pre-foreclosure notice | +30 | ✅ Live (Bexar) | Notice of Trustee Sale from the County Clerk's public feed, matched to the parcel by address |
| Divorce filing match | +25 | ⚠️ Stubbed (see below) | Party name fuzzy-matched to parcel owner using Claude (`claude-sonnet-4-6`) |
| Owned 10+ years | +20 | ⚠️ Wired — auto-ingests from `data/inbox/` (see below) | Deed date from the free BCAD export (one open-records email), or 10 years of unchanged ownership in our own history |
| Homestead exemption removed | +10 | ✅ Live (Bexar) | `HS` code present in the previous pull, missing in the current one |

Leads scoring **40 or higher** are skip-traced and staged for approval. Weights and the threshold are tunable in `config/settings.yaml` — no code changes needed.

Score examples: absentee alone = 30 (not traced). Absentee + pre-foreclosure = 60 (traced). Absentee + divorce = 55 (traced). Absentee + homestead removed = 40 (traced).

---

## Data Sources — What's Real, What's Stubbed, and Why

This is the honest state of public data access as of July 2026. Every claim below was verified by actually hitting the endpoints during development.

### 1. Parcel + owner data (Bexar & Comal) — ✅ CLEAN, FREE, BULK

Neither BCAD (bcad.org) nor Comal AD (comalad.org) offers a self-serve bulk download on their website. Instead we use the **TxGIO StratMap Land Parcels program** (Texas Geographic Information Office / TWDB), which collects every CAD's certified roll, normalizes it, and republishes per-county FileGDB downloads under a **CC0 license**:

- API: `https://api.tnris.org/api/v1/collections?search=land+parcels` → newest "Land Parcels" collection → `/resources?collection_id=…&area_type_name=Bexar` returns the zip URL.
- Verified: Comal file = 103,537 parcels (tax year 2025) with `OWNER_NAME`, `SITUS_*`, `MAIL_*`, `MKT_VALUE`. Bexar ≈ 710K parcels.
- Gotcha handled in code: TxGIO's CloudFront blocks default `python-requests`/`curl` user agents; the loader sends a browser-like UA.
- Refresh cadence is roughly annual. That's fine: parcels change slowly, and the signals that make leads *timely* (foreclosure, divorce, exemption changes) come from feeds that update monthly/weekly.

If you ever want CAD-direct data (fresher, includes deed dates): email **openrecords@bcad.org** — BCAD provides its full appraisal export via FTP for free (current year). Comal AD takes open-records requests at their office/email; no standing export exists.

### 2. Homestead exemptions — ✅ LIVE for Bexar, ❌ not available free for Comal

Bexar County's public parcel layer exposes an `Exempts` field (verified: 405,824 parcels carry `HS`):

- `https://maps.bexar.org/arcgis/rest/services/Parcels/MapServer/0` — paged at 1,000 records, joined to TxGIO parcels on `PropID`.

The module compares each pull to the previous one and emits **homestead-removed** events. Comal has no free exemption feed; the module skips counties without an `exemption_sources` entry and logs it.

### 3. Pre-foreclosure notices — ✅ LIVE for Bexar

The Bexar County Clerk publishes the current month's Notice of Trustee Sale filings through the public ArcGIS service that powers the county's [Foreclosure Map](https://maps.bexar.org/foreclosures/):

- Mortgage: `https://maps.bexar.org/arcgis/rest/services/CC/ForeclosuresProd/MapServer/0`
- Tax: `.../MapServer/1`
- Fields: `ADDRESS, DOC_NUMBER, YEAR, MONTH, TYPE, CITY, ZIP` (verified: 457 notices for the August 2026 sale).

The feed has no owner name, so notices are matched to parcels by normalized address + ZIP to attach the owner. Comal posts notices physically at the courthouse only — no digital feed exists, so pre-foreclosure is Bexar-only for now.

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
├── run_weekly_pull.py            # Weekly cron entry point (data → score → trace → stage)
├── run_push_approved.py          # Manual approval-gate entry point (push to FUB)
├── config/settings.yaml          # Scoring weights, county sources, budgets — edit freely
├── src/seller_finder/
│   ├── config.py                 # Env secrets + settings loader
│   ├── state.py                  # SQLite schema (parcels, leads, skip_traces, runs…)
│   ├── scoring.py                # 0–100 scoring engine
│   ├── fub.py                    # Follow Up Boss push: dedupe, tags, notes
│   ├── review.py                 # Review CSV, run summary, weekly digest email
│   ├── health.py                 # healthchecks.io dead-man's switch
│   ├── sources/
│   │   ├── parcels.py            # TxGIO bulk parcel loader (Bexar, Comal)
│   │   ├── exemptions.py         # Bexar homestead exemptions + removal detection
│   │   ├── preforeclosure.py     # Bexar Notice of Trustee Sale feed
│   │   ├── deeds.py              # Deed-date inbox auto-ingest (tenure signal)
│   │   └── divorce.py            # STUB + CSV inbox + Claude fuzzy matching (live)
│   └── skiptrace/
│       ├── base.py               # Provider interface (SkipTraceProvider)
│       ├── batchdata.py          # BatchData implementation
│       └── tracer.py             # Cache-aware, budget-capped orchestration
├── scripts/
│   ├── import_deed_dates.py      # One-off deed-date import (or use data/inbox/)
│   ├── e2e_live_test.py          # Live smoke test (dry-run, no spend)
│   └── live_bexar_test.py        # Full live Bexar E2E (real parcels + foreclosures)
├── tests/test_pipeline.py        # 23 unit tests
└── .github/
    ├── workflows/weekly-pull.yml     # Mon 6 AM CT cron
    ├── workflows/push-approved.yml   # Manual trigger only
    └── actions/state-sync/action.yml # Encrypted SQLite ⇄ `state` branch
```

---

## Workflows

| Workflow | Trigger | What it does |
|---|---|---|
| **Weekly Data Pull** | Cron `0 11 * * 1` (Mon 6:00 AM CDT) + manual | Full pipeline through staging. Uploads `pending-leads` artifact (review CSV + summary), writes the job summary, emails the digest, pings healthchecks.io |
| **Push Approved Leads** | `workflow_dispatch` **only** | Pushes everything in `awaiting_approval` to FUB. Optional `exclude_ids` input skips specific leads. Supports `dry_run` |

> **DST note:** GitHub cron is UTC-only. `0 11` = 6:00 AM CDT (summer). When daylight saving ends in November, change to `0 12` to stay at 6:00 AM CST.

### The approval gate, step by step

1. Monday morning: weekly run finishes; you get the digest email (new leads found, score breakdown, count awaiting approval).
2. Open the run in GitHub Actions → download the **pending-leads** artifact → review `pending_leads.csv` (owner, property, score, signals, traced phones/emails, DNC/litigator flags).
3. Happy with the list? Actions → **Push Approved Leads** → Run workflow. To drop specific rows first, paste their `lead_id`s into the `exclude_ids` box (they're marked `skipped`).
4. Leads land in FUB tagged `Seller Lead` + `County-Absentee` / `Divorce-Filing` / `Pre-Foreclosure`, with the property address, score, and signal breakdown in a note, plus a short Claude-written motivation summary.

Duplicate protection: before creating anyone, the pusher searches FUB by email, then phone, then property address; existing contacts just get the new tags and note (never a duplicate record). Creation also uses FUB's `deduplicate=true` flag as a second net.

---

## Cost Controls (skip tracing)

Skip tracing is the only per-unit cost in the system, so it is triple-guarded:

1. **Threshold gate** — only leads scoring ≥ 40 are ever traced (absentee alone doesn't qualify).
2. **Owner-level cache** — results are stored by normalized owner name + mailing ZIP in `skip_traces`. The same owner is **never billed twice**, across runs, properties, or counties. Within a single run, multi-property owners are traced once and the result attached to all their leads.
3. **Per-run budget** — `max_skip_traces_per_run: 200` in settings.yaml caps new traces per week (BatchData bills ~$0.07–0.12 per match, so worst case ≈ $14–24/week). Leads that miss the budget stay `qualified` and are picked up next run.

BatchData filters TCPA-blacklisted numbers by default, and we store the `dnc` and `litigator` flags on every trace so they're visible in the review CSV before you approve.

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
| `BATCHDATA_API_KEY` | Skip tracing: owner phone numbers + emails on every qualified lead, with DNC/litigator flags, cached so no owner is billed twice. Sign up at [batchdata.io](https://batchdata.io) (pay-as-you-go, ~$0.07–0.12/match, budget-capped at 200/run) | Qualified leads still flow to the review CSV and FUB with full scores and signals — just no phone/email contact info |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_FROM` | Monday digest email to peter@lifestyledesignrealty.com (new leads, score breakdown, awaiting-approval count). Reuse the exact same values from LDR-Automation-Clean's secrets (smtp.gmail.com / 587 / Gmail app password) | Same stats appear in the Actions run **job summary**; the lead list is in the `pending-leads` artifact |
| `HEALTHCHECK_URL` | Dead-man's switch: healthchecks.io emails you if a weekly run silently stops. You already have an account (LDR-Automation-Clean uses it) — add a check with period = 1 week, grace = 2 days, copy its `https://hc-ping.com/…` URL | GitHub still emails you on workflow *failures*; you just won't be alerted if the schedule itself silently stops firing |

Adding any of these later requires **zero code changes** — add the secret and the next run picks it up.

### First run

Trigger **Weekly Data Pull** manually with `dry_run: true` once to verify data sources, then run it for real. The first run downloads both county parcel files (~10 min) and establishes the ownership/exemption baseline — homestead-removed and owner-change signals begin firing from the *second* run onward.

---

## Adding a New County (Travis, DFW, Houston…)

1. Add the county name to `counties` in `config/settings.yaml`.
2. Add a `parcel_sources` block with its TxGIO `area_type_name` (Travis, Dallas, Tarrant, Harris — FIPS 48453, 48113, 48439, 48201). TxGIO covers all Texas counties, so parcels + absentee detection work everywhere with zero code changes.
3. Optional: if the county publishes exemption or foreclosure ArcGIS/REST feeds, add `exemption_sources` / `foreclosure_sources` entries (find them the way we found Bexar's — check the county clerk's GIS/foreclosure map page and inspect its network calls).
4. Scoring, tracing, staging, and FUB push need no changes.

---

## Compliance

- **Public records only.** Every automated source is a government-published dataset or service: TxGIO StratMap (CC0), Bexar County ArcGIS services, and County Clerk foreclosure postings (Texas Property Code §51.002 requires public posting). No Zillow, Realtor.com, or other ToS-violating scraping — the county-first approach exists precisely to avoid that.
- **No automated outreach from this repo.** It writes to FUB (tags + notes) and emails *you* a digest. Contacting leads happens in LDR-Automation-Clean under its CAN-SPAM/unsubscribe rules.
- **Human in the loop.** Nothing reaches FUB without you triggering the push workflow after reviewing the CSV.
- **TCPA awareness.** DNC and litigator flags from skip tracing are stored and surfaced in the review CSV. If you cold-call, scrub against the federal DNC registry; TCPA-restricted numbers are excluded by the provider by default.
- **Privacy.** Owner data stays inside the encrypted state DB on the private `state` branch; the review CSV lives only in 30-day Actions artifacts.

---

## Testing

```bash
pip install -r requirements.txt pytest
python3 -m pytest tests/ -v          # 16 unit tests (scoring, dedupe, matching, staging)
DRY_RUN=true python3 scripts/e2e_live_test.py   # live smoke test, zero spend
```

Verified at build time: Comal TxGIO sync (82,353 individual-owner parcels kept, 30,447 absentee), live Bexar foreclosure fetch (457 notices), scoring, dry-run tracing, staging, and review artifacts.

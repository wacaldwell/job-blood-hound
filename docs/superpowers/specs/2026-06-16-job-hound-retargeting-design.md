# job-hound retargeting — design

Date: 2026-06-16
Owner: Jordan Rivers
Status: approved for planning

## Problem

job-hound currently scans ~17 elite tech firms (Anthropic, OpenAI, Databricks,
Datadog, MongoDB, etc.) plus a handful of manual gov employers. For a 54-year-old
senior cloud/SRE/platform engineer who was just laid off and wants realistic
odds, this list is the wrong target: those companies are the most competitive in
the market.

A baseline dry-run on 2026-06-16 confirmed the problems quantitatively:

- **Volume is low and concentrated.** 32 total matches, nearly all from just two
  companies (MongoDB, Bosch). Most elite slugs returned nothing or 404'd.
- **~half the matches are junk.** Examples that passed filters: "Senior Director,
  HR Business Partnering", "Equity and Executive Compensation Director",
  "Director - Logistics & Supply Chain", "Investment Director", "Director - Real
  Estate Portfolio Americas".
- **Location filter leaks internationally.** Bosch roles in Egypt, Thailand,
  Brazil, Germany, and Mexico all passed, because the `"us"` location token
  substring-matches words like "Sunnyvale" and country-code noise.

## Goals

1. Re-target discovery away from elite firms toward employers where the operator has real
   odds, across four categories.
2. Raise filter precision so a larger company list does not drown the results in
   noise.
3. Fix the location filter so only remote-eligible or within-3h-drive roles pass.
4. Keep results readable: show which category and remote/onsite type each hit is.

## Non-goals (deferred)

- Fit-scoring / ranking layer (seniority, salary, recency weighting). This is the
  planned phase-2 follow-up once volume justifies ranking.
- Programmatic ATS slug auto-discovery tooling.
- DB schema changes. `category` and display-name surfacing in the CLI `list`/
  `show` commands are deferred to the scoring phase. This phase enriches only the
  scanner's own reporting (dry-run print, markdown report, Discord).
- Any change to the "discovery and prep only, never auto-apply" and "public
  endpoints only" hard rules. Research is used only to seed the company list; the
  pipeline still queries public ATS APIs directly.

## Constraints (from owner)

- Targeting mix: mid-market scale-ups, regional (within ~3h drive), gov/defense,
  and "boring" enterprise. **Elite firms dropped entirely** (not worth scanner
  time without a warm intro).
- Clearance: clearable / would pursue. Public-trust and "clearable" gov roles are
  in scope; clearance-required roles are kept but treated as stretch. Clearance
  cannot be detected from a job title (it lives in the JD), so the scanner does
  not attempt to filter on it; the human judges at draft time. This is stated
  honestly, not papered over.
- On-site: hybrid OK within ~3h drive of Beaverton/Portland NC.
- Salary floor: 150-180k (informational; not a scanner filter this phase).

## Design

### 1. Restructured `companies.yaml`

Replace the flat elite list with a categorized, **slug-verified** list. Each
company entry gains a `category` field:

- `mid_market` — Series B-D / profitable tech on Greenhouse/Lever/Ashby with real
  SRE/cloud/platform needs, remote-friendly, beatable.
- `regional` — employers HQ'd within ~3h drive that use a scannable ATS.
- `gov_defense` — contractors; clearable/public-trust in mind, clearance-required
  kept as stretch.
- `enterprise` — stable "boring" shops (insurance, healthcare IT, fintech
  back-office) running real AWS/Terraform.

Entry schema (additive; existing `name`/`ats`/`slug`/`careers_url` unchanged):

```yaml
- {name: "Example Co", ats: greenhouse, slug: exampleco, category: mid_market}
```

Honest caveat recorded in the file: many prime regional/enterprise employers run
Workday or iCIMS (no public feed) and therefore land in the manual bucket;
scannable wins skew mid-market tech and gov contractors.

### 2. Filter overhaul

**title_terms** — keep senior IC terms; replace dangerous bare leadership tokens
(`director`, `head of`) with scoped forms.

- IC: `site reliability`, `SRE`, `production engineer`, `platform engineer`,
  `platform engineering`, `cloud engineer`, `cloud architect`, `cloud
  infrastructure`, `cloud operations`, `infrastructure engineer`, `reliability
  engineer`, `devops engineer`, `devops`, `devsecops`, `ml platform`, `machine
  learning platform`, `ai infrastructure`, `ai platform`, `solutions architect`.
- Leadership (scoped): `director of infrastructure`, `director of platform`,
  `director of engineering`, `director of site reliability`, `director of cloud`,
  `infrastructure director`, `platform director`, `engineering director`, `head
  of infrastructure`, `head of platform`, `head of engineering`, `head of sre`,
  `head of reliability`, `head of cloud`, `VP engineering`, `VP of engineering`,
  `VP infrastructure`, `VP of infrastructure`, `vice president, engineering`.
- `engineering manager` is kept (the operator wants management roles) but flagged as the
  noisiest term to watch and tune against real output.

**exclude_terms** (owner-approved, applied with word boundaries):

`intern`, `junior`, `jr`, `new grad`, `entry level`, `entry-level`, `apprentice`,
`sales`, `account executive`, `sales engineer`, `recruiter`, `legal`, `attorney`,
`counsel`, `paralegal`, `tax`, `communications`, `public relations`, `PR`,
`procurement`, `product manager`, `program manager`, `clinical`, `nurse`,
`nursing`, `accountant`, `accounting`, `controller`, `human resources`,
`HR business`, `talent`, `compensation`, `real estate`, `logistics`, `supply
chain`, `investment`, `marketing`.

Exact lists are tuned against the first real scan; the spec records the starting
point and the principle (scope leadership terms, exclude the recurring junk
families seen in the baseline).

**location matching** — fix the substring leak and encode geography.

- Match location tokens with **word boundaries** (`\bnc\b`, not `nc` inside
  "Sunnyvale"). Same boundary treatment for exclude tokens (`\bPR\b`,
  `\bsales\b` not matching "wholesale").
- Token set = remote terms ∪ within-3h cities ∪ `united states`:
  - Remote: `remote`, `anywhere`, `distributed`, `united states`.
  - Within ~3h drive: `portland`, `beaverton`, `charlotte`, `greenville`,
    `spartanburg`, `knoxville`, `columbia`, `winston-salem`, `greensboro`,
    `atlanta`, `chattanooga`, `johnson city`, `kingsport`, `bristol`, `hickory`,
    `gastonia`.
- Effect: a role passes location if it is remote-eligible (remote / anywhere /
  united states) OR physically within ~3h. On-site roles elsewhere (e.g. a
  San-Francisco-only or Bangkok role) are dropped. `united states` is a documented
  heuristic for remote-US listings; accepted with eyes open.

### 3. Code changes (minimal, import-safe)

In `job_monitor.py`:

- `compile_patterns(terms, boundary=False)` — when `boundary=True`, wrap each
  escaped term in `\b...\b`. Location and exclude patterns compile with
  `boundary=True`; title patterns stay substring.
- In `run_scan`, after computing `hits`, enrich each match with additive fields
  only: `company_display` (the config `name`), `category` (the config `category`,
  default `""`), and `location_type` (`"remote"` if any remote token appears in
  the location, else `"onsite/hybrid"`). **`j['company']` stays the slug** — it is
  load-bearing for uid generation, slug generation, and the Greenhouse
  date-upgrade call in `job_cli.py`.
- Reporting paths (`write_report`, `notify_discord`, the dry-run print in `main`)
  use `company_display`, and show `category` and `location_type`.

`run_scan` and `generate` remain side-effect-free and import-safe. No new
dependencies. `job_cli.py` and `jobdb.py` are untouched (the new fields are extra
keys they simply ignore).

### 4. Build-and-verify method for the company list

For each category, per ATS:

1. Research real companies known to use Greenhouse/Lever/Ashby/SmartRecruiters,
   prioritizing remote + 3h-drive HQ + age-friendly + clearable-gov.
2. Write `name`/`ats`/`slug`/`category` entries.
3. Run a **verification dry-run** (`python job_monitor.py --dry-run`).
4. Drop or correct every slug that 404/403s or returns zero plausible roles.
5. Keep only slugs that resolve and return data. Companies with no public feed are
   recorded with `ats: workday|icims` + `careers_url` for the manual bucket.

### 5. Verification

- **Mocked filter tests** (per CLAUDE.md "test parsing/filter/state changes with
  mocked data before claiming they work"): feed a fixture of known-good titles
  (senior SRE/platform/cloud, scoped leadership) and known-junk titles (the
  baseline's HR/comp/logistics/foreign-location cases) through `matches()` and
  assert keep/drop. Must catch the `"us"`-substring and foreign-location
  regressions specifically.
- **Live verification scan** to confirm slugs resolve (step 4 above).
- **Baseline-vs-new delta**: report total matches, junk rate, and international
  leakage before vs after. Success = materially more relevant matches, junk rate
  visibly down, zero foreign-only roles passing.

## Success criteria

1. `companies.yaml` is categorized and every listed scannable slug resolves
   (verified by dry-run).
2. The baseline junk families (HR, compensation, logistics, real estate,
   investment) no longer pass filters.
3. No foreign-only roles pass the location filter.
4. Mocked filter tests pass.
5. A real scan returns a larger, more relevant set than the 32-match,
   ~50%-junk baseline.

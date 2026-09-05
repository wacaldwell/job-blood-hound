# Lead source and cold leads design

Date: 2026-08-28
Status: approved, not yet implemented

Two features, specified together because they change the same numbers on the
same page and their migrations touch the same rows. They can ship as separate
PRs (see **Sequencing**), and feature 2 is the larger of the two by a wide
margin.

Repos: `job-hound` (schema, write API, CLI) and `lead-inbox` (the lead inbox UI:
the Reply Window, the lead inbox, the job detail page).

## Why

**the operator, 2026-08-28:** "we should build in automatic staleness, once we get past
30 days I feel we're no longer waiting. Also, we need a field for how the lead
found me, did I reply or did someone on linkedin reach out to me, for example
strategic education was a recruiter that pinged me."

### 1. "Awaiting a reply" is telling a comfortable lie

The Reply Window puts 13 applications under **Awaiting a reply**. Eight of
them were submitted more than 30 days ago, and the slowest reply any employer
has ever sent arrived on day 31. Those eight are not waiting, they are over,
and counting them as live inflates the only number on the page that is
supposed to represent hope.

The page already half-knows this: it hatches the bars past the slowest
recorded reply and says "realistically finished" in the lede. But it still
files them under a bucket whose label says otherwise, and the bucket count is
what gets read.

### 2. The response rate is measuring two different things at once

An application the operator sent and a recruiter who found him are not the same event,
and averaging them answers no question.

Strategic Education is the live example. A recruiter pinged him; the row shows
`applied_at` 2026-08-25 and a first `interviewing` transition on 2026-08-26,
so the page reports it as a **1 day reply**. That is not an employer replying
fast to a strong resume, it is a conversation the recruiter had already
started. It is also the single data point that pulled the reported median from
8 days to 7 when the timezone fix landed.

The response rate is the page's headline claim about whether the outbound
strategy works. A recruiter ping is evidence about the operator's LinkedIn profile and
his network, not about whether his applications are landing, and it belongs in
a different denominator.

### 3. The concept already exists, in the wrong column

Four rows in the live database carry source information in `ats`, the column
that is supposed to name which applicant tracking system served the posting:

| uid prefix | company | `ats` | what it actually means |
|---|---|---|---|
| `recruiter:` | hibob | `recruiter` | a recruiter made contact |
| `linkedin:` | Nimbus | `linkedin` | the operator found it on LinkedIn |
| `linkedin:` | nscale | `linkedin` | the operator found it on LinkedIn |
| `linkedin:` | origami-risk | `linkedin` | the operator found it on LinkedIn |

Everything else is a real ATS (`greenhouse`, `ashby`, `paycom`, `avature`) or
`manual`. So `ats` currently conflates "which system served this" with "how
this reached me", and the one true recruiter ping in the pipeline, Strategic
Education, is filed as `manual` and looks like every other pasted URL.

Note the trap this table sets, and that the migration must not fall into:
**`linkedin` is a finding place, not a direction.** the operator browsing LinkedIn
jobs and applying is outbound. A recruiter messaging him on LinkedIn is
inbound. The same word covers both, and the existing column cannot tell them
apart.

## Decisions

Settled with the operator on 2026-08-28, before any design was written.

1. **Cold is a classification, not a state change.** Every read surface
   reclassifies at 30 days; nothing writes to the database, and no cron
   auto-closes anything. A reply on day 45 simply moves the row back.
2. **Outbound and inbound are separate funnels.** The response rate and the
   median reply time are computed over outbound applications only. Inbound is
   reported on its own line rather than hidden or blended.
3. **Source is set from the lead inbox UI, and defaulted at ingest.** Scanner
   and `fetch` leads default to outbound with no work from the operator; he only
   touches the exceptions. No new CLI verb.
4. **The mis-filed `ats` values get migrated**, and `ats` goes back to meaning
   one thing.

## Scope

In scope: a `source` column and its write path; a 30-day cold classification
shared by every read surface; the split-funnel statistics on the Reply Window;
a backfill of the 32 rows that have an `applied_at`.

Out of scope, deliberately:

- **Auto-closing cold leads.** Decision 1. `bin/jh sweep` was offered and not
  taken; the manual 2026-08-20 sweep stays manual.
- **A `source` taxonomy beyond two values.** See **Data model**.
- **Backfilling `source` on the ~500 rows with no `applied_at`.** A lead the operator
  never applied to has no source worth recording, and the scan default covers
  every future one.
- **Changing any `uid`.** See **Migration**.

---

## Feature 1: cold leads

### The rule

An application is **cold** when all three hold:

- it has an `applied_at`,
- no employer contact has ever been recorded (no `interviewing` transition and
  no rejection), and
- more than `COLD_AFTER_DAYS` calendar days have passed since `applied_at`.

`COLD_AFTER_DAYS = 30`. Days are Eastern calendar days, using the same
conversion `lib/reply-window.ts` already uses. Cold is evaluated at read time
from `applied_at` and the lifecycle; it is never stored.

A cold lead is still `applied` in the state machine. Nothing about the
transition table changes.

### Why 30 and not "past the slowest reply"

The Reply Window currently derives its cold line from the data: `pastWindow`
counts waits longer than the slowest reply ever received, which is 31 days.
That is elegant and it is also unstable, because a single unusually slow reply
would move the line for every other lead on the page.

30 is a fixed, explainable number that happens to sit one day under the
current empirical maximum, so the classification barely moves on today's data
(8 rows either way) while becoming stable against future outliers.

**Replace `pastWindow` with the cold rule rather than keeping both.** Two
nearly-identical notions of "probably over", differing by a day and drifting
apart as data arrives, is the kind of thing that makes a page untrustworthy.
The hatched bars in the "Still waiting" plot mean *cold* after this change,
and the lede's "already older than the slowest reply ever received" sentence
is rewritten to name the 30-day rule.

### Where the constant lives

This is the third cross-repo constant in this system, and the first two have a
documented drift hazard (`staleness.py` carries a long comment about
`STALE_AFTER_DAYS` and `COMMITTED_STATES` being duplicated in
`lead-inbox/lib/job-format.ts` and `lead-inbox/lib/job-sort.ts`, with no build step
between the repos and no test on either side able to catch a divergence).

Follow the pattern that exists rather than inventing a fourth:

- job-hound: `COLD_AFTER_DAYS = 30` in `staleness.py`, beside
  `STALE_AFTER_DAYS`, with the same "DUPLICATED, ON PURPOSE" comment naming
  its counterpart.
- lead-inbox: `COLD_AFTER_DAYS = 30` in `lib/reply-window.ts`, with the reciprocal
  comment.

**Do not reuse `STALE_AFTER_DAYS`.** It is 7 days, it measures time since
*the operator* acted, and it applies to `COMMITTED_STATES`, which excludes `applied`
entirely. Cold measures time since *the employer* was last heard from, on
exactly the state `STALE_AFTER_DAYS` ignores. They are different clocks over
different rows and they must not be collapsed.

### Surfaces

| Surface | Change |
|---|---|
| Reply Window buckets | New `cold` bucket between `waiting` and `rejected` in `BUCKET_ORDER`. Label "Gone cold", note "past 30 days, no contact". |
| Reply Window "Still waiting" plot | Hatching now means cold. Lede names the 30-day rule. Keeps listing cold rows; they are the point of the panel. |
| Reply Window tiles | Unchanged. |
| `bin/jh list` | Cold applications get the same marker treatment `staleness.py` already gives stale ones, riding the existing age line rather than adding one. |
| the lead inbox UI `/jobs` table | Cold marker beside the existing stale marker. |
| The Loop `/jobs/loop` | **No change.** Its holding tier already sorts longest-silence-first and colours a nudge; a second vocabulary for the same fact would fight it. |

### Colour

`cold` uses `var(--muted-mark)`, the same grey as `silent`. That is
deliberate: `silent` is a closed lead that never replied and `cold` is an open
one that almost certainly never will, so they are the same news at different
stages of admitting it. `waiting` keeps `--accent` blue and now means only
what it says.

---

## Feature 2: lead source

### Data model

One new column on `jobs`, through the existing additive migration:

```python
ADDED_COLUMNS = {
    ...
    "source": "TEXT",
}
```

Values, and only these two:

| Value | Meaning |
|---|---|
| `outbound` | the operator found the posting and applied. |
| `inbound` | Someone contacted the operator about the role first. |

`NULL` means unknown, and is treated as `outbound` by every reader. That is
the safe default in exactly the sense the Fit Gate uses the word: guessing
`outbound` understates the response rate, guessing `inbound` inflates it, and
this page exists to avoid flattering numbers.

**Two values, not a taxonomy.** "Recruiter on LinkedIn", "referral from a
friend", "ex-colleague" and "inbound email" are all the same fact for every
statistic on the page: the employer moved first. The channel is prose and
belongs in `notes`, which already exists, is 4000 characters, and is described
in `jobdb.py` as "the field you paste a recruiter email into". If a real
question later needs the channel as data, adding a `source_channel` column is
another additive migration and costs nothing to defer.

### Write path

`jobdb.py` gets `set_source(uid, source)`:

- validates against the two-value enum, raising on anything else,
- writes `source` and `updated_at`,
- appends a `state_log` row with a `source: <value>` note, in the same
  transaction, following `set_vote` and `set_read` rather than the unaudited
  generic `set_fields`.

`jobapi.py` gets `POST /jobs/{ident}/source`, a thin wrapper over that setter,
consistent with every other endpoint there. **No endpoint that submits, fills
or logs in**, and this one does not come close, but the hard rule in
`CLAUDE.md` is worth re-reading before adding anything to that file.

`job_cli.py` gets **no new verb**. Decision 3. The 21-verb CLI is what the
UI-first program is trying to shrink, and this field has no batch or scripted
use.

### Defaults at ingest

| Path | Default |
|---|---|
| `run_scan` discovery | `outbound`. the operator was not contacted; the scanner found it. |
| `job_cli.py fetch <url>` | `outbound`. He is pasting a posting he found. |
| the lead inbox UI lead inbox paste | **Asks.** A two-option control on the submit form, defaulting to outbound. This is the path a recruiter email actually arrives through. |
| `job_ingest.py` drain | Carries whatever the spool payload declares; defaults `outbound` when absent. |

`lib/job-inbox.ts` gains an optional `source` on its payload type beside the
existing optional `company` and `title`, and `job_ingest.py` reads it.

### the lead inbox UI surfaces

- **Lead inbox submit form:** a source control, defaulting to outbound.
- **Job detail `/jobs/[slug]`:** source shown, and editable, writing through
  `jobapi.py` like votes and notes. This is where the operator fixes a lead he
  mis-filed, and where he marks a recruiter ping that arrived by scan.
- **`/jobs` table:** an inbound marker. Inbound leads are rare and worth
  spotting.

### Statistics

This is the part that changes published numbers, so it is specified exactly.

Let `A` be every job with an `applied_at`, `A_out` those with source
`outbound` or `NULL`, and `A_in` those with `inbound`.

| Figure | Computed over | Note |
|---|---|---|
| Headline "N applications out" | `A` | Unchanged. He did send them all. |
| Headline "N came back" | `A` | Unchanged. |
| **Response rate** | `A_out` | Changes. The tile subtitle names the denominator. |
| **Median / fastest / slowest reply** | `A_out` | Changes. Strategic Ed's 1-day figure leaves. |
| Buckets | `A` | Every lead is still somewhere. |
| Funnel | `A` | Unchanged. |
| "Still waiting" plot | `A` | Unchanged. |
| **Inbound line** | `A_in` | New: count, and how many reached a loop. |

The reply-latency plot lists `A_out` only, and gains a one-line note when
`A_in` is non-empty saying how many inbound leads are excluded and why. A
number quietly missing from a chart is worse than a number explained.

Expected effect on today's data, assuming Strategic Education and hibob are
the two inbound rows (verified against the live API on 2026-08-28):

| Figure | Now | After |
|---|---|---|
| Median reply | 7 days | 8 days |
| Response rate | 11 of 32, 34% | 10 of 30, 33% |
| Replies in the latency plot | 11 | 10 |
| Interview loops, outbound | 8 | 7 |

The two rows move for different reasons and it is worth keeping them
straight. Strategic Education leaves the latency list, which is what moves the
median. hibob never replied at all, so it is in `silent` and not in the plot;
it only leaves the denominator.

**The empty case matters.** With no inbound leads at all, the inbound line and
the exclusion note must not render. the operator should not see an empty panel
explaining a distinction that has not yet applied to him.

---

## Migration

### Schema

Additive, automatic on the next `JobDB` open on the host, like every previous
column. No manual step.

### Backfill

Three passes, in order.

**Pass 1, the default.** Every job with an `applied_at` and no `source` gets
`outbound`. Self-limiting `WHERE` clause, safe to re-run, following the
`gate_model` backfill rather than the `read_at` one. (Read the comment in
`_migrate` before writing this: the `read_at` backfill is inside the
add-column branch specifically because re-running it would be catastrophic.
This one is not, but the distinction is the thing to understand.)

**Pass 2, the known exceptions.** These need the operator's confirmation row by row
before anything is written. Do not guess.

| Company | Today | Proposed | Basis |
|---|---|---|---|
| strategic-education | `ats=manual` | `source=inbound` | the operator, 2026-08-28: "a recruiter that pinged me" |
| hibob | `ats=recruiter` | `source=inbound` | The `ats` value says so |
| Nimbus (`Principal SRE`) | `ats=linkedin` | `source=outbound` | Found on LinkedIn, not contacted |
| nscale (`Senior SRE`) | `ats=linkedin` | `source=outbound` | Same, and never applied |
| origami-risk | `ats=linkedin` | `source=outbound` | Same |

**Pass 3, repairing `ats`.** For the four rows above whose `ats` is
`linkedin` or `recruiter`, set `ats` to the posting's real system where the
stored `url` determines it, and leave it alone where it does not.

**`uid` is not touched.** It is `{ats}:{company}:{ext_id}` and it is the
foreign key that `state_log`, `files` and `gaps` all point at. Rewriting it
means rewriting four tables to fix a cosmetic prefix, and the 2026-08-27
recovery is a recent reminder of what a hand-written multi-table update can
cost. The uid keeps its historical prefix; a comment in the migration says so,
because a future reader will otherwise find `linkedin:Nimbus:...` with
`ats='greenhouse'` and think something is broken.

### Deploy

Merge to `main` in both repos. job-hound's runner restarts `job-api` on
any `.py` change and lead-inbox's redeploys itself, so there is no manual step.
Run the backfill passes on the host after the schema migration has opened
once.

---

## Testing

Feature 1, in `lead-inbox/tests/reply-window.test.ts`:

- an application at exactly 30 days is **not** cold, at 31 it is (pin the
  boundary, both sides),
- a cold application that then gets a reply leaves the cold bucket,
- an application with a reply is never cold regardless of age,
- an `interviewing` lead is never cold, however long it has been quiet,
- the existing 2026-08-22 fixture still reproduces the artifact's numbers,
  with its cold rows accounted for.

Feature 1, in job-hound `test_staleness.py`: `COLD_AFTER_DAYS` and
`STALE_AFTER_DAYS` are distinct and neither is defined in terms of the other.

Feature 2, in job-hound:

- `set_source` rejects a value outside the enum,
- `set_source` writes a `state_log` row,
- `POST /jobs/{ident}/source` round-trips, and 4xx's on a bad value,
- `run_scan` ingest defaults to `outbound`,
- the migration is idempotent across two `JobDB` opens.

Feature 2, in lead-inbox:

- response rate and median exclude inbound,
- `NULL` source counts as outbound,
- with zero inbound leads, no inbound line and no exclusion note render,
- with only inbound leads, the outbound stats are null rather than `NaN` or
  zero-divided.

Run the reply-window suite under at least two timezones, as it is now.

---

## Sequencing

Four PRs. Each is independently mergeable and leaves the system correct.

1. **job-hound: `source` column, setter, endpoint, ingest defaults.** No
   reader changes anything yet; the column fills up quietly.
2. **job-hound: the backfill**, after the operator confirms the Pass 2 table. Includes
   the `ats` repair.
3. **lead-inbox: cold leads.** Independent of 1 and 2 and could ship first; it is
   the smaller feature and the one the operator will see immediately.
4. **lead-inbox: the split funnel and the source controls.** Depends on 1 and 2.

PR 3 is the one to start with if the next session has limited time.

## Open questions

- **The Pass 2 table needs the operator's confirmation**, row by row, before the
  backfill runs. That is the only thing blocking PR 2.
- **Does an inbound lead that the operator then formally applies to stay inbound?**
  Recommendation: yes. The question the field answers is who moved first, and
  that does not change later. Worth confirming, because it decides whether
  Strategic Education stays out of the outbound denominator after he submits
  a formal application.
- **`prepared` is still an undercount** (42 on record against 58 package
  folders on disk) and neither feature here touches it. It remains the
  files-table indexing item from the 2026-08-27 reconciliation.

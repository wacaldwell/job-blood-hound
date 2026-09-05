# Staleness signal: surfacing committed leads that stopped moving

Date: 2026-07-26
Status: approved, not yet implemented

## The problem

On 2026-07-26 the pipeline showed a lead in `drafted` scoring 93: Northgate, Site
Reliability Engineer Team Lead. Its documents had been generated on 2026-07-02
and it had never been submitted. Nothing in the system had surfaced that in the
24 days between. The audit trail was three rows and then silence:

```
2026-07-02  - -> discovered        scan
2026-07-02  discovered -> queued   mission-control ingest
2026-07-02  queued -> drafted      auto-draft v1
```

A generated package that is never sent is the most wasteful state in the
pipeline: the API call was spent tailoring documents, and the application was
never made. The digest ranks new leads and the inbox triages unread ones, but
once a lead reaches a committed state it goes quiet, and nothing pushes.

This is a distinct failure from the one `freshness.py` solves. That module asks
how old a *posting* is. This one asks how long since *the operator acted*.

## Scope

In scope: a derived staleness signal, surfaced in three places (CLI list, daily
Discord digest, the lead inbox UI jobs table).

Out of scope, deliberately:

- The lead inbox. It triages new unread leads one at a time; mixing in old
  committed leads would blur that single purpose.
- Any snooze or dismissal mechanism (see Decisions).
- Any auto-action on a stale lead. This system never acts on the operator's behalf.

## Decisions

Each of these was chosen over a named alternative.

**Hot means effort already spent, not high score.** A lead is eligible for a
staleness warning if its state is one of `queued`, `drafted`, `ready`,
`interviewing`. Score is irrelevant once committed. The alternative considered
was flagging high-scoring `discovered` leads too; rejected because the waste
being targeted is abandoned *investment*, and an untriaged discovered lead has
cost nothing yet.

**One threshold: 7 days.** Not two tiers in the data model. Northgate would have
been caught on day 7; the leads in flight today sit at 2 to 5 days and stay
quiet. A single constant is also a single thing to retune.

**The clock is `state_log`, never `jobs.updated_at`.** The nightly scoring pass
bumps `updated_at` without changing state (documented in CLAUDE.md), so it would
make every lead look permanently fresh. `state_log` only gains rows on real
actions.

**Gate runs and read stamps do not reset the clock.** Both write `state_log`
rows, but neither is a decision about the lead. Counting them would let a lead
quiet its own alarm without the operator doing anything. Transitions, votes, and notes
do reset it: each represents a judgment.

**No snooze command.** The warning clears when the state changes, or when a vote
or note is recorded. Adding a snooze would mean a new column, a new command, and
a second clock. The accepted cost: a lead legitimately waiting on an employer
(an `interviewing` lead where the ball is in their court) keeps warning. If that
proves annoying in practice, a note resets it, which is a fair thing to have to
write down.

**No blinking.** The lead inbox UI treatment is a static chip, amber at 7 days
and red past 14. A CSS animation on a dashboard that stays open reads as an
alarm you train yourself to ignore, and it fights `prefers-reduced-motion`. The
14-day red is a display-layer tier only; the data model still has one threshold.

**Derived at read time, no schema change.** Rejected: a denormalized
`last_activity_at` column on `jobs`, maintained by every audited setter. That
needs a migration and a backfill, and creates a second source of truth that can
drift from the audit log. Drift between two records of the same fact is exactly
the class of bug that cost a Mac-side `apply` in July (docs/single-source-of-truth.md).
The read-time JOIN is free at 411 rows and stays free well beyond that.

## Architecture

New module `staleness.py`, sibling to `freshness.py` and following its
contract: pure, import-safe, no network, no database, no file writes.

```python
COMMITTED_STATES = {"queued", "drafted", "ready", "interviewing"}
STALE_AFTER_DAYS = 7

def idle_days(last_activity_at, now=None) -> float | None
def is_stale(state, last_activity_at, now=None) -> bool
def staleness_label(state, last_activity_at) -> str | None
```

`staleness_label` returns `"idle 24d"` or `None`. Returning `None` rather than an
empty string keeps "not stale" distinguishable from "stale but unlabelable" at
every call site.

All database access lives in one new read-only method on `jobdb.py`:

```python
def last_activity(self, uids=None) -> dict[str, str]
```

One query, `GROUP BY job_uid`, returning uid to ISO timestamp, filtered to
exclude gate and read rows. Callers fetch once per render rather than per row.

Data flow:

```
db.list()             -> rows
db.last_activity()    -> {uid: iso_ts}     one query, not per row
staleness.is_stale()  -> per row, pure
```

## Surfaces

**CLI `list`.** `fmt_row` appends the marker to the existing age line, so rows
grow no taller:

```
[     drafted] [ 93] omnicell__site-reliability-engineer-team-lead__f2b2
               Site Reliability Engineer, Team Lead @ Northgate (n/a)
               age unknown · IDLE 24d
```

Plain uppercase text, no ANSI color: `job_cli.py` output is piped as often as it
is read in a terminal, and the codebase has no color helper today.

**Daily Discord digest.** A third section in `build_digest_sections`, placed
above the existing two because it is the only part about the operator rather than about
new leads:

```
**Needs attention** (2)
`93` · idle 24d · **Northgate**: Site Reliability Engineer, Team Lead · [open](...)

**New since last digest** (5)
...
```

Omitted entirely when nothing is stale, so a clean pipeline stays quiet. This is
the surface that actually solves the original problem: it arrives on cron
whether or not the operator goes looking.

**the lead inbox UI `/jobs` table.** An `idleDays: number | null` field on
`JobListItem`, rendered as a static chip on the row: amber at 7 days, red past
14.

## Error handling

The policy matches `freshness.py`: label honestly, never drop, and stay silent
on bad data. A staleness warning that cannot be trusted is worse than none,
because the section stops being read.

- No `state_log` rows for a job: not stale. A missing clock must never
  manufacture an alarm.
- Unparseable timestamp: `idle_days` returns `None`, mirroring
  `freshness.parse_iso`. Not stale.
- A state outside `COMMITTED_STATES`: never stale, checked before the clock is
  consulted at all.

## Testing

`test_staleness.py`, clock-injected via a `now=` parameter so no test depends on
wall-clock time and no new dependency (freezegun) is needed:

- boundary: 6.9 days not stale, 7.1 days stale
- every state in `COMMITTED_STATES` warns; `discovered`, `applied`, `skipped`,
  and `closed` never do
- a job whose only recent `state_log` row is a gate run stays stale
- vote rows and note rows do reset the clock
- missing rows and malformed timestamps return not-stale rather than raising

Integration:

- `jobdb.last_activity()` returns one row per job and matches a hand-computed
  `MAX(at)`, with gate and read rows excluded
- a digest with nothing stale omits the section entirely
- a regression test reconstructing the real Northgate history (drafted
  2026-07-02, no activity until 2026-07-27) asserting it flags stale, named for
  the bug it pins

On the lead-inbox side, the `idleDays` mapping gets a case in
`tests/job-hound-mapping.test.ts` alongside the existing field mappings. The
chip rendering itself is not tested: that repo runs vitest with
`environment: 'node'` and has no component test setup.

## Deployment

No migration. The signal is computed from `state_log` rows that already exist,
so it works against all 411 current rows the moment it deploys. job-hound
deploys by `git pull --ff-only origin main` on the tools host; lead-inbox
auto-deploys on merge.

The two repos are independent: the CLI and digest changes are useful on their
own, and the lead inbox UI chip can land separately.

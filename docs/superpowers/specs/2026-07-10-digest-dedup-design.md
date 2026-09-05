# Digest dedup: two-section "New vs Still open" digest

Date: 2026-07-10
Status: approved, ready for implementation plan

## Problem

The daily Discord digest showed identical leads for several days running. A
health check on the live host (`example-host`, cron `job-hound-daily.sh`
at 06:30) found the pipeline healthy in discovery (6-10 new leads/day) but two
compounding defects in ranking and delivery:

1. **No sent-deduplication.** `refine_pipeline` re-ranks the full active pool
   and posts the top 12 by score every day with no record of what was already
   sent. Same pool + same scores => byte-identical digest.
2. **Frozen ranking (separate, operational).** The LLM verdict tier fails with
   `401 Unauthorized` on the host, so the digest falls back to the deterministic
   `fit_score`, which is a static function of each job's title/location/salary
   and does not change day to day. This entrenches the same top leads.

This spec addresses defect #1 (the digest). Defect #2 is an operational key
rotation on the host, tracked separately (see "Out of scope" below); it is not
required for this change to be correct, but until it is fixed the "New" section
is the primary source of day-to-day change.

## Goal

The digest stops repeating itself while never silently dropping a strong,
still-open role. Each day the reader sees, at a glance, what is genuinely new,
with a compact recap of what is still open from prior days.

## Design

### 1. State: `digested_at` column

Add `digested_at TEXT` to the `jobs` table. It records the last time a lead was
included in a **posted** digest. `NULL` means never sent.

- Migration is an idempotent `ALTER TABLE jobs ADD COLUMN digested_at TEXT`
  guarded by a column-existence check in `jobdb.py`, applied on connect. Safe
  against the existing `jobs.db` on the host; no rebuild required.

### 2. Partition in `refine_pipeline`

Freshness and verify filtering are unchanged. The resulting fresh candidate set
is partitioned and each partition ranked by `fit_score` descending:

- **New** = candidates with `digested_at IS NULL`. Catches both brand-new
  discoveries and leads that were previously below the fold and never actually
  sent.
- **Still open** = candidates with `digested_at` set.

Scope narrowing: the digest sections include only `discovered` leads. Leads the
human has already engaged (`queued`, `drafted`, `ready`) are dropped from both
sections (they remain visible in `list`/`next`). This is a deliberate change
from today's behavior, which included all active states in the digest.

`refine_pipeline` remains free of the "sent" side effect: it returns the two
partitioned lists (plus existing counts) and does not stamp `digested_at`. It
keeps writing `fit_score` via `set_fields` as it does today.

### 3. Stamping only on delivery

`cmd_refine` stamps `digested_at = now` for every lead shown in **either**
section, but **only after `notify.post_discord` returns success**. Consequences:

- A dry `refine` (no `--digest`) never marks leads as sent.
- A failed webhook post never marks leads as sent (they reappear as New next
  run, which is the safe direction).
- The MCP adapter, which does not deliver a digest, never stamps.
- Today's "New" becomes tomorrow's "Still open".

### 4. Formatting

Existing per-line format (`fit · age · loc · role`) is retained for the New
section. Still-open collapses to compact one-liners.

```
**Job-hound digest**  (fit · age · loc · role)

**New since last digest** (2)
`92` ·  6h · rem · **acme**: Staff SRE · [open](…)
`88` ·  1d · rem · **globex**: Platform EM · [open](…)

**Still open** (10 previously sent)
Northgate 93 · beehive 90 · Nimbus 90 · waypoint 88 · summitbank 84 · … (+3 more)

Pipeline: applied 11 · discovered 291 · …
(145 stale >30d hidden · 47 onsite held)
```

Parameters:

- New section cap: **12** ranked leads.
- Still-open cap: **10** collapsed one-liners, remainder as a `(+N more)` tail.
- Empty New: the digest still posts, with a `No new leads today.` line in place
  of the New section, followed by the Still-open recap. It does not go silent.

## Voice / hard rules

No em dashes anywhere in generated output; the existing post-process safety net
in the generator/notify path still applies to digest text. The digest invents
nothing; it only formats DB rows.

## Testing

- `jobdb` migration: connecting to a DB without `digested_at` adds it; connecting
  to one that already has it is a no-op. A stamp helper sets/reads the value.
- Partition: given a candidate set with a mix of `digested_at` NULL/set across
  `discovered`/`queued` states, `refine_pipeline` returns New = never-sent
  `discovered` only, Still-open = sent `discovered` only, both score-ordered;
  `queued`/`drafted`/`ready` excluded from both.
- Stamping: `cmd_refine` with a successful post stamps every shown lead; with a
  failed post stamps nothing; dry `refine` stamps nothing.
- Formatting: two-section render with caps and `(+N more)` tail; empty-New render
  shows `No new leads today.`; no em dashes in output.
- Regression: a lead shown today appears under Still open (not New) tomorrow.

## Out of scope

- **Host API key 401.** The `ANTHROPIC_API_KEY` on `example-host` is
  rejected (HTTP 401), breaking LLM verdicts and `draft`. This is an operational
  key rotation, handled separately from this code change.
- No change to discovery, freshness policy, scoring math, or the state machine.
- No "suppress for N days" resurfacing logic; the two-section model makes it
  unnecessary.

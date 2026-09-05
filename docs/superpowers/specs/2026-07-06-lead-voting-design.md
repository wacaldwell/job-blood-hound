# Lead voting from the lead-inbox jobs tab

Date: 2026-07-06
Status: approved (design), pending implementation
Repos: job-hound (schema, drain, learning), lead-inbox (API, UI)

## Purpose

Give the operator a lightweight up/down signal on incoming leads, distinct from
lifecycle state, that (a) feeds the fit-ranking history so future scans rank
better and (b) doubles as a triage aid in the lead-inbox jobs tab. Votes are NOT
lifecycle actions: a down-voted lead can still be queued and applied to.

Decisions made during brainstorming:

- Signal shape: thumbs up/down plus an optional one-line note. No star ratings.
- Surface: lead-inbox jobs tab only. The archived CLI/MCP vote work
  (tag `archive/apply-next-vote`) stays shelved, though its jobdb method is
  the model for the new one.
- Write path: spool + drain (approach A). lead-inbox keeps its read-only handle on
  jobs.db; job-hound's Python side remains the only DB writer.
- Learning: LLM history block only. Deterministic fit_score is untouched
  (company-level weighting is a phase-2 option if needed).

## Architecture

```
jobs tab row (thumbs)                          fit.py refine
      |                                              ^
      v                                              |
POST /api/jobs/[slug]/vote          build_history() includes votes
      |                                              |
      v                                              |
votes/ spool file  --(job_ingest 5-min timer)-->  jobs.db
      |                                        vote / vote_note / voted_at
      v                                        + state_log audit row
read overlay in lib/job-hound.ts
(instant UI feedback before drain)
```

## 1. Schema and DB layer (job-hound)

- New nullable columns on `jobs`: `vote` TEXT ('up' | 'down'), `vote_note`
  TEXT, `voted_at` TEXT (ISO timestamp). Idempotent ALTER TABLE migration in
  the existing jobdb schema-init path; must be validated against an existing
  populated jobs.db.
- New method `JobDB.set_vote(uid, vote, note=None)`:
  - vote in ('up', 'down', None); None clears vote, note, and voted_at.
  - Overwrites any previous vote (last write wins).
  - Appends a state_log row with state unchanged and note
    `vote: <up|down|cleared>` plus the note text when present.
  - Adapted from `set_apply_next_vote` on tag `archive/apply-next-vote`.

## 2. Spool and drain (job-hound)

- Spool directory: `$JOB_INBOX_DIR/votes/` (both sides already share
  `JOB_INBOX_DIR`, default `~/.lead-inbox/data/job-inbox`), with `processed/` and
  `failed/` subdirectories under it. One JSON file per vote event:
  `{slug, vote, note, voted_at}`, written atomically (temp + rename) like the
  existing submission spool. Filename carries a timestamp plus slug for
  natural ordering.
- `job_ingest.py` gains `drain_votes(db, votes_dir)` called from the existing
  5-minute timer entry point:
  - Process files in name order (oldest first) so last write wins.
  - Resolve slug to uid with the existing ident resolution; unknown slug moves
    the file to `failed/` with a WARN log, never raises.
  - Malformed JSON moves to `failed/` with a WARN log.
  - Success calls `set_vote` and moves the file to `processed/` (archive,
    never delete).
  - Absent or empty spool directory is a no-op, so the job-hound PR can land
    before the lead-inbox PR.

## 3. API and instant reads (lead-inbox)

- `POST /api/jobs/[slug]/vote` with body `{vote: 'up'|'down'|null, note?}`:
  - Validates vote against the enum, caps note at 280 chars.
  - Writes the spool file; 201 on success, 400 on validation failure, 500 on
    spool write failure.
  - Spool path derived from the existing `config.jobInboxDir`
    (`JOB_INBOX_DIR`), no new env var.
- Read overlay: `getJobsDashboard` and `getJobDetail` read pending spool files
  and overlay `{vote, voteNote, votedAt}` per slug on top of DB values, newest
  timestamp wins. jobs.db stays `readonly: true`.

## 4. UI (lead-inbox jobs tab)

- Thumbs up/down on each lead row; active vote highlighted; clicking the
  active vote clears it (POST with vote: null).
- Optional note input in the job detail panel, shown once a vote exists.
- List behavior: votes visible on rows; sort/filter places up-voted leads
  first and sinks down-voted leads to the bottom (visible, not hidden).

## 5. Learning (job-hound, fit.py)

- `build_history()` includes voted jobs alongside lifecycle decisions:
  - vote up -> "liked" example; vote down -> "disliked" with vote_note as the
    stated reason.
  - Labeled distinctly from queued/applied (stronger positive) and
    skipped (stronger negative) so the LLM sees signal strength.
  - History cap stays bounded (mix of recent votes and lifecycle decisions).
- No changes to deterministic scoring in this phase.

## 6. Error handling summary

- Drain never crashes the ingest timer: bad files quarantine to `failed/`.
- API validates inputs and returns structured errors.
- Vote for a job later deleted/unknown: quarantined, logged, skipped.

## 7. Testing

job-hound:
- Migration adds columns to an existing populated DB without data loss.
- set_vote semantics: set, overwrite, clear, state_log audit rows.
- drain_votes: happy path, malformed JSON, unknown slug, empty dir no-op,
  ordering (last write wins).
- build_history: includes votes with correct labels and reasons.

lead-inbox:
- Route validation (enum, note cap) via the repo's test harness if present,
  otherwise typecheck + lint + live smoke test.

## 8. Rollout

1. job-hound PR: schema, set_vote, drain_votes, build_history, tests.
2. lead-inbox PR: config, API route, read overlay, UI buttons.
3. Order matters only in that job-hound lands first; the drain tolerates an
   absent spool and the UI tolerates missing columns never occurring (it ships
   after the schema).
4. Tools host picks both up through existing auto-deploy (daily.sh pull for
   job-hound, actions runner for lead-inbox). The ingest timer uses new code on
   its next 5-minute tick after the pull.

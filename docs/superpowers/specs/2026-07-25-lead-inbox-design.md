# Lead inbox: processing leads instead of looking at them

Date: 2026-07-25
Status: approved (design), pending implementation
Repos: job-hound (schema, write API), lead-inbox (inbox UI, API proxy)

## Purpose

Turn the lead-inbox jobs surface from a list the operator reads into a queue he works. Today
the jobs tab shows leads and accepts an up/down vote; there is no way to mark a
lead processed, no way to record a thought about it, and no way to move it
through the pipeline without dropping to a terminal. The result is a page that
gets looked at rather than used.

This design adds an inbox: one lead at a time, keyboard driven, with the three
actions that actually constitute processing a lead (vote, note, advance state),
and a durable read/unread concept so the queue drains and stays drained.

It also replaces the spool write path with a local write API, because one of
those three actions (advancing state) needs validation feedback at click time
and a spool structurally cannot provide it.

## Decisions made during brainstorming

- Primary job of the surface: triage of new leads, queue-vs-skip in one pass.
  Not archive browsing, not pipeline review.
- Shape: an inbox, one lead at a time, keyboard driven. Not a split pane, not a
  dense expandable table.
- Actions on a lead: upvote/downvote, freeform note, advance state. On-demand
  gate runs were considered and cut.
- Read/unread: a lead becomes read only by explicit action. Opening it does not
  clear it. The unread queue has to be trustworthy.
- Write path: a local FastAPI service in job-hound wrapping `jobdb.py`. Not a
  spool, and not direct SQLite writes from TypeScript.
- The existing vote spool migrates to the API, so there is exactly one write
  channel for interactive actions.
- Backlog policy: start clean. All 411 pre-existing leads are marked read at
  migration. The inbox opens empty and fills from the next scan.
- Scope split: this branch is the write layer plus the inbox. The index table
  redesign (company as its own column, salary, density) is a second branch.
- AWS: deliberately deferred to its own project with its own goal. The write
  API is the seam that makes a later move a port rather than a rewrite.

## Current state, verified against the live host DB

- 411 rows. 18 have a blank `posted_at` (empty string, zero NULLs), 4 were
  posted within 48h, 389 have good dates and are older than 48h.
- 9 rows have ever been gated: 4 RECOMMEND, 3 DO_NOT_APPLY, 1 NEEDS_REVIEW,
  1 ERROR. The other 402 are ungated because the daily scan never gates.
- Nothing is broken in storage. The jobs tab looks empty because its
  `Fresh only (48h)` default keeps undatable rows and hides datable ones
  (`isFresh` returns true when `postedAt` is null), so the surviving set is
  almost entirely the rows that cannot display a date.
- Votes and vote notes already work end to end: `jobdb.set_vote`, the
  `votes/` spool, `job_ingest.drain_votes` on a 5-minute systemd timer, a
  pending-vote read overlay in `lib/job-hound.ts`, and vote folding into the
  fit corpus in `fit.py`.
- There is no `read_at`, `seen_at`, `rating`, or `priority` column anywhere.
  `digested_at` exists but means "appeared in a Discord digest".
- `jobs.notes` (TEXT) exists and no production code writes it, but it is not
  dead: `fit.py:149` reads it as the stated reason for any job in a pursued
  state when building the few-shot corpus that trains the LLM ranker. lead-inbox
  also reads it as `JobDetail.notes`.

## Architecture

```
       inbox (one lead at a time, keyboard driven)
                        |
        Next.js API route proxy (holds the token)
                        |
                        v
       job-hound write API  (FastAPI, 127.0.0.1, bearer token)
                        |
                        v
                   jobdb.py            <-- still the only writer
          set_vote / set_notes / set_read / transition
          + state_log audit row for every one of them
                        |
                        v
                    jobs.db  (WAL, busy_timeout)
                        ^
                        |
     lead-inbox reads directly, read-only, query_only = ON (unchanged)
```

The submission spool (`pending/`, drained by `job_ingest.py`) is unchanged and
stays a spool. That work is genuinely asynchronous: it fetches a JD and calls
the model. The split is by the nature of the work, not by convenience:
interactive actions go through the API, background work stays on the spool.

## 1. Schema (job-hound)

One new nullable column on `jobs`, added through the existing `ADDED_COLUMNS`
mechanism in `jobdb.py`:

- `read_at` TEXT, ISO timestamp. NULL means unread.

**The backfill must fire exactly once.** A naive
`UPDATE jobs SET read_at = ? WHERE read_at IS NULL` in `_migrate()` would run on
every `JobDB` open and mark every future new lead read the moment it was
discovered, silently destroying the feature. The backfill therefore runs inside
the branch that adds the column, so it happens at the migration and never
again. Every row created afterward gets `read_at = NULL` and is unread.

This differs from the `gate_model` backfill precedent, which is safe to re-run
because its WHERE clause is self-limiting. The `read_at` backfill is not, so it
gets structural protection instead.

Freeform notes use the existing `notes` column. `vote_note` keeps its current
meaning, the reason attached to a vote.

**This couples the inbox to the ranker, deliberately.** Because `fit.py:149`
reads `notes` as the pursued-reason, a note written during triage becomes the
stated reason the LLM ranker learns from once that lead reaches a pursued
state. That is the behavior we want (the note really is the best available
"why I pursued this"), but it is a change to model inputs, so it gets an
explicit test rather than being left as an accident. Notes on a `discovered`
lead do not enter the corpus at all: that branch uses `vote_note`.

## 2. DB layer (job-hound)

Two new setters on `JobDB`, modeled on `set_vote`, each appending a `state_log`
row with state unchanged so the timeline stays complete:

- `set_read(uid, read=True)`. Sets `read_at` to now, or clears it when
  `read=False`. Audit note `read` / `unread`.
- `set_notes(uid, text)`. Writes `notes`. Audit note `note: <first line>`.
  Empty or whitespace-only text clears the column. Text is capped at 4000
  characters, which is a working note rather than the 280-character one-liner
  `vote_note` holds.

**What marks a lead read.** Advancing state marks it read in the same call:
queueing or skipping a lead is an explicit disposition, and leaving it in the
unread queue afterward would be a bug. Voting and noting do not mark it read,
because both are things you do while still deciding. `R` covers the remaining
case, a lead you have judged and want out of the queue without a state change.

`read_at` joins the protected set that generic `set_fields` refuses (the same
guard rail the gate columns have), because `set_fields` writes no audit row and
an unaudited read stamp would silently drain the queue.

`notes` deliberately does NOT join that set. Two existing tests write it
through `set_fields` (`test_fit_history.py:77`,
`test_jobdb_set_fields_gate_guard.py:31`, where it is the example of a
legitimate generic write), it gates nothing, and forbidding it would break
working tests to buy an audit row on a field that cannot block anything.
`set_notes` is the audited path the API uses, not a prohibition on the
generic one.

State transitions reuse the existing validated transition path unchanged. No new
transition logic, and no new legal transitions.

`JobDB.__init__` gains `PRAGMA journal_mode = WAL` and a `busy_timeout`, so the
write API, the nightly scan, the 5-minute ingest timer, and `bin/jh` stop being
able to block each other.

## 3. Write API (job-hound)

New `jobapi.py`: FastAPI, bound to `127.0.0.1`, one systemd user unit beside the
existing `job-ingest` pair, bearer token read from the shared env file
(`JOB_API_TOKEN`). Not exposed off the host.

| Method | Path | Wraps |
|---|---|---|
| POST | `/jobs/{ident}/vote` | `set_vote` |
| POST | `/jobs/{ident}/note` | `set_notes` |
| POST | `/jobs/{ident}/read` | `set_read` |
| POST | `/jobs/{ident}/state` | validated transition |
| GET | `/jobs/{ident}/transitions` | legal next states |

`GET /transitions` is the point of the design: the UI never holds a copy of
`TRANSITIONS`. The state machine stays in one language, in one file.

Legal is not the same as appropriate, and the endpoint returns the true state
machine, so a `queued` lead lists `drafted` and a `drafted` one lists `ready`.
Both of those mean documents exist on disk, and neither is a triage decision:
`drafted` is stamped by the generation path, `ready` says the package was
reviewed. **The inbox offers only `queued`, `skipped`, and `discovered`**, and
treats the endpoint as the check on what it offers rather than the list of what
to render. The rest belong to the draft pipeline.

`ident` resolves through the existing `db.resolve`, so uid, slug, or unique slug
prefix all work, matching `bin/jh`.

Responses: 200 on success returning the updated row, 400 on validation failure,
404 on unresolvable ident, 409 on an illegal transition carrying the
`TransitionError` message verbatim so the UI can show it, 422 on a malformed
request body (a missing `state`, a null `text`, unparseable JSON), and 503 when
the service is misconfigured (`JOB_DB` or `JOB_API_TOKEN` unset), which is the
fail-closed path rather than an error to retry.

**422 does not carry the same body shape as the others.** 400, 404, 409, and
503 return `detail` as a string, ready to render. 422 is FastAPI's own
validation response and returns `detail` as a list of objects (each with `loc`,
`msg`, `type`). A client that assumes a string renders garbage on exactly the
one error class it did not plan for, so handle 422 separately.

**Hard rule, stated in the module docstring and enforced by the absence of any
such endpoint:** nothing here submits an application, fills an external form, or
logs into a job site. Stamping `applied` is a state write only, exactly as the
MCP server's `job_apply` already does.

## 4. Inbox (lead-inbox)

New route `app/jobs/inbox/page.tsx` plus a nav entry. `/jobs` is untouched in
this branch; its redesign is the follow-on branch.

- `lib/job-api.ts`: typed client for the write API, base URL and token from
  `lib/config.ts` (new `JOB_API_URL` and `JOB_API_TOKEN` env vars).
- Next.js API routes proxy every write, so the token never reaches the browser.
- The existing `POST /api/jobs/[slug]/vote` is rewired from `submitVote` to the
  API. `lib/job-votes.ts` and the `readPendingVotes` overlay retire, since
  writes become synchronous and the overlay exists only to hide drain lag.
  `drain_votes` stays in job-hound so any spool files already on disk still
  land.
- `getInboxQueue()` in `lib/job-hound.ts` selects the queue, reusing
  `mapListItem`, `sortJobs`, `postedLabel`, and `gateTone` rather than
  reimplementing any of them.
- Queue switcher: **unread** (default), **upvoted**, **all**. The upvoted view
  is the "show me the leads I liked" ask.
- Optimistic writes reuse the generation-guarded rollback already proven in
  `JobTable.tsx`, then refetch the row.

Per lead the card shows: title, company, location, salary, posted age, gate
verdict, score with its origin, a truncated JD description, the gate
requirement breakdown when `gate_json` is present, the note field, and the
action bar.

Keyboard map:

```
J / K   next / previous lead
U / D   upvote / downvote
Q / S   queue / skip
N       focus the note field
O       open the posting
R       mark processed (clears it from unread)
```

## 5. Error handling

- An illegal transition returns 409 and the UI shows the message. It does not
  optimistically move the lead.
- The write API being down is visible: writes fail loudly with a retry
  affordance rather than being silently queued. This is the deliberate
  trade for synchronous validation.
- `GET /transitions` failing degrades to hiding the state actions, never to
  showing an illegal one.
- A note is capped server side at 4000 characters rather than rejected, so a
  long paste is truncated instead of lost.
- A state transition that succeeds but whose read stamp fails still leaves the
  lead in a correct state. The read stamp is written in the same call, so this
  means one extra `R` press, not a corrupt row.

## 6. Testing

job-hound:
- Migration against a populated copy of the real DB: column added, every
  pre-existing row marked read, a second open leaves a newly inserted unread
  row unread. This is the test that protects the feature.
- `set_read` and `set_notes`: set, clear, audit rows, `updated_at` behavior.
- Both columns rejected by `set_fields`.
- API via FastAPI `TestClient`: happy paths, 404 on unknown ident, 409 on
  illegal transition with the message preserved, 401 without the bearer token.
- WAL enabled without breaking existing readers.

lead-inbox:
- vitest for `lib/job-api.ts` and the inbox queue selection, alongside the
  existing `tests/job-format.test.ts` and `tests/job-sort.test.ts`.
- Local runs need a copy of `jobs.db` pulled from the host with
  `JOB_HOUND_DB_PATH` pointed at it, because the Mac has no
  `~/job-hound/jobs.db`. Take that copy with
  `sqlite3 ~/job-hound/jobs.db ".backup /tmp/jobs-copy.db"`, or copy `jobs.db*`
  including the sidecars: now that the database runs in WAL, committed rows can
  live in `jobs.db-wal` and a copy of `jobs.db` alone can be missing them.
  Never point it anywhere that could become a second real database.

## 7. Rollout

1. job-hound PR: schema, setters, WAL, `jobapi.py`, systemd unit, tests, plus
   the CLAUDE.md updates the new service and env vars require (`JOB_API_TOKEN`
   in the Environment section, the write API in Architecture, and the note that
   the lead inbox UI is no longer read-only-by-spool).
2. Deploy job-hound and start the API service on the host.
3. lead-inbox PR: config, client, proxy routes, inbox, vote rewire.
4. Deploy lead-inbox.

Order matters here in a way it did not for lead voting: the API must be live
before lead-inbox's new build points at it, or voting breaks in the gap. Back up
`jobs.db` on the host before the first open with the new code, because the
read-at backfill is one-way.

## 8. Risks

- WAL is a persistent property of the database file and affects every reader on
  the host. Validate against a copy first.
- The backfill is irreversible. Hence the backup.
- Two lead-inbox clones exist. `~/code/infrastructure/lead-inbox` is live;
  `~/code/repos/lead-inbox` is stale at PR #22. Work happens in the former.
- Retiring the vote spool touches working code. `drain_votes` stays so nothing
  already spooled is lost.

## 9. Out of scope

- The index table redesign (company column, salary, density). Second branch.
- On-demand gate runs from the UI.
- Any AWS hosting work, and any remote access such as a tunnel.
- Dating the ingest and fetch paths. `job_ingest.fetch_posting_meta` never
  returns a posting date, which is the cause of 11 of the 18 blank
  `posted_at` values. Worth fixing, but not here.

## 10. Known residual, carried into the lead-inbox branch

`fit.build_history` renders one bullet per decision, so any newline in a reason
field can fabricate a corpus entry the ranker reads as a real past decision.
This branch normalized only `notes` (`fit.py`, collapse whitespace and cap at
280), because `notes` is the field it made writable. Three sibling fields still
reach `_history_block` unnormalized: `skip_reason` and `close_reason`, which
come from CLI `--reason` flags and are single-line in practice, and
`vote_note`, which is now length-capped at 280 on both write paths but is not
newline-collapsed.

`vote_note` is the one that matters next. The moment the inbox ships a
multi-line vote-note input, a paste under 280 characters can inject a bullet.
The fix is one normalization inside `_history_block` covering all four fields,
which is better than four call sites anyway. Do it in the lead-inbox branch, before
that input ships, not after.

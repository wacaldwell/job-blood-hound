# Disposition panel: recording an outcome without a terminal

Date: 2026-08-11
Status: approved (design), pending implementation
Repos: job-hound (write API), lead-inbox (detail page UI, API proxy, table filter)

## Purpose

A rejection arrives and there is no way to record it from the page the operator is
looking at. The lead inbox UI job detail page at `/jobs/<slug>` displays
state, outcome, and close reason but offers no control to set any of them, so
every disposition requires dropping to `bin/jh` on the tools host. The
practical result is that leads sit in `applied` or `interviewing` after the
loop is over, and the pipeline view shows a pipeline that no longer exists.

This design adds a disposition panel to the detail page: the legal lifecycle
transitions for that lead, an outcome picker on close, and a mandatory reason
on the two terminal actions. It also makes the disappearance of a closed lead
from the table deliberate instead of incidental.

Motivating case, recorded live while this was being designed: the Vertex Analytics
"Manager, DevOps Engineering" loop reached the decision stage and came back a
rejection on 2026-08-11. Closing it took a CLI round trip
(`bin/jh close ... --outcome rejected --reason ...`) with the browser open on
the very page that should have accepted the click.

## Decisions made during brainstorming

- Scope is disposition only. Editing posting fields (title, company, location,
  salary) was considered and cut: it needs new endpoints and new setters, and
  `company` doubles as the ATS API slug, so it is a materially larger change
  with a real corruption risk.
- No hide flag. A dismiss bit that removes a row without recording an outcome
  would be a second disposition concept living beside `state` plus `outcome`,
  and it would drift from the audited lifecycle exactly the way a second
  `jobs.db` once did.
- One surface: the detail page. A per-row action menu on the pipeline table and
  a dedicated "awaiting decision" sweep queue were both considered and
  deferred. the operator is already on the detail page when he learns an outcome.
- The panel offers every legal transition except `drafted`. See below.
- Closed and skipped rows leave the default table view by an explicit rule, not
  as a side effect of the freshness filter.

## Current state, verified against the code

- `jobapi.py` `POST /jobs/{ident}/state` already accepts `state`, `note`, and
  `outcome`, validates the state against `jobdb.STATES`, turns a
  `TransitionError` into a 409 with the message intact, and marks the lead read
  in the same call. It is a complete lifecycle endpoint already.
- It does NOT write `close_reason` or `skip_reason`. Those columns are set by a
  second call in the CLI: `db.set_fields(uid, close_reason=...)` in `cmd_close`
  (`job_cli.py:703`) and `skip_reason` in `cmd_skip` (`job_cli.py:619`).
- `jobdb.set_fields` guards the gate columns, the audited columns, and the
  interview columns. `close_reason` and `skip_reason` pass through it, so no
  `jobdb.py` change is needed.
- lead-inbox already has the full client path: `lib/job-api.ts` `postState` and
  `getTransitions`, plus route handlers at `app/api/jobs/[slug]/state/route.ts`
  and `app/api/jobs/[slug]/transitions/route.ts`. The bearer token stays
  server-side in the route handlers.
- `app/jobs/[slug]/page.tsx` is read-only apart from `VotePanel`. It renders a
  "Close reason" line at line 189 and a "Skip reason" line at line 188 that are
  permanently blank for anything disposed of through the API, because the API
  never wrote them.
- `app/jobs/inbox/page.tsx` already has the pattern to copy: a filtered
  transition set (`TRIAGE_TRANSITIONS`), a label map, a `busy` lock so a double
  click cannot fire two POSTs, and per-write `actionSeq` refs that only roll
  back when the ref still matches the captured value.
- `_payload` in `jobapi.py` returns uid, slug, state, vote, vote_note, notes,
  read_at, and updated_at. It does not return `outcome` or `close_reason`.
- `COMMITTED_STATES` is `{queued, drafted, ready, interviewing}` in both
  `staleness.py:43` and `lead-inbox/lib/job-sort.ts:23`. `closed` and `skipped` are
  not in it, so a closed old posting is filtered by the "Fresh only" default as
  an ordinary stale discovery.
- Relevant transitions from `jobdb.TRANSITIONS`: `interviewing` goes to
  `closed` or back to `applied`; `applied` goes to `interviewing` or `closed`;
  `ready` goes to `applied`, `drafted`, or `skipped`; `closed` is terminal.
- `jobdb.OUTCOMES` is rejected, withdrawn, offer, accepted, ghosted, other.

## Design

### 1. `reason` on the write API

`StateIn` in `jobapi.py` gains `reason: str | None = None`.

**Revised after review (Codex P2 on PR #88).** The first version wrote the
reason with a follow-up `set_fields` call after `set_state` had already
committed. A crash between the two commits left a terminal row with no reason,
and `closed` has no outgoing transition to repair it with, which is the
inconsistency this change exists to prevent. `set_state` now takes `reason=`
and writes the column in the same UPDATE as the state, alongside the
`state_log` insert it already commits atomically. `REASON_COLUMN` lives in
`jobdb.py`, which owns the state machine, and `cmd_close` and `cmd_skip` drop
their own follow-up writes so there is one mechanism instead of three. The
paragraphs below describe the behaviour, which is unchanged.

The reason is written to the column that belongs to the new state:

- `closed` writes `close_reason`
- `skipped` writes `skip_reason`
- any other state with a non-empty `reason` is a **400**

The 400 is deliberate. A reason sent with `applied` has no structured column to
live in, and silently dropping a string the operator typed into a mandatory
field is worse than refusing it. The `note` parameter already covers the
free-text case for every state, and it lands in `state_log` where the audit
trail is.

Order of operations inside the endpoint: `set_state` (carrying the reason),
then `set_read`, and return `_payload` of the row `set_read` returns, exactly
as before. A refused transition raises before any write, so it stores no
reason. A no-op repost of the current state returns early and does not rewrite
an existing reason, which keeps the endpoint's documented retry safety and
costs nothing, since amending a reason is not an operation any surface offers.

`set_read` remains its own commit. That is pre-existing, and the failure it
implies (a disposed lead still sitting unread) is both self-correcting and
visible, unlike a terminal row with a missing reason.

This is the only backend change. The endpoint becomes able to produce exactly
the row `bin/jh close --outcome X --reason Y` produces, which is the property
that matters: two write paths that disagree about which columns they populate
are a slow-motion split brain even when they share a database.

### 2. Pass-through in lead-inbox

`reason` is added to `postState`'s options in `lib/job-api.ts` and to the
parsed body in `app/api/jobs/[slug]/state/route.ts`, with the same
`typeof === 'string'` guard `note` already gets. No logic in either place. The
route handler stays a proxy.

### 3. `DispositionPanel` on the detail page

A new panel in `app/jobs/[slug]/page.tsx`, rendered beside the existing "Your
take" vote panel.

**Buttons.** On mount the panel fetches
`GET /api/jobs/<slug>/transitions` and renders one button per returned target,
minus `drafted`. `drafted` is excluded because that state means generated
documents exist on disk; only `job_generate` may make that claim, and a button
that fakes it produces a row whose package folder is empty. This is the same
reason the inbox filters it, and the exclusion carries the same comment.

Labels: `queued` "Queue", `discovered` "Send back", `ready` "Mark ready",
`applied` "Mark applied", `interviewing` "Interviewing", `skipped` "Skip",
`closed` "Close".

For a lead in `interviewing` that yields two buttons, "Close" and "Back to
applied", which is the Vertex Analytics case.

**Forward moves** (`queued`, `discovered`, `ready`, `applied`, `interviewing`)
fire on click and send the optional note from a note field.

**Revised after re-review (Codex P2 on PR #86).** The split below keys on
whether an action reads as negative, which is the wrong criterion. The right
one is irreversibility. `TRANSITIONS` allows `applied` to reach only
`interviewing` and `closed`, with no route back to `ready` or `drafted`, so a
misclick on "Mark applied" is exactly as permanent as a close, and it stamps
`applied_at`, the field the work-search record is built on. `skipped` is in
fact reversible (`skipped` goes back to `queued`) and still confirms, because
it takes a reason. So the two rules are now separate: `needsConfirm` (does this
get a confirm step) and `needsReason` (does it demand written justification).

`applied` therefore confirms, carrying the hard warning text and the required
"I submitted this myself outside Job Hound" acknowledgment from
`lead-inbox/docs/job-hound-controlled-actions-design.md`. That checkbox is not
ceremony in this project: the first hard rule is that nothing here ever submits
an application, so the one state asserting an application exists should record
who submitted it. It clears on cancel and after every write so it is never
sticky.

One deliberate divergence from that older doc: it lists the note as REQUIRED
for apply, and it is optional here, because the operator explicitly chose "note
optional on forward moves, mandatory on Close and Skip" when approving this
design, and this confirmation exists for irreversibility rather than to capture
a reason. Flagged rather than decided silently.

**Terminal moves** (`closed`, `skipped`) reveal an inline confirm step in the
panel rather than a modal:

- an outcome `<select>` on close only, holding the six `OUTCOMES`, defaulting
  to `rejected` because that is the overwhelmingly common case
- a reason textarea, required
- Confirm and Cancel, with Confirm disabled while the reason is empty

The reason text is sent as both `note` and `reason`, so it lands in `state_log`
and in the structured column in one call.

**Concurrency.** A `busy` flag disables the whole action bar while any write is
outstanding, and an `actionSeq` ref guards rollback, both copied from the
inbox. Two audit rows for one perceived click is the failure mode being
prevented.

**After a successful write** the panel re-fetches `/api/jobs/<slug>` rather
than patching local state. `_payload` returns neither `outcome` nor
`close_reason`, so patching optimistically would mean rendering an outcome the
page invented from what it sent. One extra request on a rare action buys a page
that only ever displays what the database actually holds. The re-fetch also
refreshes the lifecycle history panel, which just gained a row.

**After close** the transitions endpoint returns an empty `next`, and the panel
renders a terminal line in place of the buttons: the word "Closed" plus the
outcome actually stored on the row, so a lead closed as `ghosted` does not read
`rejected`. The same empty-`next` path covers any future terminal state without
a special case.

**Revised after review (Codex P2 on PR #86).** An empty `next` only means
terminal when the fetch succeeded. As first written the panel also showed that
line while the request was in flight and after it failed, reporting an
unreachable write API as lifecycle truth and telling the operator a live lead
was finished. The panel now takes an explicit `status` of loading, ready, or
unavailable, derived from a generation tag on the answer rather than set at the
top of the effect (`react-hooks/set-state-in-effect`, the same constraint the
inbox works around with `loadedView`). The tag also makes a post-write refetch
return to loading, so the previous state's buttons are never left on screen and
clickable against a lead that has already moved.

**And again on re-review (Codex P2 on PR #86).** That first pass tagged the
transitions read and left the detail read untagged, which is the same bug in
the other half of the page. Each write starts another detail read, so an older
one finishing last could overwrite newer state and history, and an older
failure could replace a good newer read with the unavailable page. The detail
effect now captures a sequence from a ref and drops its own result if a newer
read has started, on the success path, the failure path, and the `finally`.

The two guards are deliberately different shapes. Transitions carry a
generation tag on the stored value because the render has to tell loading from
ready from unavailable. The detail read only needs last-write-wins, so a ref is
enough and avoids restructuring `job`, `error`, and `loading` into one object.

**Errors** render the write API's message verbatim in the panel, the way the
inbox surfaces `actionError`. A `TransitionError` message names exactly which
transition is not allowed and from where, and that text is more useful than
anything the UI could compose.

### 4. Closed and skipped leave the default table view

The pipeline table in `app/jobs/page.tsx` excludes rows in `closed` and
`skipped` unless the state filter explicitly selects that state, independent of
the "Fresh only" checkbox.

Today a closed lead vanishes only because it is an old posting that is not in
`COMMITTED_STATES`, so the freshness filter catches it as if it were a stale
discovery. That is luck, not design: unchecking "Fresh only" brings every
closed lead back into the working view, and a lead closed the week it was
posted never leaves it at all. Disposing of something should remove it from the
active view because it was disposed of.

The state filter remains the way back in, so nothing becomes unreachable.

**Revised after review (Codex P2 on PR #86).** That last sentence was false as
first written. Selecting Closed cleared the disposed filter but not the
freshness filter, and since disposed states are absent from
`COMMITTED_STATES`, every disposed lead older than the 48h window was still
dropped: picking Closed surfaced only the last two days of closures, and the
138-day-old Vertex Analytics row that motivated this whole change would not have
appeared. Hiding rows is only defensible if the way back actually works, so
`freshOnlyApplies(freshOnly, stateFilter)` suspends freshness whenever a
disposed state is explicitly selected, and the checkbox disables itself there
rather than sitting checked and doing nothing. The exemption is deliberately
narrow: selecting `discovered` keeps freshness, because that selection is the
discovery firehose the filter exists to triage.

**Revised again after re-review (Codex P2 on PR #86).** Hiding disposed rows on
the client left them competing for the server row budget first.
`applyRowLimit` exempts only `COMMITTED_STATES`, so disposed rows spend the
same slots as live discoveries and were then discarded on arrival. Measured on
the live database: 199 skipped and 4 closed out of 483 rows against a 250-row
budget, so roughly 40% of the response was thrown away and lower-ranked active
leads were pushed out of it with nothing on screen to explain the gap.

`withoutUnrequestedDisposed` drops them before the limit, and only when the
caller has not named a disposed state. That second half is the load-bearing
one: a blanket server-side exclusion would have re-broken the escape hatch in a
worse way, because no client-side filter can recover a row the server never
sent. The jobs page therefore sends its state filter to the query and refetches
when it changes, guarded by a sequence ref. `counts` and `applyNext` come from
separate unfiltered reads in `getJobsDashboard`, so the status cards and the
apply-next buckets are unaffected by the added parameter.

The general lesson, worth carrying: a client-side visibility rule laid over a
server-side row budget is not free. Whatever the client hides, the server is
still paying for.

## Out of scope, worth naming

`applied` is not in `COMMITTED_STATES`. An applied lead on an old posting is
therefore hidden by the default "Fresh only" filter, which is the opposite of
what that filter is for: an application in flight is the most committed thing
in the pipeline. This is a pre-existing condition in both repos and it is not
fixed here, because changing that set changes the freshness contract on three
surfaces (`bin/jh list`, the MCP `job_list`, the lead inbox UI) that are required
to agree. It deserves its own change.

Also deferred, and previously designed in
`lead-inbox/docs/job-hound-controlled-actions-design.md`: a Draft package action.
Drafting spends LLM money and writes files, and it has to run
`gate.require_pass()`, so it is a different kind of button from these.

## Testing

job-hound, in `test_jobapi_state.py`:

- a reason with `state=closed` writes `close_reason` and leaves `skip_reason`
  alone
- a reason with `state=skipped` writes `skip_reason`
- a reason with `state=applied` is a 400 and writes nothing at all, including
  no state change
- `note` still reaches `state_log` when `reason` is also present, and the two
  are independent
- a reason on a transition the state machine refuses (a 409) writes no reason,
  which is what putting `set_fields` after `set_state` buys

lead-inbox:

- the state route forwards `reason` and rejects a non-string one the way it
  rejects a non-string `note`
- the panel excludes `drafted` from a transition list that contains it
- Confirm is disabled until the reason is non-empty, for both close and skip
- a failed write leaves the displayed state unchanged and shows the upstream
  message
- the pipeline table hides closed and skipped with "Fresh only" off, and shows
  them when the state filter selects them

## Hard rules this touches

None of them. Nothing here submits an application, fills a form, or logs into a
job site. Every write goes through an audited `jobdb.py` setter via the
existing write API, `jobdb.py` stays the only writer, and the state machine
stays in one language. No new bypass of the Fit Gate: this panel cannot reach
`drafted`, and `generate()` still owns `require_pass()`.

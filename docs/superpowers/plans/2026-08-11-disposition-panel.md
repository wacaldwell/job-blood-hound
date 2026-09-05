# Disposition Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the operator record a lifecycle outcome (a rejection, a skip, an advance) from the lead inbox UI job detail page instead of dropping to `bin/jh` on the tools host.

**Architecture:** `jobapi.py`'s existing `POST /jobs/{ident}/state` gains a `reason` field so the API path populates `close_reason` and `skip_reason` the way `cmd_close` and `cmd_skip` already do with a second `set_fields` call. lead-inbox passes that field through its route proxy, and a new `DispositionPanel` on `app/jobs/[slug]/page.tsx` renders whatever `GET /transitions` returns (minus `drafted`) with an inline confirm step on the two terminal actions. All branching lives in a pure `lib/job-disposition.ts` so it is testable under vitest's node environment.

**Tech Stack:** Python 3 / FastAPI / pydantic / pytest (job-hound), Next.js App Router / React / TypeScript / vitest / `renderToStaticMarkup` (lead-inbox).

**Spec:** `docs/superpowers/specs/2026-08-11-disposition-panel-design.md`

## Global Constraints

- **No em dashes, ever.** Commas, parentheses, or separate sentences. Applies to code comments, test names, commit messages, and UI copy.
- **`jobdb.py` stays the only writer.** Every write goes through an audited `jobdb.py` setter reached via `jobapi.py`. No SQL from TypeScript, no new spool.
- **No submit, fill, or login.** Nothing in this change may submit an application, fill an external form, or log into a job site.
- **`drafted` is never a target.** That state asserts generated documents exist on disk. Only `job_generate` may assert it.
- **Commit style:** `[Component]: Brief description`.
- **Branches:** job-hound work goes on `feature/disposition-panel` (already created, holds the spec). lead-inbox work goes on a `feature/disposition-panel` branch cut from an up-to-date `origin/main`. Both repos are protected on `main` and land through PRs with a passing `tests` check.
- **Deploy order:** job-hound ships first. pydantic ignores unknown fields by default, so an lead-inbox that sends `reason` to an un-upgraded `jobapi.py` drops it silently instead of erroring, which would look like the feature working while writing nothing.

## File Structure

**job-hound**
- Modify `jobapi.py`: `StateIn` gains `reason`, a new `REASON_COLUMN` map, and two added lines in `post_state`.
- Modify `test_jobapi_state.py`: append a reason block. Existing fixtures (`client`, `slug`) are reused as-is.

**lead-inbox**
- Modify `lib/job-api.ts`: `postState` opts gain `reason`.
- Modify `app/api/jobs/[slug]/state/route.ts`: parse and forward `reason`.
- Create `lib/job-disposition.ts`: pure, client-safe, React-free disposition rules. All branching the panel does.
- Create `components/DispositionPanel.tsx`: presentational. Takes `next` as a prop so it renders under `renderToStaticMarkup`.
- Modify `app/jobs/[slug]/page.tsx`: fetch transitions, render the panel, re-fetch detail on a successful write.
- Modify `lib/job-sort.ts`: add `DISPOSED_STATES` and `passesDisposedFilter` beside `COMMITTED_STATES`.
- Modify `app/jobs/page.tsx`: one added `.filter()` in the `displayedJobs` chain.
- Create `tests/job-disposition.test.ts`, `tests/disposition-panel.test.ts`. Append to `tests/job-route-body.test.ts` and `tests/job-sort.test.ts`.

---

### Task 1: `reason` on the write API

**Files:**
- Modify: `jobapi.py:115-147` (`StateIn` and `post_state`)
- Test: `test_jobapi_state.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `POST /jobs/{ident}/state` accepting `{state, note, outcome, reason}`. A `reason` with `state` in `{closed, skipped}` writes `close_reason` / `skip_reason`. A `reason` with any other state is a 400. Response shape is unchanged (`_payload`, which returns neither `outcome` nor the reason columns).

- [ ] **Step 1: Write the failing tests**

Append to `test_jobapi_state.py`:

```python
def _row(tmp_path, slug):
    """Read the row straight from the DB the client fixture is pointed at.

    `tmp_path` is per-test, so this opens the same file the TestClient wrote.
    The reason columns are deliberately absent from the endpoint's payload, so
    there is nothing to assert against in the response body.
    """
    db = jobdb.JobDB(tmp_path / "jobs.db")
    try:
        return db.resolve(slug)
    finally:
        db.close()


def _walk_to_applied(client, slug):
    for state in ("queued", "drafted", "ready", "applied"):
        r = client.post(f"/jobs/{slug}/state", json={"state": state},
                        headers=AUTH)
        assert r.status_code == 200, r.text


def test_a_reason_on_close_writes_close_reason(client, slug, tmp_path):
    """cmd_close writes this column with a second set_fields call. The API path
    has to write the same one, or a lead closed from the dashboard renders a
    blank close reason on the detail page forever."""
    _walk_to_applied(client, slug)
    r = client.post(f"/jobs/{slug}/state",
                    json={"state": "closed", "outcome": "rejected",
                          "reason": "Rejected at decision stage."},
                    headers=AUTH)
    assert r.status_code == 200
    row = _row(tmp_path, slug)
    assert row["state"] == "closed"
    assert row["outcome"] == "rejected"
    assert row["close_reason"] == "Rejected at decision stage."
    assert row["skip_reason"] is None


def test_a_reason_on_skip_writes_skip_reason(client, slug, tmp_path):
    r = client.post(f"/jobs/{slug}/state",
                    json={"state": "skipped", "reason": "Deep Kubernetes."},
                    headers=AUTH)
    assert r.status_code == 200
    row = _row(tmp_path, slug)
    assert row["skip_reason"] == "Deep Kubernetes."
    assert row["close_reason"] is None


def test_a_reason_on_any_other_state_is_400(client, slug, tmp_path):
    """There is no structured column for it, and silently dropping a string the
    operator typed into a mandatory field is worse than refusing it. `note` is
    the audited free-text field and works for every state."""
    r = client.post(f"/jobs/{slug}/state",
                    json={"state": "queued", "reason": "looks good"},
                    headers=AUTH)
    assert r.status_code == 400
    assert "reason is only accepted for" in r.json()["detail"]
    assert _row(tmp_path, slug)["state"] == "discovered"


def test_a_refused_transition_writes_no_reason(client, slug, tmp_path):
    """set_fields runs after set_state, so a 409 leaves the reason columns
    untouched instead of stamping a reason onto a state that never happened."""
    r = client.post(f"/jobs/{slug}/state",
                    json={"state": "closed", "outcome": "rejected",
                          "reason": "should not land"},
                    headers=AUTH)
    assert r.status_code == 409
    row = _row(tmp_path, slug)
    assert row["state"] == "discovered"
    assert row["close_reason"] is None


def test_note_and_reason_are_independent(client, slug, tmp_path):
    _walk_to_applied(client, slug)
    client.post(f"/jobs/{slug}/state",
                json={"state": "closed", "outcome": "ghosted",
                      "note": "audit line", "reason": "structured line"},
                headers=AUTH)
    row = _row(tmp_path, slug)
    assert row["close_reason"] == "structured line"
    db = jobdb.JobDB(tmp_path / "jobs.db")
    try:
        notes = [e["note"] for e in db.history(row["uid"])]
    finally:
        db.close()
    assert "audit line" in notes


def test_a_state_write_without_a_reason_still_works(client, slug, tmp_path):
    """The field is optional. Every existing caller omits it."""
    r = client.post(f"/jobs/{slug}/state", json={"state": "queued"},
                    headers=AUTH)
    assert r.status_code == 200
    assert _row(tmp_path, slug)["skip_reason"] is None
```

- [ ] **Step 2: Confirm `db.history` is the right accessor**

Run: `grep -n "def history" jobdb.py`
If the method has another name, fix `test_note_and_reason_are_independent` to use the real one. Everything else in this task is independent of it.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `source .venv/bin/activate && python -m pytest test_jobapi_state.py -v`
Expected: the six new tests FAIL. `test_a_reason_on_close_writes_close_reason` fails on `close_reason is None` (pydantic drops the unknown `reason` field silently), and `test_a_reason_on_any_other_state_is_400` fails with 200 instead of 400.

- [ ] **Step 4: Add the field and the map**

In `jobapi.py`, replace the `StateIn` class:

```python
class StateIn(BaseModel):
    state: str
    note: str | None = None
    outcome: str | None = None
    reason: str | None = None


# The column a structured reason belongs in, per state. cmd_close and cmd_skip
# in job_cli.py write these with a second set_fields call after the transition,
# and this endpoint has to write the same ones: two write paths that disagree
# about which columns they populate are a split brain even when they share a
# database. Every other state has no such column, and `note` is the audited
# free-text field that covers all of them.
REASON_COLUMN = {"closed": "close_reason", "skipped": "skip_reason"}
```

- [ ] **Step 5: Enforce the 400 and write the column**

In `post_state`, immediately after the existing `STATES` check, add:

```python
    if body.reason and body.state not in REASON_COLUMN:
        raise HTTPException(
            400,
            f"reason is only accepted for {sorted(REASON_COLUMN)}, not "
            f"'{body.state}'. Use note, which is audited for every state.")
```

Then, between the existing `set_state` try/except block and the closing `return _payload(db.set_read(...))`, add:

```python
    # After set_state, never before: a refused transition must leave the reason
    # columns untouched rather than stamp a reason onto a state that did not
    # happen. set_fields is safe here because close_reason and skip_reason are
    # not gate, audited, or interview columns.
    if body.reason:
        db.set_fields(updated["uid"], **{REASON_COLUMN[body.state]: body.reason})
```

- [ ] **Step 6: Run the full suite**

Run: `source .venv/bin/activate && python -m pytest test_jobapi_state.py test_jobapi.py test_lead_inbox_setters.py -v`
Expected: all PASS, including the pre-existing tests. The 409 test and the read-marking test must still pass unchanged.

- [ ] **Step 7: Run the whole repo suite**

Run: `source .venv/bin/activate && python -m pytest -q`
Expected: no new failures against the baseline.

- [ ] **Step 8: Commit**

```bash
git add jobapi.py test_jobapi_state.py
git commit -m "[jobapi]: accept a structured reason on the state endpoint"
```

---

### Task 2: Pass `reason` through the lead-inbox proxy

**Files:**
- Modify: `lib/job-api.ts` (`postState`)
- Modify: `app/api/jobs/[slug]/state/route.ts`
- Test: `tests/job-route-body.test.ts`

**Interfaces:**
- Consumes: the Task 1 endpoint contract.
- Produces: `postState(slug, state, {note?, outcome?, reason?})`. The route accepts `reason` in the JSON body, forwards it only when it is a string, and otherwise omits it.

Cut the branch first:

```bash
cd ~/code/repos/lead-inbox
git checkout main && git pull --ff-only origin main
git checkout -b feature/disposition-panel
```

- [ ] **Step 1: Write the failing test**

Append to `tests/job-route-body.test.ts`:

```ts
describe('the state route forwards reason', () => {
  it('passes a string reason through to postState', async () => {
    const { postState } = await import('@/lib/job-api')
    vi.mocked(postState).mockResolvedValue({} as never)
    await postStateRoute(
      request(JSON.stringify({ state: 'closed', outcome: 'rejected', reason: 'Rejected at decision stage.' })),
      context,
    )
    expect(postState).toHaveBeenCalledWith(SLUG, 'closed', {
      note: undefined,
      outcome: 'rejected',
      reason: 'Rejected at decision stage.',
    })
  })

  it('drops a non-string reason instead of forwarding it', async () => {
    const { postState } = await import('@/lib/job-api')
    vi.mocked(postState).mockResolvedValue({} as never)
    vi.mocked(postState).mockClear()
    await postStateRoute(request(JSON.stringify({ state: 'skipped', reason: 42 })), context)
    expect(postState).toHaveBeenCalledWith(SLUG, 'skipped', {
      note: undefined,
      outcome: undefined,
      reason: undefined,
    })
  })
})
```

- [ ] **Step 2: Run it to verify it fails**

Run: `npx vitest run tests/job-route-body.test.ts`
Expected: FAIL. `postState` is called with an object that has no `reason` key.

- [ ] **Step 3: Add `reason` to the client**

In `lib/job-api.ts`, replace `postState`:

```ts
export function postState(
  slug: string,
  state: string,
  opts: { note?: string; outcome?: string; reason?: string } = {},
) {
  return callJobApi<JobWriteResult>(`/jobs/${enc(slug)}/state`, {
    method: 'POST',
    body: {
      state,
      note: opts.note ?? null,
      outcome: opts.outcome ?? null,
      reason: opts.reason ?? null,
    },
  })
}
```

- [ ] **Step 4: Add `reason` to the route**

In `app/api/jobs/[slug]/state/route.ts`, widen the body cast and the call. Replace the `const body =` assignment and the `postState` call:

```ts
  const body =
    parsed && typeof parsed === 'object'
      ? (parsed as { state?: unknown; note?: unknown; outcome?: unknown; reason?: unknown })
      : {}
```

```ts
      await postState(decodedSlug, body.state, {
        note: typeof body.note === 'string' ? body.note : undefined,
        outcome: typeof body.outcome === 'string' ? body.outcome : undefined,
        reason: typeof body.reason === 'string' ? body.reason : undefined,
      }),
```

- [ ] **Step 5: Run the tests**

Run: `npx vitest run tests/job-route-body.test.ts tests/job-api.test.ts`
Expected: PASS, including the pre-existing null-body and slug-validation tests.

- [ ] **Step 6: Commit**

```bash
git add lib/job-api.ts app/api/jobs/\[slug\]/state/route.ts tests/job-route-body.test.ts
git commit -m "[jobs]: forward a structured reason on state writes"
```

---

### Task 3: Pure disposition rules

**Files:**
- Create: `lib/job-disposition.ts`
- Test: `tests/job-disposition.test.ts`

**Interfaces:**
- Consumes: nothing.
- Produces: `dispositionTargets(next: string[]): string[]`, `labelFor(state: string): string`, `needsReason(state: string): boolean`, `canConfirm(state: string, reason: string): boolean`, `DISPOSITION_LABELS: Record<string, string>`, `TERMINAL_STATES: Set<string>`, `OUTCOMES: string[]`. Task 4 imports all of these.

- [ ] **Step 1: Write the failing test**

Create `tests/job-disposition.test.ts`:

```ts
import { describe, expect, it } from 'vitest'
import {
  OUTCOMES,
  canConfirm,
  dispositionTargets,
  labelFor,
  needsReason,
} from '@/lib/job-disposition'

describe('dispositionTargets', () => {
  it('never offers drafted, because only job_generate may claim files exist', () => {
    expect(dispositionTargets(['drafted', 'skipped', 'discovered'])).toEqual(['skipped', 'discovered'])
  })

  it('offers what interviewing actually allows', () => {
    expect(dispositionTargets(['closed', 'applied'])).toEqual(['closed', 'applied'])
  })

  it('returns nothing for a terminal state', () => {
    expect(dispositionTargets([])).toEqual([])
  })

  it('drops a state it has no label for rather than rendering a raw name', () => {
    expect(dispositionTargets(['closed', 'negotiating'])).toEqual(['closed'])
  })

  it('preserves the order the endpoint returned', () => {
    expect(dispositionTargets(['skipped', 'closed'])).toEqual(['skipped', 'closed'])
  })
})

describe('labelFor', () => {
  it('labels the lifecycle states in operator language', () => {
    expect(labelFor('closed')).toBe('Close')
    expect(labelFor('applied')).toBe('Mark applied')
    expect(labelFor('discovered')).toBe('Send back')
  })

  it('falls back to the raw state rather than rendering undefined', () => {
    expect(labelFor('negotiating')).toBe('negotiating')
  })
})

describe('needsReason', () => {
  it('requires one on the two dispositions', () => {
    expect(needsReason('closed')).toBe(true)
    expect(needsReason('skipped')).toBe(true)
  })

  it('does not require one on progress', () => {
    expect(needsReason('applied')).toBe(false)
    expect(needsReason('interviewing')).toBe(false)
  })
})

describe('canConfirm', () => {
  it('blocks a terminal action until a reason is written', () => {
    expect(canConfirm('closed', '')).toBe(false)
    expect(canConfirm('closed', '   ')).toBe(false)
    expect(canConfirm('closed', 'Rejected at decision stage.')).toBe(true)
  })

  it('never blocks a forward move', () => {
    expect(canConfirm('applied', '')).toBe(true)
  })
})

describe('OUTCOMES', () => {
  it('matches jobdb.OUTCOMES exactly, in order, with rejected first', () => {
    expect(OUTCOMES).toEqual(['rejected', 'withdrawn', 'offer', 'accepted', 'ghosted', 'other'])
  })
})
```

- [ ] **Step 2: Run it to verify it fails**

Run: `npx vitest run tests/job-disposition.test.ts`
Expected: FAIL, cannot resolve `@/lib/job-disposition`.

- [ ] **Step 3: Write the module**

Create `lib/job-disposition.ts`:

```ts
/**
 * Disposition rules for the job detail page's action panel.
 *
 * Pure, client-safe, and free of React, the same shape as lib/job-sort.ts and
 * lib/job-format.ts, so every branch the panel takes can be unit tested under
 * vitest's node environment. DispositionPanel renders what these decide and
 * holds no rules of its own.
 */

// `drafted` is never offered. That state asserts generated documents exist on
// disk, and only job_generate may assert it; a button that sets it produces a
// row pointing at an empty package folder. app/jobs/inbox/page.tsx excludes it
// from triage for the same reason.
const NEVER_OFFERED = new Set(['drafted'])

// The two states that take a structured reason, matching REASON_COLUMN in
// job-hound's jobapi.py (close_reason, skip_reason). Both are dispositions
// rather than progress, so the panel demands a written reason before sending
// either one.
export const TERMINAL_STATES = new Set(['closed', 'skipped'])

// Operator language for each offerable state. A state absent from this map is
// not offered at all: a lifecycle state added in job-hound should reach this
// panel as a deliberate edit here, not as a button labelled with a raw
// database value.
export const DISPOSITION_LABELS: Record<string, string> = {
  queued: 'Queue',
  discovered: 'Send back',
  ready: 'Mark ready',
  applied: 'Mark applied',
  interviewing: 'Interviewing',
  skipped: 'Skip',
  closed: 'Close',
}

// jobdb.OUTCOMES, same values in the same order. `rejected` leads because it
// is what the picker opens on, and it is also what jobdb declares first.
export const OUTCOMES = ['rejected', 'withdrawn', 'offer', 'accepted', 'ghosted', 'other']

/**
 * The buttons to draw, given whatever GET /transitions returned. Order is the
 * endpoint's, which is jobdb's sorted TRANSITIONS set.
 */
export function dispositionTargets(next: string[]): string[] {
  return next.filter((state) => !NEVER_OFFERED.has(state) && state in DISPOSITION_LABELS)
}

export function labelFor(state: string): string {
  return DISPOSITION_LABELS[state] ?? state
}

export function needsReason(state: string): boolean {
  return TERMINAL_STATES.has(state)
}

/** Whether the confirm button is live. A whitespace-only reason is no reason. */
export function canConfirm(state: string, reason: string): boolean {
  return !needsReason(state) || reason.trim().length > 0
}
```

- [ ] **Step 4: Run the tests**

Run: `npx vitest run tests/job-disposition.test.ts`
Expected: PASS, all 14 assertions.

- [ ] **Step 5: Commit**

```bash
git add lib/job-disposition.ts tests/job-disposition.test.ts
git commit -m "[jobs]: add pure disposition rules for the detail panel"
```

---

### Task 4: The panel, wired into the detail page

**Files:**
- Create: `components/DispositionPanel.tsx`
- Modify: `app/jobs/[slug]/page.tsx`
- Test: `tests/disposition-panel.test.ts`

**Interfaces:**
- Consumes: everything Task 3 produces, plus `postState`'s route from Task 2 (called as `fetch('/api/jobs/<slug>/state')`, not the server-only client).
- Produces: `<DispositionPanel state={string} outcome={string | null} slug={string} next={string[]} onWritten={() => void} />`. `next` is a prop, not fetched internally, so the component renders under `renderToStaticMarkup`. `onWritten` is the parent's detail re-fetch.

- [ ] **Step 1: Write the failing test**

Create `tests/disposition-panel.test.ts`:

```ts
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import DispositionPanel from '@/components/DispositionPanel'

function render(props: Partial<React.ComponentProps<typeof DispositionPanel>> = {}) {
  return renderToStaticMarkup(
    React.createElement(DispositionPanel, {
      slug: 'vertexanalytics__manager-devops-engineering__6def',
      state: 'interviewing',
      outcome: null,
      next: ['applied', 'closed'],
      onWritten: () => {},
      ...props,
    }),
  )
}

describe('DispositionPanel', () => {
  it('renders a button per legal target', () => {
    const html = render()
    expect(html).toContain('Close')
    expect(html).toContain('Mark applied')
  })

  it('never renders a drafted button', () => {
    const html = render({ state: 'queued', next: ['drafted', 'skipped', 'discovered'] })
    expect(html).not.toContain('Draft')
    expect(html).toContain('Skip')
    expect(html).toContain('Send back')
  })

  it('reports the terminal state instead of buttons when nothing is legal', () => {
    const html = render({ state: 'closed', outcome: 'rejected', next: [] })
    expect(html).toContain('rejected')
    expect(html).toContain('No further transitions')
    expect(html).not.toContain('Mark applied')
  })

  it('names the stored outcome, not a hardcoded rejection', () => {
    const html = render({ state: 'closed', outcome: 'ghosted', next: [] })
    expect(html).toContain('ghosted')
    expect(html).not.toContain('rejected')
  })

  it('opens with no confirm step showing, so a stray click cannot close a lead', () => {
    const html = render()
    expect(html).not.toContain('Confirm')
    expect(html).not.toContain('Reason')
  })
})
```

- [ ] **Step 2: Run it to verify it fails**

Run: `npx vitest run tests/disposition-panel.test.ts`
Expected: FAIL, cannot resolve `@/components/DispositionPanel`.

- [ ] **Step 3: Write the component**

Create `components/DispositionPanel.tsx`:

```tsx
'use client'

import { useRef, useState } from 'react'
import {
  OUTCOMES,
  canConfirm,
  dispositionTargets,
  labelFor,
  needsReason,
} from '@/lib/job-disposition'

type Props = {
  slug: string
  state: string
  outcome: string | null
  /** Whatever GET /api/jobs/<slug>/transitions returned. A prop, not a fetch,
   *  so this component renders under renderToStaticMarkup in tests. */
  next: string[]
  /** The parent's detail re-fetch. Called after a successful write. */
  onWritten: () => void
}

export default function DispositionPanel({ slug, state, outcome, next, onWritten }: Props) {
  const [note, setNote] = useState('')
  // The target awaiting confirmation, or null when no confirm step is open.
  // Opening closed means a lead is one click from terminal, so nothing is
  // pre-selected on mount.
  const [pending, setPending] = useState<string | null>(null)
  const [chosenOutcome, setChosenOutcome] = useState(OUTCOMES[0])
  const [reason, setReason] = useState('')
  // Disables the whole bar while a write is outstanding. Two audit rows for
  // one perceived click is the failure this prevents, the same guard
  // app/jobs/inbox/page.tsx uses.
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const writeSeq = useRef(0)

  const targets = dispositionTargets(next)

  const send = async (target: string) => {
    const seq = ++writeSeq.current
    setBusy(true)
    setError(null)
    const terminal = needsReason(target)
    const body: Record<string, string> = { state: target }
    if (terminal) {
      body.reason = reason.trim()
      body.note = reason.trim()
      if (target === 'closed') body.outcome = chosenOutcome
    } else if (note.trim()) {
      body.note = note.trim()
    }
    try {
      const res = await fetch(`/api/jobs/${encodeURIComponent(slug)}/state`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const payload = await res.json().catch(() => null)
      if (!res.ok) {
        const message = (payload as { error?: string } | null)?.error
        if (writeSeq.current === seq) setError(message || `Write failed (${res.status})`)
        return
      }
      if (writeSeq.current !== seq) return
      setPending(null)
      setReason('')
      setNote('')
      // Re-read rather than patch. The write API's payload carries neither
      // outcome nor close_reason, so patching locally would render an outcome
      // this page invented from what it sent. The re-read also refreshes the
      // lifecycle history, which just gained a row.
      onWritten()
    } catch {
      if (writeSeq.current === seq) setError('Write API unreachable')
    } finally {
      if (writeSeq.current === seq) setBusy(false)
    }
  }

  if (targets.length === 0) {
    return (
      <Panel>
        <p className="text-xs text-[#aeb8cc] font-mono leading-relaxed">
          {state}{outcome ? ` (${outcome})` : ''}. No further transitions.
        </p>
      </Panel>
    )
  }

  return (
    <Panel>
      <div className="flex flex-wrap items-center gap-2">
        {targets.map((target) => (
          <button
            key={target}
            disabled={busy}
            onClick={() => (needsReason(target) ? setPending(target) : send(target))}
            className={`text-sm px-3 py-1 rounded border font-mono disabled:opacity-40 ${
              needsReason(target)
                ? 'text-[#ff5555] border-[#ff5555]/30 hover:border-[#ff5555]/60'
                : 'text-[#8be9fd] border-[#8be9fd]/30 hover:border-[#8be9fd]/60'
            }`}
          >
            {labelFor(target)}
          </button>
        ))}
      </div>

      {pending === null && (
        <textarea
          aria-label="Transition note"
          value={note}
          onChange={(event) => setNote(event.target.value)}
          placeholder="Optional note, recorded in the lifecycle history"
          rows={2}
          className="w-full rounded border border-[#8be9fd]/10 bg-[#0d0e14] px-3 py-2 text-xs text-[#f8f8f2] font-mono focus:border-[#8be9fd]/40"
        />
      )}

      {pending !== null && (
        <div className="space-y-2 rounded border border-[#ff5555]/20 bg-[#ff5555]/5 p-3">
          <div className="text-xs text-[#ff5555] font-mono uppercase tracking-widest">
            {labelFor(pending)} this lead
          </div>
          {pending === 'closed' && (
            <label className="block text-xs text-[#aeb8cc] font-mono">
              Outcome
              <select
                value={chosenOutcome}
                onChange={(event) => setChosenOutcome(event.target.value)}
                className="mt-1 w-full rounded border border-[#44475a]/40 bg-[#0d0e14] px-2 py-1.5 text-xs text-[#f8f8f2] font-mono"
              >
                {OUTCOMES.map((value) => <option key={value} value={value}>{value}</option>)}
              </select>
            </label>
          )}
          <label className="block text-xs text-[#aeb8cc] font-mono">
            Reason (required)
            <textarea
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder="Why. Written to the row and to the lifecycle history."
              rows={2}
              className="mt-1 w-full rounded border border-[#ff5555]/20 bg-[#0d0e14] px-3 py-2 text-xs text-[#f8f8f2] font-mono focus:border-[#ff5555]/50"
            />
          </label>
          <div className="flex items-center gap-2">
            <button
              disabled={busy || !canConfirm(pending, reason)}
              onClick={() => send(pending)}
              className="text-xs px-2 py-1 rounded border font-mono text-[#ff5555] border-[#ff5555]/40 hover:border-[#ff5555]/70 disabled:opacity-40"
            >
              Confirm
            </button>
            <button
              disabled={busy}
              onClick={() => { setPending(null); setReason('') }}
              className="text-xs px-2 py-1 rounded border font-mono text-[#aeb8cc] border-[#44475a]/40 disabled:opacity-40"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {error && <p className="text-xs text-[#ffb86c] font-mono leading-relaxed">{error}</p>}

      <p className="text-xs text-[#8b8fa8] font-mono leading-relaxed">
        Writes go through job-hound&apos;s audited state machine. Drafting stays in the CLI,
        because generated documents have to exist before a lead can claim them.
      </p>
    </Panel>
  )
}

function Panel({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-[#50fa7b]/10 bg-[#1a1b26] overflow-hidden">
      <div className="px-4 py-3 border-b border-[#50fa7b]/10 text-sm font-bold text-[#50fa7b] font-mono uppercase tracking-widest">
        Disposition
      </div>
      <div className="p-4 space-y-3">{children}</div>
    </div>
  )
}
```

- [ ] **Step 4: Run the tests**

Run: `npx vitest run tests/disposition-panel.test.ts`
Expected: PASS, all five cases.

- [ ] **Step 5: Wire it into the detail page**

In `app/jobs/[slug]/page.tsx`:

Add the import beside the existing ones:

```tsx
import DispositionPanel from '@/components/DispositionPanel'
```

Add two state hooks next to the existing ones (after `const [errorAsOf, setErrorAsOf] = useState<string | null>(null)`):

```tsx
  const [next, setNext] = useState<string[]>([])
  // Bumped after a successful disposition write. Both the detail fetch and the
  // transitions fetch depend on it, so one write re-reads both: the row's new
  // outcome and reason, and the button set the new state allows.
  const [reloads, setReloads] = useState(0)
```

Change the existing detail `useEffect` dependency array from `[slug]` to `[slug, reloads]`, and add a second effect directly after it:

```tsx
  useEffect(() => {
    if (!slug) return
    fetchJson<{ state: string; next: string[] }>(`/api/jobs/${encodeURIComponent(slug)}/transitions`)
      .then((data) => setNext(data.next))
      // A failed transitions read is not a failed page. The panel simply
      // offers nothing, and the rest of the detail still renders.
      .catch(() => setNext([]))
  }, [slug, reloads])
```

Render the panel above the existing `VotePanel` in the left column:

```tsx
          <DispositionPanel
            slug={job.slug}
            state={job.state}
            outcome={job.outcome}
            next={next}
            onWritten={() => setReloads((n) => n + 1)}
          />
```

- [ ] **Step 6: Typecheck and lint**

Run: `npx tsc --noEmit && npx eslint components/DispositionPanel.tsx lib/job-disposition.ts 'app/jobs/[slug]/page.tsx'`
Expected: clean. If eslint objects to a `setState` call inside the detail effect, note that this plan adds none: the new effect only calls `setNext`, matching the existing pattern in the same file.

- [ ] **Step 7: Run the full lead-inbox suite**

Run: `npx vitest run`
Expected: no new failures against the baseline. Capture the baseline first with a `git stash` run if any pre-existing test is already red.

- [ ] **Step 8: Commit**

```bash
git add components/DispositionPanel.tsx tests/disposition-panel.test.ts 'app/jobs/[slug]/page.tsx'
git commit -m "[jobs]: add a disposition panel to the job detail page"
```

---

### Task 5: Disposed rows leave the default table view

**Files:**
- Modify: `lib/job-sort.ts` (add beside `COMMITTED_STATES`)
- Modify: `app/jobs/page.tsx:92-106` (the `displayedJobs` filter chain)
- Test: `tests/job-sort.test.ts`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `DISPOSED_STATES: Set<string>` and `passesDisposedFilter(job: JobListItem, stateFilter: string): boolean`, exported from `lib/job-sort.ts`.

- [ ] **Step 1: Write the failing test**

Append to `tests/job-sort.test.ts`. Reuse whatever job factory that file already has; if it builds rows inline, follow suit with a minimal object cast:

```ts
describe('passesDisposedFilter', () => {
  const row = (state: string) => ({ state } as JobListItem)

  it('hides closed and skipped from the default all-states view', () => {
    expect(passesDisposedFilter(row('closed'), 'all')).toBe(false)
    expect(passesDisposedFilter(row('skipped'), 'all')).toBe(false)
  })

  it('keeps everything else in the default view', () => {
    for (const state of ['discovered', 'queued', 'drafted', 'ready', 'applied', 'interviewing']) {
      expect(passesDisposedFilter(row(state), 'all')).toBe(true)
    }
  })

  it('shows them when the state filter asks for them by name', () => {
    expect(passesDisposedFilter(row('closed'), 'closed')).toBe(true)
    expect(passesDisposedFilter(row('skipped'), 'skipped')).toBe(true)
  })

  it('is independent of the freshness filter, so a lead closed today also leaves', () => {
    // The point of the rule: today an old closed lead vanishes only because
    // COMMITTED_STATES omits closed and the freshness filter catches it as a
    // stale discovery. A freshly closed one never left at all.
    expect(passesDisposedFilter(row('closed'), 'all')).toBe(false)
  })
})
```

Add `passesDisposedFilter` to that file's existing import from `@/lib/job-sort`.

- [ ] **Step 2: Run it to verify it fails**

Run: `npx vitest run tests/job-sort.test.ts`
Expected: FAIL, `passesDisposedFilter` is not exported.

- [ ] **Step 3: Add the rule**

In `lib/job-sort.ts`, directly after the `COMMITTED_STATES` declaration:

```ts
// Rows that have been disposed of. They leave the default table view because
// they were disposed of, not as a side effect of the freshness filter. Neither
// closed nor skipped is in COMMITTED_STATES, so today an old closed lead
// vanishes by luck (it reads as a stale discovery) and one closed the week it
// was posted never leaves at all. The state filter is the way back in, so
// nothing here becomes unreachable.
export const DISPOSED_STATES = new Set(['closed', 'skipped'])

/**
 * The table's disposed filter. Only applies to the default 'all' view: asking
 * for closed or skipped by name is an explicit request to see them.
 */
export function passesDisposedFilter(job: JobListItem, stateFilter: string): boolean {
  if (stateFilter !== 'all') return true
  return !DISPOSED_STATES.has(job.state)
}
```

- [ ] **Step 4: Apply it on the page**

In `app/jobs/page.tsx`, add `passesDisposedFilter` to the existing `@/lib/job-sort` import, then add one line to the `displayedJobs` chain, immediately after the `stateFilter` line:

```ts
      .filter((job) => stateFilter === 'all' || job.state === stateFilter)
      .filter((job) => passesDisposedFilter(job, stateFilter))
```

- [ ] **Step 5: Label the filter honestly in the UI**

The state filter `<select>` already lists Closed and Skipped, which is now the only way to see them. Change its `all` option label so the default view does not silently claim to show everything. In the `<option value="all">` element:

```tsx
          <option value="all">All active states</option>
```

- [ ] **Step 6: Run the tests and typecheck**

Run: `npx vitest run tests/job-sort.test.ts && npx tsc --noEmit`
Expected: PASS and clean.

- [ ] **Step 7: Run the full suite**

Run: `npx vitest run`
Expected: no new failures. Watch `tests/job-table-responsive.test.ts` and any test asserting on the full row count of the jobs page, since the visible set just shrank.

- [ ] **Step 8: Commit**

```bash
git add lib/job-sort.ts app/jobs/page.tsx tests/job-sort.test.ts
git commit -m "[jobs]: hide disposed leads from the default pipeline view"
```

---

### Task 6: Ship it

**Files:** none. This task is verification and PRs.

- [ ] **Step 1: Open the job-hound PR**

```bash
cd ~/code/job-hound
git push -u origin feature/disposition-panel
gh pr create --base main --title "Accept a structured reason on the state endpoint" --body "$(cat <<'EOF'
Adds `reason` to `POST /jobs/{ident}/state` so the API write path populates
`close_reason` and `skip_reason` the way `cmd_close` and `cmd_skip` already do.
A reason on any other state is a 400, because there is no column for it and
`note` already covers every state.

Unblocks the disposition panel in lead-inbox, which is a separate PR. Ship this
one first: pydantic ignores unknown fields, so an lead-inbox that sends `reason`
to an un-upgraded API drops it silently.

Design: docs/superpowers/specs/2026-08-11-disposition-panel-design.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01E6rusBubE5i1HmGVsiThKf
EOF
)"
```

- [ ] **Step 2: Wait for the `tests` check, then merge**

Run: `gh pr checks --watch` then merge once green. Merging deploys job-hound to the tools host automatically through the self-hosted runner, which restarts `job-api.service` because a `.py` file changed.

- [ ] **Step 3: Verify the deployed endpoint rejects a bad reason**

Run on the host, against a lead that is safe to leave untouched (the 400 path writes nothing):

```bash
ssh $JOB_HOST 'source ~/.config/job-hound/job-hound.env 2>/dev/null; curl -s -o /dev/null -w "%{http_code}\n" \
  -X POST "http://127.0.0.1:${JOB_API_PORT:-8765}/jobs/vertexanalytics__manager-devops-engineering__6def/state" \
  -H "Authorization: Bearer $JOB_API_TOKEN" -H "Content-Type: application/json" \
  -d "{\"state\":\"queued\",\"reason\":\"probe\"}"'
```

Expected: `400`. A `422` means the deployed service is still the old one (it would have accepted and ignored `reason`, then failed the transition with a 409 instead). Do not proceed to step 4 until this returns 400.

- [ ] **Step 4: Open the lead-inbox PR**

```bash
cd ~/code/repos/lead-inbox
git push -u origin feature/disposition-panel
gh pr create --base main --title "Add a disposition panel to the job detail page" --body "$(cat <<'EOF'
Records an outcome from `/jobs/<slug>` instead of from `bin/jh` on the host.
The panel renders whatever `GET /transitions` returns, minus `drafted`, with a
mandatory reason and an outcome picker on the two terminal actions. All
branching lives in `lib/job-disposition.ts` so it is unit tested rather than
asserted against markup.

Also makes a disposed lead leave the default pipeline view by rule instead of
by accident: `closed` and `skipped` are absent from `COMMITTED_STATES`, so
today an old closed lead vanishes only because the freshness filter reads it
as a stale discovery.

Requires the job-hound `reason` field, already merged and deployed.

Design: job-hound docs/superpowers/specs/2026-08-11-disposition-panel-design.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01E6rusBubE5i1HmGVsiThKf
EOF
)"
```

- [ ] **Step 5: Merge and verify in the browser**

After the check passes and the runner deploys, open
`http://example-host:31337/jobs/` and confirm:
- the Vertex Analytics row is absent from the default view, and appears when the state filter is set to Closed
- its detail page shows "closed (rejected). No further transitions." with the close reason rendered in the Fit and rationale panel
- a lead in `applied` shows Close and Interviewing buttons, and Close will not confirm with an empty reason

- [ ] **Step 6: Delete the merged branches**

```bash
cd ~/code/job-hound && git checkout main && git pull --ff-only origin main && git branch -d feature/disposition-panel && git fetch --prune
cd ~/code/repos/lead-inbox && git checkout main && git pull --ff-only origin main && git branch -d feature/disposition-panel && git fetch --prune
```

---

## Self-Review

**Spec coverage.** Section 1 (`reason` on the write API) is Task 1, including the 400 and the ordering. Section 2 (pass-through) is Task 2. Section 3 (the panel: filtered buttons, inline confirm, outcome picker, busy lock, re-fetch on success, empty-`next` terminal line, verbatim errors) is Tasks 3 and 4. Section 4 (disposed rows leave the default view) is Task 5. Every test bullet in the spec's Testing section maps to a named test. The spec's out-of-scope items (`applied` missing from `COMMITTED_STATES`, a Draft action) get no tasks, correctly.

**Deviation from the spec, recorded here.** The spec described one `DispositionPanel` fetching its own transitions. This plan splits it into `lib/job-disposition.ts` (pure rules) plus a component that takes `next` as a prop, and moves the transitions fetch up to the page. Reason: lead-inbox's vitest runs `environment: 'node'` and its component tests use `renderToStaticMarkup`, which runs no effects and dispatches no events. A self-fetching component would be untestable, and the spec's requirement that the panel exclude `drafted` would have no test. This matches how `lib/job-sort.ts` and `lib/job-format.ts` are already factored in that repo.

**Placeholder scan.** No TBD, TODO, "add error handling", or "similar to Task N". Every code step carries the actual code. Two steps are deliberately conditional on a fact to check first (Task 1 Step 2 on `db.history`'s real name, Task 5 Step 1 on the existing job factory in `job-sort.test.ts`), each with a stated fallback.

**Type consistency.** `dispositionTargets`, `labelFor`, `needsReason`, `canConfirm`, `OUTCOMES`, `TERMINAL_STATES`, and `DISPOSITION_LABELS` are defined in Task 3 and used under those exact names in Task 4. `DispositionPanel`'s props (`slug`, `state`, `outcome`, `next`, `onWritten`) are identical in the Task 4 test, the component, and the page wiring. `REASON_COLUMN` is Python-side only and is referenced by name in the Task 3 comment as documentation, not as an import.

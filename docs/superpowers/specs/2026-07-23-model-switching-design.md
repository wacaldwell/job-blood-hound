# Model switching design

Date: 2026-07-23
Status: approved, not yet implemented

## Why

job-hound makes three LLM calls, all hardcoded to the Anthropic Messages API:
the Fit Gate extraction (`gate.py`), the fit-score verdict (`fit.py`), and
drafting (`job_generate.py`). Each has its own private `_call_anthropic()` that
hardwires `https://api.anthropic.com/v1/messages`, the `x-api-key` header, and
reads `JOB_MODEL` (default `claude-opus-4-8`).

The single-provider assumption has a concrete, recurring cost: the tools-host
Anthropic key keeps hitting billing failures (400 credit errors on 2026-07-08,
07-10, 07-21, and again 07-23). When that happens the gate, the fit verdict, the
digest, the MCP path, and drafting all stall at once. There is no fallback.

Primary goal: a **manually selectable** second provider so evaluations can keep
running when Anthropic is down. Kimi K2 (Moonshot) is the first target because it
exposes an **Anthropic-compatible** `/v1/messages` endpoint, so switching is a
base-URL + key + model-name change, not a new request shape.

Secondary goal: confirm the fallback is "good enough" by re-running already-done
evaluations through the alternate model and comparing the decisions.

## Scope

In scope:
- A shared, provider-configurable LLM call path (`llm.py`), switchable by env.
- Provenance: record which model produced each gate result.
- A read-only comparison bench that re-evaluates gated jobs with other models and
  reports decision agreement.
- Project tracking on the chat agent kanban (tracking-only) with Discord updates.

Out of scope (explicitly not building now):
- Automatic failover. The switch is manual (`JOB_PROVIDER=kimi`). The Fit Gate is
  safety-critical and must never change models without the operator choosing to.
- An OpenAI/OpenRouter request-shape adapter. Moonshot speaks the Anthropic shape;
  a second shape is YAGNI until a non-Anthropic-compatible provider is needed.
- Autonomous agent execution of the work. The kanban is a tracking board; code
  lands through the normal feature-branch → PR → tests → review flow.
- A gold-standard answer key. "Good enough" is judged by decision agreement plus
  the operator's adjudication, matching how the gate already treats him as final gauge.

## Architecture

### A. Provider switch: `llm.py`

New module `llm.py`, the single place that knows how to reach a provider.

- `resolve_provider()` reads `JOB_PROVIDER` (default `anthropic`) and returns a
  small record: `{base_url, api_key, model, version_header}`.
  - Registry:
    - `anthropic` → base_url `https://api.anthropic.com`, key from
      `ANTHROPIC_API_KEY`, model `claude-opus-4-8`, header `anthropic-version:
      2023-06-01`.
    - `kimi` → base_url `https://api.moonshot.ai/anthropic`, key from
      `MOONSHOT_API_KEY`, model `kimi-k2-*` (exact id a config constant), same
      Anthropic version header.
  - Per-piece overrides (highest precedence): `JOB_MODEL`, `JOB_LLM_BASE_URL`,
    `JOB_LLM_API_KEY`. These let an operator point at any Anthropic-compatible
    endpoint without a registry entry.
- `call_messages(system, user, *, max_tokens, provider=None)` performs the one
  shared POST that all three sites already use (system + single user message),
  returns the concatenated text blocks, and preserves the existing
  `stop_reason == "max_tokens"` truncation signal.

`gate.py`, `fit.py`, and `job_generate.py` keep their thin wrappers (they differ
in `max_tokens` and in error semantics such as gate's `TruncatedResponse`) but
delegate the HTTP to `llm.call_messages`. The three copies of `_call_anthropic`
collapse to one code path.

Key resolution moves into `resolve_provider()` so switching provider switches the
key too. Backward compatibility: with `JOB_PROVIDER` unset, behavior and the
`ANTHROPIC_API_KEY` source are unchanged. The daily-cron and the lead inbox UI
ingest paths, which read `ANTHROPIC_API_KEY` directly and thread it through, keep
working; when they pass an explicit `api_key`, it is honored as the provider key
for the default provider.

### B. Provenance: which model gated a job

- Add a `gate_model` column to `jobs` (additive migration, auto-runs on `JobDB`
  open like the existing gate columns). Also stamp `"model"` and `"provider"`
  inside the persisted `gate_json` blob.
- `run_gate()` records the model it actually used.
- Backfill: every existing gated row (`gate_decision IS NOT NULL`) was evaluated
  with `claude-opus-4-8`, the only model ever run in production, so the migration
  stamps those rows with it. This is a fact, not a guess.
- `show` and `list` surface `gate_model` so the operator always knows which model
  produced a decision.

### C. Comparison bench: `bench_models.py`

A standalone, host-side script. **Read-only on job data; writes only report
files.** It must never mutate a job's live `gate_decision`/`gate_json`.

- Input: a set of already-gated jobs (default: all rows with a non-null
  `gate_decision`), or an explicit list of idents.
- For each model in a configured list (e.g. `claude-opus-4-8`, `kimi-k2`): build
  a `call_messages` bound to that provider/model and run `gate.extract` →
  `gate.enforce` → `gate.decide` (plus the location overlay) against the stored
  JD and the `master_resume.yaml` capabilities. This reuses the exact production
  decision logic; only the `extract` LLM call differs. Nothing is persisted to
  the job row.
- Output: a markdown report under the reports dir, per job, each model's
  DECISION and score side by side, with disagreements collected at the top. The
  fit verdict (`fit.verdict`) may optionally be included the same way.
- the operator reviews the disagreements and rules; that adjudication is the "good
  enough" verdict for adopting the fallback.

Because it drives `extract` directly (which already accepts a `call=` override)
rather than `run_gate`, the bench is inherently side-effect-free on the pipeline.

### D. Testing

- Unit tests inject a fake `call_messages`/`call` (no live network), matching the
  existing gate/fit test pattern.
- A new test for `resolve_provider()`: env combinations map to the correct
  base_url, key, model, and that per-piece overrides win.
- A test that the default (`JOB_PROVIDER` unset) path is unchanged.
- No live-network tests in the suite.

### E. Project tracking: the chat agent kanban (tracking-only)

The chat agent kanban on agent-host (`~/.agent/agent-cli/kanban.py`,
board home `~/.agent/kanban/`) is an autonomous execution system, but here it is
used purely as a tracking board.

- A dedicated board for this project (create new, or reuse `job-hound-dashboard`,
  decided at setup).
- One card per implementation-plan task, created in an **undispatched backlog
  status, unassigned**, so the dispatcher never spawns a worker. The dispatcher's
  pickup rule is verified before any card is created live.
- `notify-subscribe` binds the board to the Discord gateway source so every
  ticket update posts to Discord once work begins. The twice-daily kanban digest
  is available as a secondary summary.
- Cards are moved manually as PRs open and land. Code is written the normal way.

## Data flow

1. Operator selects a provider (`JOB_PROVIDER=kimi`) or leaves the default.
2. `resolve_provider()` yields base_url/key/model; `call_messages` posts to it.
3. gate/fit/generate get text back exactly as today; gate persists `gate_model`.
4. Bench (out-of-band) re-runs `extract`/`enforce`/`decide` per model on stored
   JDs and emits a comparison report; no pipeline state changes.

## Error handling

- Missing provider key: `resolve_provider()` fails clearly ("MOONSHOT_API_KEY not
  set for JOB_PROVIDER=kimi"), and the gate's existing fail-closed ERROR path
  still blocks drafting. A missing key is never a silent pass.
- Provider HTTP/billing errors surface as they do today (the recent
  gate-error-classification work names billing failures honestly); the fallback
  is chosen by the operator, not auto-triggered.
- Truncation (`stop_reason == max_tokens`) keeps its current dedicated signal.

## Prerequisites and risks

1. **Moonshot API key.** None exists on the Mac or the host. The plumbing (A/B/D)
   and the bench harness (C) can be built without it, but the bench cannot
   actually run until a `MOONSHOT_API_KEY` is provisioned.
2. **Unattended paths must keep working.** A/B must not regress the daily cron or
   the lead inbox UI ingest, both of which pass `ANTHROPIC_API_KEY` directly.
3. **Kanban side-effects.** Creating cards on a shared production host is an
   outward action; cards are created undispatched/unassigned and the dispatcher
   pickup rule is confirmed first, so nothing auto-executes.

## Definition of done: key is the only remaining step

Everything is built, tested, and deployed so that the sole outstanding action is
the operator dropping in the Moonshot key once they have it. Concretely, when the
key arrives:

1. Add `MOONSHOT_API_KEY=sk-...` to the host secrets at `~/.config/job-hound/job-hound.env`
   (the file `bin/jh` and the cron already source). No code change.
2. Run with `JOB_PROVIDER=kimi` for a manual fallback gate, or point the bench at
   the `kimi` provider, both already wired.

No other step (no edit, no deploy, no migration) should be required at
key-insertion time. Until the key exists, `JOB_PROVIDER=anthropic` (the default)
is fully functional, and every unit of A/B/C/D ships and is verifiable without a
Moonshot key (the bench validates its non-Kimi path against `claude-opus-4-8`).

## Success criteria

- `JOB_PROVIDER=kimi bin/jh gate <ident>` runs a real gate through Kimi and
  records `gate_model=kimi-k2` on the row.
- Default behavior and existing tests are unchanged with `JOB_PROVIDER` unset.
- `bench_models.py` produces a decision-agreement report over a set of gated jobs
  without altering any job row.
- The project's tasks exist as undispatched cards on the chat agent board, and ticket
  updates post to Discord.

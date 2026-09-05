# Daily discovery + feedback-primed fit-ranking

Design doc. Date: 2026-06-18. Owner: Jordan Rivers.

## Problem

The pipeline discovers and stores leads but does nothing to rank them. With 72+
leads (and noisy sources like Anduril dumping 27 hardware reqs), the actionable
roles drown. There is also no automation: a scan only happens when the operator types it,
so leads go stale between manual runs. the operator wants a daily unattended run that
keeps the list current and surfaces the most actionable roles first, with a way
for the ranking to improve over time from his own decisions.

## Goals

- Run discovery daily, unattended, on an always-on host.
- Rank leads by fit so the most actionable rise to the top and noise sinks.
- Let the ranking evolve automatically from the operator's accept/skip decisions (no
  manual retuning, no model retraining).
- Push a daily ranked digest to the operator via Discord.

## Non-goals (and hard rules preserved)

- No auto-apply, no form filling, no logins. This is discovery/triage only.
- Public ATS endpoints only; keep the polite User-Agent and request delays.
- No weight auto-tuning / ML training (premature with tiny data; YAGNI).
- Voice rule (no em dashes) continues to apply to the digest and any generated
  text.

## Decisions (locked with the operator)

1. The unattended daily run **does** make LLM verdict calls (top-N only).
2. Scheduling runs on the **always-on tools host**, not the Mac.
3. **Clean rebuild** of `jobs.db` (delete + re-scan) instead of a migration.
4. Deployment model **A**: repo + `jobs.db` + `JOB_APPS_DIR` all live on the
   tools host (single source of truth). Drafting runs on the host; packages are
   pulled to the Mac to submit by hand.
5. Daily digest is delivered to **Discord via an incoming webhook** (the cron
   job is not Claude Code, so it cannot use the Discord MCP). Webhook URL lives
   in the host environment, never committed.

## Architecture

A new **Refine** stage slots between Store and Track in the existing five-stage
pipeline. The deterministic scorer and the LLM verdict live in one new module;
the CLI gains a `refine` command and feedback flags; the scheduler is an OS-level
timer on the tools host.

### New module: `fit.py`

Pure, import-safe, mirroring the side-effect-free convention of `run_scan` and
`generate`. Two entry points:

- `score(job, profile) -> (int, str)` — deterministic, no network. Returns a
  0-100 fit score and a short `fit_reasons` string explaining what drove it.
  Signals:
  - title match to target role families (solutions architect; platform / cloud /
    SRE *lead*; engineering manager; principal) - weighted
  - remote-US match (full credit); Portland/Beaverton on-site OK; else
    penalty
  - salary floor 150-180k **only if present** in the listing; absent salary is
    neutral, never penalized
  - penalties for exclude-term hits and heavy-coding markers (the Acme Scheduling
    lesson: "codes daily", language-specific IC SRE)

- `verdict(job, profile, history, client) -> dict` — the expensive tier. Runs
  only on the top-N deterministic survivors (default N=10) or on demand at draft
  time. Fetches the full JD (reuse the fetch in `job_generate.py`), sends
  `master_resume.yaml` + JD + a decision-history few-shot block, and returns
  `{llm_fit_score, llm_rationale, llm_coding_bar}`. Reuses the existing Anthropic
  client in `job_generate.py` - no new dependency. This doubles as the automated
  "vet the JD before drafting" rule.

### New file: `profile.yaml`

Scoring weights, target role families, salary floor, acceptable locations, and
coding-bar markers, kept legible and hand-tunable. Separated from
`companies.yaml` (which is scan targets) and `master_resume.yaml` (which is
source experience).

### Feedback priming (how the ranking evolves)

The corpus is built from data the system already records:

- positive examples: jobs that reached queued / drafted / ready / applied /
  interviewing
- negative examples: jobs that were skipped or closed with a rejecting outcome

Each example contributes `{title, company, reason}`. Reasons come from new
`skip_reason` / `close_reason` fields plus `state_log`. The few-shot block is
rebuilt from this corpus on **every** verdict run, capped to the most recent ~20
examples to bound the prompt. Result: the LLM verdict mirrors the operator's judgment and
improves every time he acts, with zero retraining and a fully inspectable corpus.

### Schema changes (`jobdb.py`)

New nullable columns on `jobs`:
`fit_score, fit_reasons, llm_fit_score, llm_rationale, llm_coding_bar,
skip_reason, close_reason`. Per decision 3, the rollout is a clean rebuild
(delete `jobs.db`, re-scan), not a migration.

### CLI changes (`job_cli.py`)

- `refine [--top N] [--no-llm] [--digest]` - re-score all active leads
  (deterministic, free), run the LLM verdict on the top-N that lack a cached
  verdict, then emit a ranked digest. Composable; runnable by hand or by the
  scheduler.
- `skip <ident> --reason "..."` and `close <ident> --outcome <o> --reason "..."`
  - capture structured feedback that feeds the priming corpus.
- `list` gains fit ordering: sort by `llm_fit_score` when present, else
  `fit_score`.

### `refine` run flow

1. Load active leads (discovered, queued).
2. Compute deterministic `fit_score` + `fit_reasons` for each; store. (Free, so
   re-scored every run in case `profile.yaml` changed.)
3. Take the top-N by `fit_score` that lack a cached LLM verdict; run `verdict()`;
   store. (Cached verdicts are not re-run, so daily cost tracks only genuinely
   new top leads.)
4. Build the ranked digest (top ~10 by final ordering): title @ company, scores,
   one-line rationale, coding bar, posting age, link, plus pipeline counts.
5. If a Discord webhook URL is configured, POST the digest to it.

### Daily automation (tools host)

- The repo is cloned to the tools host; `jobs.db` and `JOB_APPS_DIR` live there.
- `ANTHROPIC_API_KEY` and `DISCORD_WEBHOOK_URL` live in the host environment
  (never committed; matches the sensitive-files rules).
- A cron entry (or systemd timer) runs `scan` then `refine --digest` once daily,
  logging to `$LOG_DIR/job-hound/` per the workspace logging convention.
- Discord delivery is a plain HTTPS POST to the incoming webhook.

## Testing (TDD)

- `fit.score`: pure function, unit-tested across the signal matrix. A
  Acme Scheduling-type heavy-coding role must score low; a Temporal Solutions Architect
  role must score high. Covers remote, on-site-NC, salary present/absent, and
  exclude-term penalties.
- few-shot builder: corpus assembly from mocked `state_log` + reason fields;
  asserts positive/negative split and the ~20 cap.
- `fit.verdict`: mock the Anthropic client; assert the prompt carries the history
  block and the JD, and that the response is parsed into the three stored fields.
- `refine` flow: mocked data end to end; asserts deterministic re-score of all,
  LLM verdict only on uncached top-N, and digest ordering.
- Per CLAUDE.md, all parsing/scoring is tested with mocked data before any
  success claim.

## Rollout

1. Land `fit.py`, `profile.yaml`, schema columns, CLI changes behind tests.
2. Delete `jobs.db`, re-scan locally, run `refine` by hand, eyeball the ranking
   and digest.
3. Deploy to the tools host: clone, set env, add the cron/timer, confirm one
   manual run posts to Discord.
4. Enable the daily schedule.

## Open questions

None blocking. Webhook setup (creating the Discord incoming webhook URL) is a
one-time manual step the operator does; the spec assumes it exists at deploy time.

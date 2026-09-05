# AGENTS.md

Context for automated agents and code reviewers working in this repo.

**Read `CLAUDE.md` in this directory first.** It is the full architecture,
the reasoning behind every rule below, and the record of what went wrong the
last time each rule was broken. This file is the short version.

## What this is

A read-only job discovery and application-prep pipeline. It finds roles by
querying public ATS APIs, tracks them through a lifecycle in a local SQLite
database, screens them through a fail-closed Fit Gate, and generates tailored
resumes and cover letters. The human applies by hand.

The operator's own data (`master_resume.yaml`, `profile.yaml`,
`companies.yaml`, `ideal-jd.md`) is gitignored; the repo tracks `.example`
templates that get copied into place on first run. Never commit a live copy.

Seven stages over one SQLite system of record: discover (`job_monitor.py`),
store (`jobdb.py`), gate (`gate.py`), generate (`job_generate.py`), track
(`job_cli.py`), freshness (`freshness.py`), liveness (`liveness.py`).

## Public-safe operator setup

This is a personal job-search tool, not an applicant-tracking system or an
application bot. Its scope is public job discovery, fit evaluation, interview
tracking, and preparation of documents for a human to review and submit. The
target role, seniority, location policy, salary floor, watched companies, and
exclusions are operator choices, not repository defaults.

The primary proven workflow is application assistance: tailoring documents,
checking fit, preparing for interviews, tracking rounds and state, and
organizing the search. Discovery is an optional convenience, not a promise
that the tool will supply the roles a person applies to. A human may fetch a
posting found elsewhere and use the same gate, preparation, and tracking
workflow.

Keep the operator's files outside the tracked tree. The normal root-relative
paths are only examples and may be replaced with absolute paths through the
environment:

- `master_resume.yaml` (`JOB_MASTER`): the source of truth for experience and
  the `do_not_claim` ledger. Start from `master_resume.example.yaml`.
- `profile.yaml` (`JOB_PROFILE`): target titles, location rules, and scoring
  settings. Start from `profile.example.yaml`.
- `ideal-jd.md` (`JOB_IDEAL_JD`): prose describing the role sought by the wide
  net. Start from `ideal-jd.example.md`.
- `companies.yaml` (`JOB_CONFIG`): public ATS boards and discovery filters.
  Start from `companies.example.yaml`.
- `jobs.db` (`JOB_DB`): the one SQLite system of record. Keep it outside Git.
- `applications/` (`JOB_APPS_DIR`): generated packages, also outside Git.

Before running a real search, copy the example files to private paths, set
`JOB_MASTER`, `JOB_PROFILE`, `JOB_IDEAL_JD`, `JOB_CONFIG`, `JOB_DB`, and
`JOB_APPS_DIR`, and confirm `git status` shows no live data. Never put a real
resume, job ledger, posting database, generated package, API key, or operator
path into an issue, fixture, test, commit, or pull request.

## Hard rules, non-negotiable

- **Never auto-apply.** No code that submits applications, fills external
  forms, or logs into job sites. Everything up to the click is automated; the
  human makes the submission.
- **Public endpoints only.** No scraping behind logins, no CAPTCHA solving, no
  credential automation. Keep the polite User-Agent and the inter-request
  delays in `job_monitor.py`. LinkedIn is limited to the logged-out guest view
  of a single posting the human supplied, never search or bulk scanning.
- **Generated documents may never invent** experience, employers, metrics, or
  skills that are not in `master_resume.yaml`. The tailoring note calls out
  gaps honestly rather than papering over them.
- **No em dashes, ever.** Use commas, parentheses, or separate sentences. This
  applies to generated documents, to code comments, and to anything written for
  the operator. Enforced in the generator prompt and in a post-process safety net; keep
  both. Flag any literal em dash a change introduces.

## The Fit Gate fails closed

`gate.py` blocks every prep artifact until it returns RECOMMEND or PROCEED, or
the operator records a written override. It exists because a role was prepped for a
week and then rejected for a gap that was visible in the job description on day
one. Its invariants:

- **One-directional enforcement.** Code may only make a verdict harsher, never
  softer. No code path upgrades a NONE.
- **Every error path returns ERROR**, and ERROR blocks drafting exactly like
  DO_NOT_APPLY. A gate that fails open is not a gate.
- **The `do_not_claim` ledger is absolute.** It overrules the model outright
  and is not adjudicable, by the model or by a human ruling, whether it matched
  by substring or by meaning (the semantic screen).
- **`require_pass()` lives in `job_generate.generate()`**, not the CLI, so the
  MCP and lead-inbox ingest paths inherit it. Do not move it and do not
  add a bypass.
- Nothing unblocks the gate without a written, audited record, and an override
  waives exactly one decision.

## When reviewing a change here

Focus on correctness in ATS-response parsing, the `jobdb.py` state machine
(illegal transitions must raise; the audit trail must stay intact), the gate
invariants above, and secret handling. `run_scan` and `generate` must stay
import-safe and side-effect-free.

Skip nitpicks. Reference file paths and line numbers. If a change is clean, say
so briefly rather than inventing concerns.

## Testing

`pytest -q` from the venv. `conftest.py` isolates `JOB_APPS_DIR` and `JOB_DB`
so tests can never write into the real `~/job-applications` or create a second
`jobs.db`. Keep both fixtures.

Never run `python job_cli.py` directly on your workstation: it would create a
second, divergent database. There is exactly one `jobs.db`, on the deployment
host. Drive it through `bin/jh`.

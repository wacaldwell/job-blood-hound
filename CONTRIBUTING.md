# Contributing

Thanks for looking at job-hound. This is a small, legible Python CLI over a
SQLite database, and it is meant to stay that way. Read `CLAUDE.md` and
`AGENTS.md` before you write code: `CLAUDE.md` is the full architecture plus
the record of what went wrong the last time each rule was broken, and
`AGENTS.md` is the short version of the rules themselves.

## Setting up

Python 3.12 or newer.

```
git clone <your fork>
cd job-hound
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy the example config files into place. The un-suffixed names are what the
code loads at runtime and they are gitignored, because between them they hold
a legal name, contact details, employment history, salary floor and commuting
radius:

```
cp master_resume.example.yaml master_resume.yaml
cp profile.example.yaml       profile.yaml
cp ideal-jd.example.md        ideal-jd.md
cp companies.example.yaml     companies.yaml
cp .env.example               .env
```

Then edit `.env`. `ANTHROPIC_API_KEY` is only needed for the stages that call a
model (gate, draft, and LLM-assisted refine); scanning, listing, pruning and
the deterministic ranking all run without it.

PDF generation needs LibreOffice (`soffice`) on `PATH`. Set `JOB_PDF=off` to
skip it.

## Running the tests

```
source .venv/bin/activate
python -m pytest -q
```

About 970 tests, and they should all pass before you open a pull request. CI
runs the same command on every PR (`.github/workflows/pr-checks.yml`), and the
`checks` job is the only required status check.

`conftest.py` redirects `JOB_APPS_DIR` and `JOB_DB` with autouse fixtures, so
the suite can never write into a real application-packages directory or create
a second `jobs.db` in the checkout. Keep both fixtures if you touch test setup.
They are the reason this suite is safe to run against a working install.

Test parsing, filter and state changes against mocked data. Do not claim an ATS
response shape works because it looks right; several fetchers were written to
documented shapes and only some have been exercised against live data.

## Branching and pull requests

GitHub Flow. `main` plus short-lived `feature/*`, `fix/*` and `chore/*`
branches. `main` is protected; changes land through pull requests with a
passing `checks` run, never direct pushes. Delete the branch after it merges,
locally and on the remote, then `git fetch --prune`.

Commit messages are `[Component]: Brief description`, where the component is
the file or subsystem you touched:

```
[gate]: ledger sweep runs over the raw JD, not just extracted requirements
[jobdb]: refuse an outcome on any destination but closed
[docs]: describe the wide net's status file
```

Work in progress uses `WIP: [Component]: what was accomplished`.

Keep pull requests small and single-purpose. Do not reformat or refactor code
you are not changing.

## House rules

These are not style preferences. Each one exists because breaking it caused a
real problem, and a pull request that violates one will be rejected on that
basis alone.

**No em dashes, anywhere.** Not in code, not in comments, not in docs, not in
strings the program emits, and not in generated documents. Use commas,
parentheses, or separate sentences. This is enforced in the generator prompt
and again in a post-process safety net; keep both. If a change introduces a
literal em dash, say so in the pull request so it can be removed.

**The Fit Gate fails closed and never gains a bypass.** `gate.py` blocks every
prep artifact until it returns RECOMMEND or PROCEED, or a written, audited
override is recorded. Its invariants:

- Enforcement is one-directional. Code may only make a verdict harsher, never
  softer, and no code path upgrades a NONE.
- Every error path returns ERROR, and ERROR blocks drafting exactly like
  DO_NOT_APPLY. A gate that fails open is not a gate.
- The `do_not_claim` ledger is absolute. It overrules the model and is not
  adjudicable, by the model or by a human ruling.
- `require_pass()` lives in `job_generate.generate()`, not in the CLI, so every
  caller (the CLI, the MCP adapter, the ingest path) inherits it. Do not move
  it, and do not add a flag that skips it.

**Never auto-apply.** No code that submits an application, fills an external
form, uploads a document to an employer, or logs into a job site. Everything up
to the click is automated and the human makes the submission. This applies to
every surface, including the write API and the MCP tools. There is no
configuration flag that would make it acceptable.

**Public unauthenticated endpoints only.** No scraping behind a login, no
CAPTCHA solving, no credential automation. Keep the polite User-Agent and the
inter-request delays in `job_monitor.py`; if you add a new fetcher, it sleeps
between calls like the others do. LinkedIn is limited to the logged-out guest
view of a single posting a human explicitly supplied, never search, discovery
or bulk scanning.

**Never commit personal data.** No real resume, no real contact details, no
populated `jobs.db`, no generated application packages, and no `.env`. The
`.gitignore` already covers these; do not remove those entries, and do not work
around them with `git add -f`. If you need example data, edit the `.example`
files, which carry an identical schema filled in with a fictional persona.

**Generated documents may never invent** experience, employers, metrics or
skills that are not in `master_resume.yaml`. The tailoring note calls out gaps
honestly rather than papering over them.

## Design conventions worth knowing

- SQLite is the system of record. `jobdb.py` is the only writer, and the state
  machine lives there in one language. Ask `jobdb.next_states(row)` rather than
  reading `TRANSITIONS` directly.
- `run_scan` and `generate` stay import-safe and side-effect-free, so a future
  GUI, skill or adapter can call them directly.
- Do not reach for new dependencies casually.
- Validate any schema change against an existing database, or say plainly in
  the pull request that a rebuild is required.
- Keep the MCP adapter thin. Every tool calls existing stage code and returns
  plain dicts.

## Reporting a security issue

See `SECURITY.md`. Do not open a public issue for a vulnerability.

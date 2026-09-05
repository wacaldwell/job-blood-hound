# job-hound

A read-only job discovery and application-prep pipeline. It finds roles by
querying public, unauthenticated ATS APIs directly (not LinkedIn search, not
aggregators), tracks each one through a lifecycle in a local SQLite database,
runs a fail-closed fit check before spending anything, and generates a tailored
resume and cover letter per role. It never applies to anything. Everything up
to the click is automated; you make the click.

That last part is deliberate and permanent. Auto-submitting is what gets
applications filtered and accounts banned, and it throws away the one real
advantage of preparing each application properly. There is no submit endpoint,
no form filler, and no login automation anywhere in this repo, and none will be
added.

## At a glance

| | |
|---|---|
| Language | Python 3.12 |
| Storage | one local SQLite file (`jobs.db`) |
| LLM provider | Anthropic API (`anthropic` SDK) |
| Interfaces | CLI (`job_cli.py`), MCP server (`job_hound_mcp.py`), local write API (`jobapi.py`) |
| Runs on | anything that runs Python; designed for one always-on Linux host |
| Cost of an unattended day | zero (the scheduled path makes no model calls) |
| License | MIT, see [LICENSE](LICENSE) |

## What it covers

| ATS | Public API? | Handling |
|-----|-------------|----------|
| Greenhouse | yes, no auth | fetched directly |
| Lever | yes, no auth | fetched directly |
| Ashby | yes, no auth | fetched directly |
| SmartRecruiters | yes, no auth (posting API) | fetched directly, paginated |
| Workday | yes (unauthenticated cxs JSON) | fetched when a `{host}/{site}` slug is set; else manual |
| iCIMS, Phenom, Avature | partner-gated only | listed for hand-checking |

Workday entries can carry an optional `search_text` (for example `engineer`),
sent server-side, for tenants whose full boards are too large to paginate.
Companies with no scannable feed print at the bottom of each report with their
careers URLs.

There is a second discovery source. `openjobs.py` ranks a large public corpus
of postings (roughly 2M postings across roughly 65,000 boards) against a query
document you write, `ideal-jd.md`. It reaches iCIMS, Oracle Cloud, Dayforce,
Workable, Paycom, Paylocity, Eightfold and Taleo, families with no feed to
scan. It costs no API spend on a normal run: the one embedding call fires only
when `ideal-jd.md` changes, ranking is local numpy, and the corpus group files
are anonymous static downloads.

## The pipeline

Eight stages over a shared SQLite system of record.

1. **Discover.** `job_monitor.py` is the deep watch: it polls the ATS boards
   listed in `companies.yaml` and filters by title, location, and exclude
   terms. `openjobs.py` is the wide net over the public corpus. Both land
   `discovered` rows.
2. **Store.** `jobdb.py` is the source of truth. Jobs with lifecycle state and
   posting date, an audit log of every state change, and versioned file
   records. Illegal state jumps raise rather than silently succeed.
3. **Gate.** A fail-closed fit check runs before any artifact exists. One LLM
   call reads the whole JD against your verified capabilities and proposes a
   verdict per requirement; Python then applies one-directional rules and
   renders the decision. Any error path (no API key, unfetchable JD,
   unparseable response) blocks drafting exactly like a bad fit does. Nothing
   bypasses it without a written, audited reason.
4. **Generate.** Fetches the full JD, calls the Anthropic API with your master
   resume plus the JD, and writes versioned DOCX (plus PDF if LibreOffice is
   present) into a standardized package folder.
5. **Track.** State transitions through the CLI. You apply by hand, then mark
   the state.
6. **Freshness.** Posting age, with its provenance labelled honestly. Some
   ATSes publish a true first-posted date; some only publish a last-updated
   date, and those are flagged approximate rather than presented as fact.
7. **Interview board.** Each job carries its own ordered list of interview
   rounds, rendered to an HTML board.
8. **Liveness.** `jh prune` asks the public endpoint whether a posting is still
   up, with one unauthenticated GET and no model call, so dead leads are
   cleared before the gate spends a call finding out the expensive way.

Two safety postures run in opposite directions on purpose. The fit gate fails
**closed**: anything uncertain blocks drafting, because a bad application costs
more than a skipped one. The liveness sweep fails **safe**: anything uncertain
stays `unknown` and is never marked closed, because a wrong `closed` silently
removes a real opportunity and no later stage would surface it again.

## Install

```bash
git clone <this repo>
cd job-hound
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

PDFs are generated through LibreOffice if `soffice` is on your `PATH`. Without
it, DOCX still writes; set `JOB_PDF=off` to skip the PDF step cleanly.

## First run: configuration

The repo tracks example config files. Copy each one to its un-suffixed name
(those are gitignored, so your real config never gets committed) and edit it:

```bash
cp master_resume.example.yaml master_resume.yaml
cp profile.example.yaml       profile.yaml
cp ideal-jd.example.md        ideal-jd.md
cp companies.example.yaml     companies.yaml
```

| File | What it is |
|------|-----------|
| `master_resume.yaml` | Everything you have actually done. The generator selects and emphasizes from this and never invents anything beyond it. It also holds `do_not_claim`, the ledger of things you must not claim, which overrules the model outright. |
| `profile.yaml` | Fit-scoring weights: title families you want, terms that mean remote-eligible, on-site areas you accept. |
| `ideal-jd.md` | Prose in the shape of a real posting, describing the role you want. The wide net embeds it into the same vector space as real postings, so write it as a job description, not as a list of keywords. |
| `companies.yaml` | The boards the deep watch scans, plus `title_terms`, `location_terms` and `exclude_terms`. |

`master_resume.yaml` says what you **have**; `ideal-jd.md` says what you
**want**. They are deliberately different documents.

There is a fifth template for secrets and paths. Copy it and fill it in; `.env`
is gitignored, `.env.example` is the tracked template:

```bash
cp .env.example .env
$EDITOR .env
set -a; source .env; set +a
```

At minimum you need:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export JOB_DB="$PWD/jobs.db"
export JOB_APPS_DIR="$PWD/applications"
```

`JOB_DB` has no default and nothing invents one. With it unset and no `jobs.db`
in the working directory, the CLI refuses to start rather than silently
creating a second database. That refusal is deliberate; see
[docs/single-source-of-truth.md](docs/single-source-of-truth.md).

## First run

```bash
python job_cli.py scan            # discover + ingest
python job_cli.py list            # see what landed
python job_cli.py queue <ident>   # mark one to pursue; runs the fit gate
python job_cli.py draft <ident>   # fetch JD, call the API, write the package
python job_cli.py show <ident>    # detail, files, history
```

`<ident>` resolves a full uid, a slug, or a unique slug prefix.

The company slugs in `companies.yaml` are guesses until you check them. A wrong
one returns 404 or 403, which is logged, and the scan continues. Open the real
careers page and confirm the slug (the part of the ATS URL that names the
company) before you rely on a run.

## The command surface

```
job_cli.py scan                     discover + ingest (default companies.yaml)
job_cli.py -c other.yaml scan       scan an alternate config
job_cli.py openjobs [--top N] [--groups N]
                                    wide-net discovery over the public corpus,
                                    ranked against ideal-jd.md. No LLM, no API
                                    spend. Ingests at most --top (default 15)
                                    NEW leads per run
job_cli.py fetch <url>              ingest one posting by URL -> queued, runs
                                    the fit gate
job_cli.py list [--state S] [--limit N] [--max-age H] [--all]
                                    pipeline view; hides postings older than
                                    --max-age (default 720h = 30 days)
job_cli.py prune [--state S] [--limit N] [--apply]
                                    liveness check (no LLM, no API spend); dry
                                    run unless --apply, which marks only closed
                                    postings skipped
job_cli.py show <ident>             detail + files + history
job_cli.py queue <ident>            mark to pursue (runs the fit gate)
job_cli.py gate <ident>             re-run the fit gate against a posting
job_cli.py gate-rule <ident> <n> --hard|--soft --note "..."
                                    rule on an UNSURE requirement; recomputes
                                    free, refuses a confident or ledger-forced
                                    item
job_cli.py gate-override <ident> --reason "..."
                                    bypass a blocked gate (reason mandatory)
job_cli.py gaps [<ident>]           open gaps, all or for one job
job_cli.py gap-plan <id> --plan "..." --hours N --deadline YYYY-MM-DD
job_cli.py gap-close <id> --reason "..."
job_cli.py draft <ident>            generate tailored package -> drafted
job_cli.py refine [--top N] [--no-llm] [--digest]
                                    score + rank leads, optional Discord digest
job_cli.py rounds <ident> ["a,b,c"] [--add "..."]
                                    show or set a job's interview rounds
job_cli.py stage <ident> <n|decision> [--next "..."] [--on YYYY-MM-DD]
                                    move the interview marker
job_cli.py board [--open]           render live loops to an HTML board
job_cli.py ready <ident>            reviewed, ready to submit
job_cli.py next                     next job to apply, with link + folder
job_cli.py apply <ident>            mark applied (date stamped)
job_cli.py skip <ident> [--reason "..."]
job_cli.py state <ident> <S>        set state directly (validated)
job_cli.py close <ident> --outcome <o>
job_cli.py stats                    pipeline counts
```

States: `discovered`, `queued`, `drafted`, `ready`, `applied`, `interviewing`,
`closed`, `skipped`. Close outcomes: `rejected`, `withdrawn`, `offer`,
`accepted`, `ghosted`, `other`. `closed` is terminal for a decided outcome; a
`ghosted` close is the one exception and reopens to `applied` or
`interviewing`, because ghosted means "they stopped replying", not "they said
no".

`job_monitor.py` also runs standalone, without the database, if all you want is
discovery:

| Flag | Effect |
|------|--------|
| `-c, --config PATH` | Config YAML (default `./companies.yaml`) |
| `--state PATH` | Seen-jobs state file (default: per-user data dir) |
| `--dry-run` | Print matches, write nothing, send nothing |
| `--first-run` | Seed state from current postings, suppress notifications |
| `--no-discord` | Skip Discord even if a webhook is configured |

Start with `--dry-run` while you tune filters, then `--first-run` once, then
normal runs.

## Environment variables

| Variable | Meaning |
|----------|---------|
| `ANTHROPIC_API_KEY` | Required for gate, refine with LLM, and draft |
| `JOB_DB` | SQLite path. Required unless a `jobs.db` exists in the working directory. No default is invented |
| `JOB_APPS_DIR` | Application packages root (default `~/job-applications`) |
| `JOB_CONFIG` | Scan config (default `./companies.yaml`) |
| `JOB_MASTER` | Master resume path (default `./master_resume.yaml`) |
| `JOB_PROFILE` | Fit profile path (default `./profile.yaml`) |
| `JOB_IDEAL_JD` | Wide-net query document (default `./ideal-jd.md`) |
| `JOB_FIT_MODEL` | Ranking model (default `claude-haiku-4-5`) |
| `JOB_GATE_MODEL` | Fit gate model (default `claude-opus-4-8`) |
| `JOB_DRAFT_MODEL` | Drafting model (default `claude-opus-4-8`) |
| `JOB_MODEL` | Global model override; component variables win when both are set |
| `JOB_LLM_USAGE_LOG` | Usage log path (default `${LOG_DIR:-$HOME/logs}/job-hound/llm-usage.log`), or `off` |
| `JOB_PDF` | `off` skips PDF generation |
| `JOB_CONTACT_EMAIL` | Contact address advertised in the outbound User-Agent |
| `JOB_OPENJOBS_CACHE` | Wide-net manifest and group cache (default beside `jobs.db`) |
| `JOB_OPENJOBS_URL` | Corpus endpoint |
| `DISCORD_WEBHOOK_URL` | Digest destination, optional |
| `JOB_HOST` | ssh target used by `bin/jh` and `bin/jh-pull` |
| `JOB_API_TOKEN`, `JOB_API_PORT` | Local write API auth and port |
| `PATH` | LibreOffice (`soffice`) must be on it for PDFs |

## Cost controls

The scheduled daily path (`bin/daily.sh`) runs scan, the wide net, and
`refine --no-llm --top 0 --digest`. None of that calls a model, so an
unattended day costs nothing. A manual `job_cli.py refine` scores at most three
previously unscored leads by default (`--top`). The gate is one call per job.
Drafting is one call per package.

Every successful model response appends a secret-free JSON event to the usage
log. Events identify the component (`fit`, `gate`, `gate_screen`, `draft`,
`benchmark`), record the model and exact API token counts, and include an
estimated cost when the model has a known price. Summarize recorded spend:

```bash
jq -s 'group_by(.component) | map({component: .[0].component,
  calls: length, cost_usd: (map(.estimated_cost_usd // 0) | add)})' \
  "${LOG_DIR:-$HOME/logs}/job-hound/llm-usage.log"
```

## What a package looks like

```
<JOB_APPS_DIR>/2026-06-16__acme__senior-site-reliability-engineer__6d83/
  Jordan_Rivers_Senior_Site_Reliability_Engineer_Resume_v1.docx
  Jordan_Rivers_Senior_Site_Reliability_Engineer_Cover_Letter_v1.docx
  (PDFs alongside if LibreOffice is present)
  job-description.md
  tailoring-note.md
```

Filenames are `Name_Role_DocType_vN`: your name first so it is findable in a
recruiter's downloads folder, the role next so the tailoring is visible, no
company (the folder carries that), and a version suffix that strips cleanly
before upload. Re-drafting bumps the version. Nothing is overwritten.

Note that ATS systems parse file **contents**, not filenames, so this
convention is for the human reading it and for your own organization. It does
nothing for ATS ranking, and this repo does not claim otherwise.

## Tests

```bash
source .venv/bin/activate
pytest -q
```

`conftest.py` isolates `JOB_APPS_DIR` and `JOB_DB` for the whole suite with
autouse fixtures, so running the tests can never write into your real
applications folder or create a second `jobs.db` in the checkout. Keep both
fixtures if you touch test setup. They are what makes the suite safe to run
against a working repo.

CI runs the same `pytest -q` on every pull request
(`.github/workflows/pr-checks.yml`).

## Architecture

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) walks the modules and the data
flow. [CLAUDE.md](CLAUDE.md) is the long-form design record: what each rule is
for, and which of them exist because something went wrong once.

Deployment runbooks live in `docs/` and `deploy/`:

- [docs/deploy-tools-host.md](docs/deploy-tools-host.md), running it on an
  always-on host
- [docs/deploy-mcp.md](docs/deploy-mcp.md), exposing it over MCP
- [docs/single-source-of-truth.md](docs/single-source-of-truth.md), why there is
  exactly one database
- [deploy/README-job-api.md](deploy/README-job-api.md) and
  [deploy/README-job-ingest.md](deploy/README-job-ingest.md)

The scan and generation cores are import-safe and side-effect-free, so a GUI or
another tool can call them directly:

```python
from job_monitor import run_scan
new_jobs, all_matches, manual = run_scan(cfg, seen)
```

`run_scan` does no file I/O and sends no notifications. The caller owns state
and output.

## Ethics and scope

These are hard rules, not preferences. They are enforced in the code and in the
review checklist, and a change that weakens one will not be accepted.

- **Discovery and prep only. Never auto-apply.** No feature that submits an
  application, fills an external form, or logs into a job site. `apply` stamps
  a state, nothing more.
- **Public endpoints only.** The ATS APIs used here are the same
  unauthenticated endpoints a company's own careers page calls from your
  browser. No scraping behind a login, no CAPTCHA solving, no credential
  automation.
- **LinkedIn: one posting at a time, guest view only.** `job_fetch.py` may read
  the logged-out public view of a single posting you explicitly hand it. There
  is no LinkedIn search, discovery, or bulk scanning, and there will not be.
- **Be a polite client.** The scanner identifies itself with a real User-Agent
  (set `JOB_CONTACT_EMAIL`) and sleeps between calls. The liveness sweep uses a
  1.5s delay and runs weekly, not daily. Leave those in. Polite use is what
  keeps public APIs public.
- **Accuracy over keyword stuffing.** The generator may only select and
  emphasize from your master resume. It must never invent experience,
  employers, metrics, or skills that are not in it, and the tailoring note has
  to name gaps honestly rather than paper over them.

## Contributing and security

- [CONTRIBUTING.md](CONTRIBUTING.md) covers the branch and PR workflow.
- [SECURITY.md](SECURITY.md) covers reporting a vulnerability.
- Your real config files (`master_resume.yaml`, `profile.yaml`, `ideal-jd.md`,
  `companies.yaml`), `jobs.db`, and everything under `applications/` are
  gitignored. They contain your personal data. Check `git status` before you
  commit if you are unsure.

## License

MIT. See [LICENSE](LICENSE).

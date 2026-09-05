# job-hound quickstart

Read-only job discovery plus tailored application generation. Discovers roles
from public ATS APIs, generates a tailored resume and cover letter per job,
tracks everything in a local database. You apply by hand. See README.md for the
full picture; this is just the run sequence.

The main value is application assistance: fit checking, tailored documents,
interview preparation, and tracking. Discovery is optional. You can fetch a
posting found elsewhere with `python job_cli.py fetch <url>` and use the same
review and preparation workflow.

## One-time setup

```bash
git clone <repository-url> job-hound
cd job-hound

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy the tracked example configs to their real names and edit them. The
un-suffixed names are gitignored, so your personal data never gets committed:

```bash
cp master_resume.example.yaml master_resume.yaml   # what you have actually done
cp profile.example.yaml       profile.yaml         # fit-scoring weights
cp ideal-jd.example.md        ideal-jd.md          # the role you want, as prose
cp companies.example.yaml     companies.yaml       # boards to scan + filters
```

`master_resume.yaml` is the one to spend real time on. The generator selects
and emphasizes from it and never invents anything beyond it, so anything
missing from it can never appear in a draft.

Do the same for secrets and paths. `.env.example` is the tracked template;
`.env` is gitignored:

```bash
cp .env.example .env
$EDITOR .env                       # put your ANTHROPIC_API_KEY in it
set -a; source .env; set +a
echo ${ANTHROPIC_API_KEY:0:7}      # should print: sk-ant-
```

Point the database and the generated packages at the project for now:

```bash
export JOB_DB="$PWD/jobs.db"
export JOB_APPS_DIR="$PWD/applications"
```

These are local-only paths. If you prefer to keep personal data elsewhere,
set `JOB_MASTER`, `JOB_PROFILE`, `JOB_IDEAL_JD`, and `JOB_CONFIG` to the four
private config files, and set `JOB_DB` and `JOB_APPS_DIR` to private storage
locations. Do not put those paths or files in Git.

`JOB_DB` has no default. With it unset and no `jobs.db` in the working
directory, the CLI refuses to start rather than quietly creating a second
database somewhere you will forget about.

The API key is required for the fit gate and document generation, but not for
installing the project, running the tests, or inspecting a local database.

PDFs need LibreOffice on `PATH` (optional; DOCX works without it):

```bash
export PATH="/Applications/LibreOffice.app/Contents/MacOS:$PATH"   # macOS
# or skip PDFs entirely:
# export JOB_PDF=off
```

## First run

`test.yaml` is a one-company config against a known-live Greenhouse board, so
you can prove the plumbing works before touching your real `companies.yaml`.

```bash
# 1. discover + ingest
python job_cli.py -c test.yaml scan

# 2. see what landed
python job_cli.py list

# 3. queue one (use the slug or a unique prefix from the list)
python job_cli.py queue <slug-prefix>
```

`queue` runs the fit gate. The gate fails closed, so if it returns anything
other than RECOMMEND or PROCEED, `draft` will refuse. Read the report, then
either fix the underlying issue or record a written override:

```bash
python job_cli.py show <slug-prefix>                       # includes the gate report
python job_cli.py gate-override <slug-prefix> --reason "..."   # reason is mandatory
```

An override waives exactly one decision. Any fresh `gate` run clears it, so
finish any `gate-rule` rulings before you override, not after.

```bash
# 4. the real test: fetch JD, call the API, generate the package
python job_cli.py draft <slug-prefix>

# 5. inspect
python job_cli.py show <slug-prefix>
open applications/
```

## Every new shell

You only re-run the environment lines, not the install:

```bash
cd path/to/job-hound
source .venv/bin/activate
set -a; source .env; set +a
export JOB_DB="$PWD/jobs.db"
export JOB_APPS_DIR="$PWD/applications"
```

Tip: put those four lines in a `dev.sh` and `source dev.sh` to save typing.
Keep the secrets in `.env`, not in `dev.sh`.

## Common verbs

```
job_cli.py scan                  discover + ingest
job_cli.py list [--state STATE]  see the pipeline
job_cli.py show <ident>          detail + files + history
job_cli.py queue <ident>         mark to pursue (runs the fit gate)
job_cli.py gate <ident>          re-run the fit gate
job_cli.py draft <ident>         generate tailored package -> drafted
job_cli.py ready <ident>         reviewed, ready to submit
job_cli.py next                  next job to apply, with link + folder
job_cli.py apply <ident>         you submitted it (date stamped)
job_cli.py state <ident> STATE   set state directly
job_cli.py close <ident> --outcome offer
job_cli.py stats                 pipeline counts
```

The full command surface is in README.md.

## If draft errors

The Greenhouse description fetch is the most road-tested path. The Lever,
Ashby, and SmartRecruiters fetchers were written to their documented response
shapes and have had less live exercise. If `draft` fails on one of those, the
error is deliberately clean rather than a crash. Note the message; the fetcher
is the thing to adjust.

If it fails with `ANTHROPIC_API_KEY not set`, your env file did not load. If it
fails with a gate message, that is the gate working as designed, not a bug.

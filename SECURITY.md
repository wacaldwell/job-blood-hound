# Security policy

job-hound is a personal-scale tool: one operator, one SQLite database, no
multi-tenant service and no hosted deployment anyone else logs into. The
security surface is small, but it handles a resume and an API key, so please
report problems rather than filing them publicly.

## Reporting a vulnerability

Open a **private security advisory** through GitHub:

> Security -> Report a vulnerability

on this repository. If you cannot reach that form, contact the maintainer
through the GitHub profile listed as the repository owner. Do not open a public
issue, a pull request, or a discussion thread for a suspected vulnerability.

Please include what you found, the file and line if you have it, the steps to
reproduce, and what an attacker could actually do with it.

### What to expect

This is a side project maintained by one person, so the timelines are honest
rather than aspirational:

- Acknowledgement within 7 days.
- An assessment (accepted, needs more information, or out of scope) within 30
  days.
- A fix on `main` for anything accepted as a real issue, on a best-effort
  schedule. There is no backport or long-term-support branch; `main` is the
  only supported version.

Please give a reasonable window before disclosing publicly. Credit in the
release notes on request.

## In scope

- Leaking the operator's personal data (resume contents, contact details,
  job-search history) to anywhere it was not meant to go.
- Leaking `ANTHROPIC_API_KEY`, `JOB_API_TOKEN`, a Discord webhook URL, or any
  other secret into logs, generated files, network requests, or the repository.
- The local write API (`jobapi.py`): authentication bypass, a route reachable
  off localhost, or any endpoint that writes without going through an audited
  `jobdb.py` setter.
- SQL injection or state-machine bypass in `jobdb.py`.
- Path traversal in package-folder or file-record handling.
- Prompt injection through a job description that causes the tool to exfiltrate
  data, run commands, or bypass the Fit Gate.
- Any path that submits an application, fills an external form, or authenticates
  to a job site. That is a hard rule of this project, so a way to trigger it is
  a security bug, not a feature request.

## Out of scope

- Anything requiring an attacker who already has shell access as the operator,
  or write access to the checkout. At that point the API key in the environment
  is theirs anyway.
- Vulnerabilities in the Anthropic API, in an ATS vendor's public endpoints, or
  in third-party dependencies. Report those upstream; if a dependency needs
  pinning here, a normal issue is the right place.
- Rate limiting, denial of service, or scanning volume against public ATS
  endpoints. The politeness delays are a courtesy control, not a security
  boundary.
- The absence of authentication on a tool that only ever binds to `127.0.0.1`
  and is not exposed off the host.

## Data handling posture

The interesting part of this project's security story is what data it touches,
so here it is plainly.

**Everything is local by default.** Job-search state lives in one SQLite file
(`jobs.db`), on disk, owned by the operator. There is no hosted backend, no
telemetry, no analytics, and nothing phones home. Generated resumes and cover
letters are written to a local packages directory (`JOB_APPS_DIR`, default
`~/job-applications`).

**Data does leave the machine, in three specific directions.**

1. **The Anthropic API.** Screening a role through the Fit Gate and drafting a
   tailored package both send the job description plus the contents of
   `master_resume.yaml` (the operator's full employment history, skills and
   certifications) to the model. This is the core function of the tool and
   cannot be avoided while using those stages, but the deterministic paths
   (`scan`, `list`, `prune`, `refine --no-llm`) never make a model call and
   send nothing. An alternative Anthropic-compatible provider can be selected
   with `JOB_PROVIDER`, in which case that provider receives the same data.
2. **Public ATS endpoints.** Unauthenticated GET requests to job boards, to
   read postings. These carry no personal data beyond a User-Agent, whose
   contact address is configurable through `JOB_CONTACT_EMAIL`.
3. **A Discord webhook, if configured.** The daily digest posts job titles,
   companies, scores and links. No resume content.

**Secrets live in the environment, never in the repository.** `ANTHROPIC_API_KEY`
is read from the environment (`os.environ`) at call time and is never written to
a config file in the repo, never logged, and never included in a generated
document. The LLM usage log records model, token counts and cost, never keys and
never prompt contents. `.env`, `*.env` and `.env.*` are gitignored, and
`.env.example` carries placeholders only. A pre-commit hook set
(`.pre-commit-config.yaml`) runs gitleaks, ripsecrets and private-key detection
against the staged diff; install it with `pre-commit install`.

**The operator's identity files are gitignored, not tracked.** `master_resume.yaml`,
`profile.yaml`, `ideal-jd.md` and `companies.yaml` are the four files the code
loads at runtime and they are all ignored by git. What the repository tracks
instead is a `.example` sibling of each, with an identical schema filled in with
a fictional persona. `jobs.db`, `*.db`, the generated `applications/` directory
and any LinkedIn data export are ignored for the same reason. Committing any of
them to a public fork would publish a resume and a job-search plan under a real
name, so those `.gitignore` entries are load-bearing. Do not remove them, and
run `scripts/check-no-pii.sh` before publishing a fork.

**The tool never authenticates as the operator to anything.** No job-site login,
no cookies, no credential store, no form submission. The local write API is the
only authenticated surface, it binds to `127.0.0.1` only, and its bearer token
(`JOB_API_TOKEN`) comes from the host environment file.

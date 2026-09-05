# Fetch-by-URL ingestion

Date: 2026-07-06
Status: approved
Origin: adapted from pushcv-cli (github.com/notnotparas/pushcv-cli, MIT), which
demonstrated the LinkedIn guest-view fetch and the generic schema.org
JobPosting fallback.

## Problem

job-hound can only ingest a posting by URL through the lead inbox UI spool,
and only for Greenhouse, Lever, Ashby, and SmartRecruiters URLs. A LinkedIn
link (the most common thing a recruiter sends) fails with "Unsupported posting
URL, paste the JD". There is also no local CLI way to say "I found this role,
put it in the pipeline".

## Decisions (made with the operator, 2026-07-06)

- New dependencies accepted: `curl_cffi` (browser TLS impersonation so
  LinkedIn's bot wall serves the public guest view) and `beautifulsoup4`
  (guest DOM parsing). Used only by the new fetch module.
- `job fetch <url>` ingests and marks the job `queued`, with no LLM spend.
  The lead inbox UI path keeps its existing full auto-eval behavior.

## Hard-rule compliance

- LinkedIn access is the logged-out public guest view only: no login, no
  cookies, no credential automation. Single posting per invocation, always
  user-initiated. Never LinkedIn search, discovery, or bulk scanning.
- Discovery-and-prep only. Nothing here submits applications.
- A guardrail note goes into CLAUDE.md's hard rules section.

## Design

### New module: job_fetch.py

Side-effect-free (returns dicts, no DB writes, no printing), matching the
run_scan/generate convention. Header comment credits pushcv-cli.

1. `fetch_linkedin_job(url)`: extracts the job id from `currentJobId=` or
   `/jobs/view/<id>` URL shapes, fetches the guest job-view page plus the
   `linkedin.com/jobs-guest/jobs/api/jobPosting/{id}` fragment endpoint via
   curl_cffi (Chrome impersonation), parses JSON-LD JobPosting with guest DOM
   selectors as fallback. Returns
   `{title, company, location, description, apply_url, posted_at}`.
2. `fetch_jsonld_posting(url)`: generic fallback for any careers page. Fetches
   the page and parses a schema.org `JobPosting` object from
   `<script type="application/ld+json">`. Covers Ashby-hosted pages, Workable,
   and many iCIMS/Phenom sites (issue #42).
3. `resolve_url(url)`: the one orchestrator both consumers call.
   Resolution order:
   - (a) existing `job_ingest.parse_posting_url` ATS matchers, then existing
     `fetch_posting_meta` (that code path is unchanged);
   - (b) LinkedIn host: guest fetch; if the apply URL parses as a supported
     ATS, chain-scrape to the canonical ATS posting so the uid becomes
     `ats:company:id` and dedupes against the daily scan (LinkedIn description
     kept as fallback if the ATS fetch fails);
   - (c) anything else: JSON-LD fallback.
   Returns a normalized
   `{ats, company, ext_id, title, location, description, url, posted_at,
   date_source}` or raises `FetchError` with a human-readable reason.

### Consumers (one code path)

- `job_cli.py fetch <url>` (new `cmd_fetch`): resolve, `upsert_job`, store
  description, transition to `queued` with note "fetched by url". Prints
  company/title/slug and a `job draft <slug>` hint. If the uid already exists,
  report its current state and touch nothing.
- `job_ingest.process_one`: the unparseable-URL branch tries
  LinkedIn/JSON-LD resolution before demanding a pasted JD. Idempotency
  guard, scoring, auto-draft, and Discord messages are untouched. Discord
  submissions of LinkedIn links start working with zero the lead inbox UI
  changes.

### Data mapping

- Canonical-ATS chained postings: normal `ats:company:ext_id` uid, identical
  to scanner rows (dedupe for free).
- LinkedIn-only postings (Easy Apply or no recognizable ATS behind the apply
  button): `ats="linkedin"`, `ext_id` = numeric LinkedIn job id, company
  slugified from the JSON-LD hiringOrganization name.
- Generic JSON-LD pages: existing `manual` ats convention (`_manual_ext_id`
  sha1, `_host_company_slug` fallback for company).
- `posted_at` from JSON-LD `datePosted` where present with
  `date_source="jsonld"` (a true date); otherwise empty, so the row is
  honestly "age unknown" per the freshness policy.

### Errors

Any fetch or parse failure raises `FetchError` with the reason. The CLI prints
it and suggests the lead inbox UI paste-the-JD route. `process_one` keeps
its existing `fetch_failed` record shape.

## Testing

Fixture-driven, no live network:

- URL id extraction table test (view URLs, currentJobId URLs, junk).
- LinkedIn guest page fixture (JSON-LD present), fragment fixture,
  login-walled fixture (no JobPosting data), Easy Apply fixture.
- Chain-scrape case: LinkedIn fixture whose apply URL is a Greenhouse posting.
- Generic JSON-LD careers-page fixture.
- `process_one` with a LinkedIn URL (resolver injected).
- `cmd_fetch` flow with an injected resolver: new job, duplicate job,
  fetch failure.

## Out of scope

No MCP tool (the Discord path already covers remote ingestion). No LinkedIn
discovery. No salary estimation. No PDF/Markdown changes to the generator.

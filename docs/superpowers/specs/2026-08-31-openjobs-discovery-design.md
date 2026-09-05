# open-jobs as a second discovery source

Date: 2026-08-31
Branch: `feat/openjobs-discovery`
Status: implemented, awaiting review

## Problem

`job_monitor.run_scan` can only find jobs at companies already listed in
`companies.yaml`: 214 entries, 23 of which are `manual` (no public feed), so
191 boards are actually scanned. Every lead the pipeline has ever discovered
came from that list.

The ceiling is measurable. On 2026-08-31 a hand-run of the open-jobs toolchain
produced a 10-role shortlist; 9 of the 10 were at companies the scanner cannot
see. One 12-group slice of the open-jobs corpus carries 3,061 postings from
1,327 boards and 1,316 companies, and includes iCIMS, Oracle Cloud, Dayforce,
Workable, Paycom, Paylocity, Eightfold, Pinpoint, Recruitee, Crelate, Personio
and Taleo postings. CLAUDE.md currently records iCIMS, Phenom and Avature as
having "no public feed"; open-jobs already carries three of those families.

## What open-jobs is

A daily crawl of ~65,000 company boards, ~2.07M open postings, with full JD
text and a 1536-dim embedding per posting, published as static files behind a
Cloudflare Worker. Three public contracts, documented as fixed:

- `POST /embed` - text in, vector out. The only call that carries our data.
- `GET /data/manifest.json`, `GET /data/centroids.bin` - the group tree and its
  centroid matrix.
- `GET /data/groups/<id>.json` - one group's postings, with full JD and vectors.

## Design

### Shape: a second parallel source, not a replacement

`run_scan` is untouched. `openjobs.py` is a new discovery stage that produces
the same job-dict shape and ingests through the same `db.ingest_scan` path.

```
bin/daily.sh
  1. job_cli.py scan            ATS deep watch, 191 boards
  2. job_cli.py openjobs        wide net, 2.07M corpus, top 15   <- new
  3. job_cli.py prune --apply   Sundays only
  4. job_cli.py refine --no-llm --top 0 --digest
```

The scanner is the deep watch on curated companies and guarantees daily
coverage of them. open-jobs is the wide net. Neither can break the other: the
new stage catches every exception at its own boundary and returns an empty
result, exactly as `run_scan` catches per-company failures.

### No new toolchain: a native client

We do NOT vendor the open-jobs repo, shell out to `uv run tools/jobs.py`, or
adopt duckdb/parquet. `openjobs.py` talks to the three public endpoints
directly with `requests` (already a dep) and stdlib `json`.

The single new dependency is `numpy`, and the reason is MEMORY rather than
speed. The centroid matrix is 15,011 nodes x 1,536 dims: 92MB as a float32
array, against a measured 739MB as a Python list of lists (23M float objects
plus per-list overhead). The tools host has 8GB total and already runs Mission
Control, the write API and Grafana, so a 739MB allocation in the daily cron is
a real OOM risk.

Speed is a secondary bonus, and an earlier draft of this document inflated it:
ranking is ~0.005s in numpy against ~0.48s in pure Python. Half a second a day
would not have justified a dependency on its own, and claiming it did was the
kind of unearned number this repo's whole culture of honest provenance exists
to prevent.

Consequence: nothing about the host's job-hound checkout changes except
`pip install -r requirements.txt`, which `.github/workflows/deploy.yml` already
runs when requirements.txt changes.

### Cost: zero LLM spend, zero API spend

`bin/daily.sh` is deliberately deterministic and free, and stays that way.

- The embedding call runs only when `ideal-jd.md` changes. Its vector is cached
  in the openjobs cache dir, keyed by the sha256 of the JD text. A daily run
  with an unchanged JD makes no `/embed` call at all.
- Group ranking is local numpy.
- Group downloads are anonymous static files.

`/embed` is rate-limited to 10 per 10 min per IP. Since we embed on JD change
only, we are nowhere near it.

### The ideal JD

`ideal-jd.md` lives at the repo root, beside `master_resume.yaml`,
`companies.yaml` and `profile.yaml`. It is hand-maintained prose in the shape
of a real posting, because it embeds into the same space as real postings.

It is deliberately NOT generated from `master_resume.yaml`. The resume says
what the operator HAS; the ideal JD says what he WANTS, and the two differ (the resume
records Kubernetes exposure that `do_not_claim` forbids claiming, and says
nothing about the $150k floor or the Portland radius).

It is version-controlled rather than kept in a scratch dir, because the
2026-08-31 hand-run left `ideal-jd.v1.md` and `ideal-jd.v2.md` on disk with
different salary targets ($150k vs $200k) and no record of which produced the
shortlist. A checked-in file with a hash-keyed vector cache makes that
impossible: the cache records which text produced the vector.

### Volume: a hard cap, not a threshold

Each run takes at most `--top N` (default 15) postings. Ordering is cosine
similarity to the ideal JD, descending. Anything already in the DB by uid or by
URL is dropped before the cap is applied, so the cap always buys 15 genuinely
new leads rather than 15 slots mostly filled by yesterday's.

A similarity threshold was considered and rejected: `discovered` already holds
285 rows and the binding constraint is triage capacity, not recall. A cap is
one tunable number and cannot flood.

### Dedup

Two layers, because one is not enough:

1. **uid.** `uid = ats:company:ext_id`, and job-hound's `company` is the board
   slug. open-jobs publishes `ats`, `slug` and `id` from the same boards, so a
   posting both sources see produces the same uid and `upsert_job` already
   no-ops on it. This is free and catches the common case.
2. **URL.** Where an ATS id or slug is spelled differently between the two
   crawlers, the canonical posting URL still matches. Checked explicitly
   against the whole jobs table before ingest.

### Provenance: a `source` column

`jobs.source TEXT` is added via the existing additive `ADDED_COLUMNS`
migration, defaulting to `'scan'` for every pre-existing row. `upsert_job`
writes `job.get("source", "scan")`, and the `state_log` note for a discovery
becomes the source name rather than the hardcoded `'scan'`.

This is what the lead inbox UI renders as a provenance badge on a lead, and what
lets a future analysis ask which source actually produces interviews.

### Free JD text

The corpus carries full JD text per posting. `upsert_job` already accepts and
stores `description`, so an open-jobs lead lands with its JD already populated.

Scope of that win, stated precisely, because the first draft of this document
overstated it. `job_generate.fetch_description` ALWAYS re-fetches for the four
ATSes it can build a posting URL for (greenhouse, lever, ashby,
smartrecruiters), deliberately, so a re-draft picks up an edited JD. For those,
the stored text is not read and the gate still pays one fetch.

Where it does pay off is the rest of the corpus, which is most of what the wide
net uniquely brings: oraclecloud, dayforce, workable, paycom, paylocity,
eightfold, breezy, recruitee, crelate, personio, pinpoint, icims, taleo and
hostname-slug Workday rows all return None from `posting_endpoint` and fall
through to the stored description. Before this change a lead on any of them had
no JD at all and the gate returned ERROR on "an unfetchable or empty JD", which
blocks drafting exactly like DO_NOT_APPLY. Those leads are now gradeable.

The same asymmetry decides `company_slug`. For the four URL-bearing ATSes,
`company` is a path segment, so the slug is never rewritten: `umbralabs.com`
is a real Ashby board slug (its API returns 200, `umbralabs` returns 404), and
prettifying it would turn a good lead into a gate ERROR. Everywhere else the
slug builds no URL and a pathological one is cleaned up for the reader.

### Location and filtering

`classify_location` and `residency_excludes_eastern` are imported from
`job_monitor` rather than reimplemented, so both sources bucket
`location_type` identically. Because the JD text is already in hand, the
body-residency check that `scan_and_ingest` performs with a bounded set of
extra HTTP fetches runs here with no network at all.

The `title_terms` / `exclude_terms` filters from `companies.yaml` are applied
too. Embedding similarity is a good ranker and a poor filter; the existing
term lists are the cheap guard against a semantically-near role that is
obviously wrong.

### Failure behavior

Discovery fails SAFE, matching `run_scan`, and unlike the Fit Gate which fails
closed. A missing ideal-JD file, a 429, a network error, a corrupt group file
or an unparseable manifest all yield zero candidates and a logged reason. The
daily digest must still go out. The asymmetry is deliberate and is the same one
CLAUDE.md already records for the liveness sweep: a missed lead costs one day,
a broken digest costs the whole routine.

`bin/daily.sh` runs the stage isolated from `set -e`, exactly as the weekly
prune sweep is isolated, so a failed wide net cannot stop the digest.

## Components

| Unit | Responsibility | Depends on |
|---|---|---|
| `openjobs.py` | corpus client + ranking + candidate selection. No DB writes, no printing. | requests, numpy, job_monitor (location helpers) |
| `job_cli.openjobs_and_ingest` | drives the stage, writes rows, returns a summary dict | openjobs, jobdb |
| `job_cli.cmd_openjobs` | CLI surface, prints the summary | the above |
| `jobdb` `source` column | provenance, one additive migration | - |
| lead-inbox lead inbox | renders the provenance badge | jobs.db |

`openjobs.py` stays import-safe and side-effect-free like `run_scan` and
`generate`, so the MCP adapter and any future GUI can call it directly.

## Testing

Every network boundary is injectable. Tests use a recorded manifest, a small
synthetic centroid matrix and two hand-written group files; no test touches the
network. `conftest.py` gets a third autouse fixture isolating the openjobs
cache dir, for the same reason it already isolates `JOB_DB` and
`JOB_APPS_DIR`.

Cases: vector cache hit/miss on JD hash, ranking order, the top-N cap, uid
dedup, URL dedup, description carry-through, location classification parity
with the scanner, term filtering, and every failure path returning empty rather
than raising.

## Out of scope

- The `enrich` endpoint (metered, $5/hr per IP). Not needed; the gate already
  does structured extraction, better and against the real resume.
- The taste model / pairwise `rank.py` sort. `fit.score()` already ranks the
  pipeline, and a second ranker would drift from it.
- `companies.yaml` auto-suggestions from open-jobs discoveries. Worth doing,
  but it is a separate change with its own review.

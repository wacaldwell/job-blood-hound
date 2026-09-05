# CLAUDE.md

Guidance for Claude when working in the job-hound project.

## What this is

A read-only job discovery and application-prep pipeline. It finds roles by
querying public ATS APIs directly (not LinkedIn or aggregators), tracks them
through a lifecycle in a local SQLite database, and generates tailored resumes
and cover letters per role via the Anthropic API. The human applies by hand.

Who it is for: a senior infrastructure engineer (cloud, platform, SRE) running
their own search, typically for remote senior through principal roles plus
management. Nothing about that operator is hardcoded. The resume, the target
titles, the companies watched, the wanted-role prose, the on-site area for
non-remote roles (`onsite_ok`) and the salary floor (`salary_floor`) all live
in operator-supplied data files, and the code reads them from there.

## Scope and private paths

The intended job scope is configurable: public ATS discovery, a deep watch of
selected boards, a wide-net search against the public corpus, fit scoring,
interview tracking, and application preparation for a human reviewer. It does
not submit applications, log into job sites, or decide a role's fit without
the operator's fail-closed gate and review. Titles, seniority, remote or
on-site policy, salary floor, watched companies, and excluded roles belong in
the operator's private configuration rather than in code.

The primary real-world use is application assistance rather than autonomous
lead generation. The useful path is to evaluate a role, tailor the resume and
cover letter, prepare for interviews, track rounds and application state, and
keep the search organized. The original operator did not rely on this app's
discovered leads as the main source of applications. Discovery is therefore
an optional convenience; a role found elsewhere can enter the same workflow
through `job_cli.py fetch`.

Use these root-relative names only as safe examples. Keep the live files
outside Git and point to them with environment variables when they live
elsewhere:

| Purpose | Example path | Override |
| --- | --- | --- |
| What the operator has done, plus `do_not_claim` | `master_resume.yaml` | `JOB_MASTER` |
| Target titles, location policy, and scoring | `profile.yaml` | `JOB_PROFILE` |
| Prose describing the wanted role | `ideal-jd.md` | `JOB_IDEAL_JD` |
| Watched ATS boards and filters | `companies.yaml` | `JOB_CONFIG` |
| SQLite system of record | `jobs.db` | `JOB_DB` |
| Generated resume and cover-letter packages | `applications/` | `JOB_APPS_DIR` |

Start from the tracked `.example` templates. A safe local setup can use
`$PWD` for the database and packages, while a managed deployment can use its
own external paths. Do not document or commit a real operator resume,
database, generated package, host path, endpoint, credential, or search
history. The tracked examples and test fixtures must remain synthetic.

**First run.** Those four data files are gitignored so a fork never carries
someone else's career. The repo tracks `.example` templates; copy them before
anything works:

```
cp master_resume.example.yaml master_resume.yaml   # what you HAVE
cp profile.example.yaml       profile.yaml         # location + scoring rules
cp companies.example.yaml     companies.yaml       # boards the deep watch scans
cp ideal-jd.example.md        ideal-jd.md          # what you WANT
```

Everything below refers to the live (un-suffixed) copies, because those are
what the code loads at runtime.

## Hard rules

- **Discovery and prep only, never auto-apply.** Everything up to the click is
  automated; the human makes the submission. Auto-submitting gets applications
  filtered and accounts banned, and throws away the early-human-tailored edge.
  Do not add any feature that submits applications, fills external forms, or
  logs into job sites.
- **Public endpoints only.** No scraping behind logins, no CAPTCHA solving, no
  credential automation. The ATS APIs used here are public and unauthenticated.
  Keep the polite User-Agent and inter-request delays in job_monitor.py.
- **LinkedIn: single-posting guest view only.** `job_fetch.py` may read the
  logged-out public guest view of one posting the human explicitly supplied
  (no login, no cookies). Never add LinkedIn search, discovery, or bulk
  scanning; the daily scan stays on the ATS APIs.
- **Accuracy over keyword stuffing in generated documents.** The generator
  selects and emphasizes from master_resume.yaml and must never invent
  experience, employers, metrics, or skills not present there. The tailoring
  note must call out gaps honestly rather than papering over them.
- **Voice: no em dashes, ever.** Use commas, parentheses, or separate sentences.
  Plain English. This applies to generated documents AND to anything written
  for the operator. Enforced in the generator prompt and a post-process safety
  net; keep both.
- **The Fit Gate fails closed.** `gate.py` runs at `queue` and `fetch` and
  blocks every prep artifact until it returns RECOMMEND or PROCEED, or the
  operator records a written override. The decisions, best to worst: RECOMMEND
  (a strong match with no possible gaps, apply), PROCEED, CONDITIONAL (one gap,
  plan it), NEEDS_REVIEW (an unsure item, the operator rules it), DO_NOT_APPLY,
  NOT_REMOTE (skills may fit but the location does not), ERROR. Only RECOMMEND
  and PROCEED draft without an override. Every error path (no API key, an
  unfetchable or empty JD, an unparseable model response, a malformed
  capability ledger) returns ERROR, and ERROR blocks drafting exactly like
  DO_NOT_APPLY. A gate that fails open is not a gate. It grades against the
  WHOLE resume (experience, skills, certs, education), not just the
  `capabilities` highlights, and credits adjacent or transferable experience as
  PARTIAL with a written bridge; NONE is reserved for genuine absence, and a
  stretch bridge is a NONE. When the model genuinely cannot tell, it grades
  NONE and the operator, the final gauge, rules it up.
  Location is a hard overlay: a role that is not remote and not inside the
  operator's `onsite_ok` area is NOT_REMOTE no matter how strong the skills.
  Remote is read from an explicit "Workplace Type" field first, then the
  location field or title, then strict role-specific phrases (never generic
  "work from home" boilerplate). The LLM extracts and classifies; Python decides. The VERDICT
  is one-directional: code can only make it harsher, never softer, and no code
  path upgrades a NONE. The CLASSIFICATION (hard or soft) is adjudicable by
  the operator, but only when the gate itself came back UNSURE (`gate-rule`),
  never when `do_not_claim` forced the requirement (the ledger is absolute, not
  arguable by the model or by the operator), and a human ruling has the same
  net effect on the decision that a softer verdict would, so it is written and
  audited exactly like one. Evidence may never be UNSURE; thin evidence
  always resolves to NONE, or the confirmation bias just relocates.
  `do_not_claim` in master_resume.yaml overrules the model outright and is
  the only thing that can; a missing entry there is a silent hole (on one live
  run the model tried to rationalize "correlation" into a PARTIAL, citing an
  adjacent project as "signal correlation work"; the ledger forced it to NONE).
  The ledger is matched TWICE, because
  matching it only against extracted requirements left a hole: the extractor
  reliably quotes bulleted requirements and reliably drops role-framing prose,
  which is exactly where disqualifying phrasing lives. On one live run
  (2026-08-18) the JD said "This is a player-coach role" verbatim, the
  token had been in the ledger since 2026-07-24, and the gate still returned
  NEEDS_REVIEW because no extracted quote contained it. `ledger_sweep` now
  also runs the tokens over the RAW JD and synthesizes a hard NONE for any
  entry the extraction missed, one per entry rather than per token, skipping
  entries a real requirement already caught so one gap cannot be counted
  twice. It only ever adds hard NONEs, so it stays one-directional like every
  other rule. The guard lives in `job_generate.generate()`,
  not the CLI, so the MCP path and the lead-inbox ingest path
  both inherit it. Do not move it, and do not add a bypass. Nothing unblocks
  the gate without a written record: `gate-override`, `gap-close`, and
  `gate-rule` all demand a mandatory reason or note and are all audited to
  state_log, but that record has a shelf life of exactly one decision. An
  override waives ONE decision, not the job. Any fresh `gate` run, and any
  `gate-rule` recompute, clears it immediately, even when the decision string
  does not change, because the report it was written against is gone. Re-run
  `gate-override` if you still want to draft. Practical consequence: finish
  your `gate-rule` rulings BEFORE you override, not after, or the override is
  discarded. The lead-inbox ingest auto-draft path fires on RECOMMEND or a
  clean PROCEED; a CONDITIONAL or NEEDS_REVIEW will not auto-draft on the cron,
  which is deliberate conservatism on an unattended path.

## Architecture

Eight stages over a shared SQLite system of record:

1. **Discover** - two sources, both landing `discovered` rows.
   - `job_monitor.py` is the DEEP watch. `run_scan(cfg, seen)` is the
     import-safe core: fetches public ATS APIs for the companies in
     companies.yaml, filters by title/location/exclude terms, returns matches.
     One company's bad data must never crash the whole scan (there is a
     catch-all in the per-company loop; keep it).
   - `openjobs.py` is the WIDE net, added because the deep watch has a hard
     ceiling: it can only ever find jobs at the scannable boards already
     listed in companies.yaml (191 of them in the operator's own config). On
     2026-08-31 a hand-run of the open-jobs toolchain produced a
     10-role shortlist and 9 of the 10 were at companies the scanner cannot
     see. `discover(cfg)` ranks a 2.07M-posting public corpus (~65,000 boards)
     against `ideal-jd.md` and returns candidates best-match first. It reaches
     iCIMS, Oracle Cloud, Dayforce, Workable, Paycom, Paylocity, Eightfold and
     Taleo, families with no feed we could scan ourselves.
     It costs NO API spend on a normal run: the embed call is the only request
     carrying our data and fires only when `ideal-jd.md` changes (the vector is
     cached against the sha256 of the text that produced it), ranking is local
     numpy, and group downloads are anonymous static files.
     Discovery fails SAFE, the mirror image of the Fit Gate and for the same
     reason the liveness sweep does: a lead missed today comes back tomorrow, a
     broken run takes the digest with it. Every error path (no JD, a 429, a
     network error, a corrupt group, an unparseable manifest) yields zero
     candidates and a logged reason, and nothing raises into `bin/daily.sh`.
     The group centroid decides WHICH groups to download and the posting's own
     vector ranks inside them; using the centroid for both gave all 15 ingested
     leads an identical score and quietly turned the top-N cap into "the first
     N rows of the nearest group file".
     `company` is stored EXACTLY as the board publishes it, never prettified,
     because it is the uid's middle field and, for greenhouse/lever/ashby/
     smartrecruiters, a path segment in `posting_endpoint`. An earlier version
     cleaned it up and broke two things at once: some Ashby board slugs are
     themselves domain-shaped and the two forms are NOT interchangeable (a
     board published as `umbralabs.com` answers 200 on that exact slug and 404
     on `umbralabs`), so the gate's fetch 404'd and returned ERROR, which
     blocks drafting exactly like DO_NOT_APPLY; and
     `.split(".")[0]` collapsed `careers.acme.com` and
     `careers.beta.com` onto one slug, so two employers sharing a per-tenant
     requisition number produced one uid and the second lead vanished as a
     false duplicate. The readable name lives in its own `company_display`
     column, where a bad guess can only ever cost an ugly card. Nothing that
     names a company for a human may touch a key or a URL.
     Dedup is two layers: uid (free where both crawlers use the same board
     slug) and `jobdb.canonical_url`, which strips scheme, `www.`, host case,
     query and a trailing slash and folds the `job-boards.greenhouse.io` /
     `boards.greenhouse.io` split. It deliberately preserves PATH case, since
     ATS ids are case-sensitive and folding them would turn a dedup miss into
     a silently dropped lead. Note the uid layer does NOT cover Workday: the
     scanner stores those as `{host}/{site}` and the corpus as the bare host,
     so the URL layer is what catches them.
     Wide-net leads on ATS families with no fetcher arrive with their JD text
     attached and are gradeable for the first time; before, `fetch_description`
     had no endpoint for them and the gate returned ERROR on an empty JD. The
     four URL-bearing ATSes are still re-fetched at gate time on purpose, so a
     re-draft picks up an edited JD.
     `discover()` prunes its own group cache to the two most recent builds:
     ~37MB per build, daily, on a host with 8GB free is 13GB a year.
     The ideal-JD vector is cached against (sha256 of the text, corpus recipe,
     dims) and a mismatch on any of the three RE-EMBEDS. Keying on the text
     alone ranked a stale vector against a rebuilt space (confident noise, not
     an empty run), and bailing out instead of re-embedding killed the stage
     permanently the first time dims changed.
     See docs/superpowers/specs/2026-08-31-openjobs-discovery-design.md.
2. **Store** - `jobdb.py`. SQLite is the source of truth. Tables: jobs (with
   lifecycle state + posting date), state_log (audit trail), files (versioned
   document records). State machine is enforced via TRANSITIONS; illegal jumps
   raise TransitionError. Pure data layer, no network or file generation.
3. **Gate** - `gate.py`. Fails closed before any prep artifact exists. One LLM
   call (`extract`) reads the full JD against the capabilities in
   master_resume.yaml and proposes a verdict per requirement; Python
   (`enforce`, `decide`) applies the one-directional demotion rules and
   renders the actual decision (RECOMMEND, PROCEED, CONDITIONAL, NEEDS_REVIEW,
   DO_NOT_APPLY, NOT_REMOTE, ERROR), then a location overlay downgrades a
   non-remote role to NOT_REMOTE. `ledger_sweep` runs the `do_not_claim`
   tokens over the raw JD before `enforce`, so prose the extractor skipped
   still trips the ledger. Runs at `queue` and `fetch`; the decision, the JSON,
   and the rendered fit report are persisted on the job row so `draft` never
   re-calls the model. See docs/superpowers/specs/2026-07-14-fit-gate-design.md.
4. **Generate** - `job_generate.py`. Fetches the full JD, calls the Anthropic
   API with master_resume.yaml + JD, writes versioned DOCX (+ PDF if
   LibreOffice present) into a standardized package folder, records files.
   `generate()` calls `gate.require_pass()` as its first step, the single
   choke point every artifact passes through.
5. **Track** - state transitions via the CLI. Human applies, then marks state.
6. **Freshness** - `freshness.py`. Computes posting age and labels its
   provenance honestly (true date vs approximate). See "Posting dates" below.
7. **Interview board** - `board.py` plus `jh rounds` / `jh stage` / `jh board`.
   `interviewing` is one flat state, so a job in its final round and a job
   awaiting a decision are otherwise identical rows. Each job carries its OWN
   ordered round list (free-text labels, JSON on the row) plus a marker. The
   base frame every application starts on is
   `recruiter | round 1 | round 2 | round 3 | decision`, seeded from
   `DEFAULT_ROUNDS`, with `decision` appended at render time. The generic
   node caption is DERIVED from position (recruiter, round 1..N, decision) and
   never stored, so a relabelled round can no longer put a label naming one
   number into a slot holding another; the stored label becomes the small-print
   detail under it (who was actually in the round). A detail identical to its
   caption, and any generic `round N` placeholder, is dropped rather than
   printed under a caption it may disagree with. `DEFAULT_ROUNDS` seeds
   NUMBERLESS placeholders (`recruiter, round, round, round`) for the same
   reason: positions are 1-based over the whole list and captions skip the
   recruiter screen, so a seeded "round 1" sat in position 2 and `jh stage
   <ident> 2` reported a different number than the board drew. All three
   surfaces read one derivation now: `jh stage` takes its caption from
   `board.captions`, and `jh rounds` prints the list POSITION (what `jh stage`
   takes) with an unfilled placeholder shown as `(unnamed)` rather than
   verbatim, via `board.is_placeholder`. That last one matters because there
   is no migration: every job already staged carries the old `round 1`..`round
   3` seed, so `jh rounds` would otherwise still print `2. round 1`. A loop
   that runs two rounds or
   five just edits its own list. Round order is never
   modelled globally: on one live loop, round 3 was booked as the
   technical and became a peer plus a TPM because of interviewer availability,
   so any fixed ladder is wrong the first time somebody takes vacation.
   `decision` is a render-time terminal node, never stored, because the outcome
   already lives in `state` + `outcome` and a second lifecycle would drift from
   the first. `board.py` is pure rendering: rows in, HTML string out, no network
   and no model call.
8. **Liveness** - `liveness.py` plus `jh prune`. Asks the public ATS endpoint
   whether a posting is still up, with one unauthenticated GET and no model
   call, so dead leads are cleared before the gate spends an LLM call finding
   out the expensive way. `check()` returns open, closed, or unknown.

**The Fit Gate fails closed; the liveness sweep fails safe, and they are
mirror images.** Marking a live posting closed silently removes a real
opportunity from the pipeline, and no later stage would ever surface it
again, so every uncertain case resolves to `unknown`: no public endpoint, a
network error, a timeout, a 5xx, a 429, an unparseable body, an unrecognized
payload shape. `--apply` acts only on `closed`, never on `unknown` and never
on `open`. Only two things earn a `closed`: a 404 or 410, and an ashby board
that is healthy AND populated and does not list this posting's id (an empty
`jobs` list is a paused or errored board, not 300 dead postings). Do not add
a `closed` path for a shape nobody has observed; an `open` or an `unknown`
costs one stale row, a wrong `closed` costs a job. The endpoint comes from
`job_generate.posting_endpoint`, never a second copy of those URLs.

`job_cli.py` is the command surface and the only place that wires the stages
together and owns state persistence. Keep run_scan and generate side-effect
free so a future GUI or skill can call them directly. The reusable cores
`scan_and_ingest` and `refine_pipeline` live here too (they return data and do
no printing) so the CLI and the MCP adapter drive one code path.

`job_hound_mcp.py` exposes the pipeline as an MCP server, so the search can be
driven from a chat client. It is a thin adapter: every tool
calls the existing stage code and returns plain dicts. Keep it thin, and keep
the hard rule in the surface itself: no submit/fill/login tool, and `job_apply`
stamps state only. Deploy and wiring are covered by the MCP runbook in `docs/`.

`job_ingest.py` drains lead-inbox submissions (fetch JD, LLM fit verdict,
auto-draft above threshold). Its auto-draft path calls `gate.run_gate()`
itself before calling `job_generate.generate()`, so a bad lead never reaches
generation at all; `generate()`'s own `require_pass()` check still runs
underneath it as the backstop.

`jobapi.py` is the local write API (FastAPI, 127.0.0.1, bearer token) that the
lead inbox UI writes through. Every endpoint is a thin wrapper
over an audited `jobdb.py` setter, so `jobdb.py` stays the only writer and the
state machine lives in one language. `GET /jobs/{ident}/transitions` exists so
the UI never duplicates `TRANSITIONS`. It returns the true state machine, which
is not the same as the list of buttons to draw: the inbox offers only `queued`,
`skipped`, and `discovered`, because `drafted` and `ready` mean documents exist
on disk and belong to the draft pipeline rather than to triage. It has
no endpoint that submits, fills, or logs in, and must never get one. Runbook:
deploy/README-job-api.md.

## Commands

```
python job_cli.py scan                  discover + ingest (default companies.yaml)
python job_cli.py -c test.yaml scan     scan an alternate config
python job_cli.py openjobs [--top N] [--groups N]
                                        wide-net discovery over the open-jobs
                                        corpus, ranked against ideal-jd.md. No
                                        LLM and no API spend. Ingests at most
                                        --top (default 15) NEW leads per run;
                                        dedup runs before the cap, so a day
                                        whose best matches are all already
                                        known still ingests 15 fresh ones.
                                        --top 0 means ZERO, not unlimited: it
                                        is a budget on rows written to the
                                        system of record, unlike refine's
                                        --top 0, which is a display limit
python job_cli.py fetch <url>           ingest one posting by URL -> queued, runs the fit gate
                                        (LinkedIn, ATS link incl. Workday, or
                                        JSON-LD page)
python job_cli.py list [--state S]      pipeline view, fresh-only by default
python job_cli.py list --all            include stale + undated
python job_cli.py prune [--state S] [--limit N] [--apply]
                                        check postings for liveness (no LLM, no API
                                        spend); dry run unless --apply, which marks
                                        only closed postings skipped, oldest first
python job_cli.py show <ident>          detail + files + history
python job_cli.py queue <ident>         mark to pursue (runs the fit gate)
python job_cli.py gate <ident>          re-run the fit gate against a posting
python job_cli.py gate-rule <ident> <n> --hard|--soft --note "..."
                                        rule on an UNSURE requirement (--note mandatory),
                                        recomputes free; refuses a confident or ledger-forced item
python job_cli.py gate-override <ident> --reason "..."
                                        bypass a blocked gate (--reason mandatory)
python job_cli.py gaps [<ident>]        open gaps, all or for one job
python job_cli.py gap-plan <id> --plan "..." --hours N --deadline YYYY-MM-DD
python job_cli.py gap-close <id> --reason "..."
                                        close a gap (--reason mandatory)
python job_cli.py draft <ident>         generate tailored package -> drafted
python job_cli.py refine            score + rank leads, push Discord digest
python job_cli.py refine --no-llm   deterministic scoring only (no API spend)
python job_cli.py rounds <ident> ["a,b,c"] [--add "..."]
                                        show or set a job's interview rounds
python job_cli.py stage <ident> <n|decision> [--next "..."] [--on DATE]
                                        move the interview marker; transitions
                                        applied -> interviewing on the way.
                                        --on YYYY-MM-DD is when the round really
                                        happened (default today), and it is what
                                        the quiet clock measures from
python job_cli.py board [--open]        render live loops to interviews.html
python job_cli.py ready <ident>         reviewed, ready to submit
python job_cli.py next                  next job to apply, with link + folder
python job_cli.py apply <ident>         mark applied (date stamped)
python job_cli.py state <ident> <S>     set state directly (validated)
python job_cli.py close <ident> --outcome <o>
python job_cli.py stats                 pipeline counts
```

`<ident>` resolves a full uid, a slug, or a unique slug prefix.
States: discovered, queued, drafted, ready, applied, interviewing, closed, skipped.
`closed` is terminal for an outcome that was actually DECIDED (rejected,
withdrawn, offer, accepted). A `ghosted` close is the one exception and reopens
to `applied` or `interviewing`, because ghosted means "they stopped replying",
not "they said no" (live case 2026-08-31: an employer went quiet after the
recruiter screen, the lead was closed ghosted on 08-20, and the recruiter came
back on 08-31 to book round 1). Reopening clears `outcome`, `closed_at`, and
`close_reason`, but
deliberately does NOT restamp `applied_at`: the Reply Window measures employer
response time from it, so moving it would turn a 13-day silence into an instant
reply. The close itself stays in state_log, which is where the silence is
remembered. An `outcome` is refused for any destination but `closed` (a
TransitionError in `jobdb`, a 400 in `jobapi`, mirroring how `reason` is
already handled): the reopen guard DECIDES from `outcome`, so a live row left
carrying `ghosted` would stay reopenable forever. Ask `jobdb.next_states(row)`
rather than reading `TRANSITIONS`
directly when you need what one row can do next; `GET /jobs/{ident}/transitions`
does, so the inbox never draws a reopen button on a rejected lead.
A lead is unread until the operator disposes of it (`read_at IS NULL`). Read
state is set from the lead inbox UI, not the CLI.

The daily run on the deployment host is `bin/daily.sh` (cron): scan, then the
open-jobs wide net (`openjobs --top 15`, guarded by `OPENJOBS_ENABLED` /
`OPENJOBS_TOP`), then on Sunday only the weekly liveness sweep (`prune
--apply`, guarded by `PRUNE_DAY` / `PRUNE_ENABLED`), then `refine --no-llm
--top 0 --digest`.

The wide net runs as its OWN process, so its outcome reaches the digest through
a status file (`openjobs-cache/last-run.json`) rather than a return value, and
the digest carries one `Wide net:` line reporting it. That line exists because
failing safe is not the same as failing visibly: on 2026-09-01 the corpus
`/embed` endpoint was down, the stage correctly produced nothing and exited 0,
and the only trace was a line in daily.log nobody reads. It reports in BOTH
directions (`12 new from 252 candidates` / `unavailable (...)`), because a line
only on failure leaves "working" and "never deployed" looking identical. With
no status file at all the digest is byte-for-byte unchanged. The wide net and the sweep are both isolated from `set -e`
for the same reason: they are nice-to-haves and the digest is not. The unattended digest
is deliberately deterministic and free. See the host deploy runbook in `docs/`.

## Git workflow and releases

GitHub Flow (the repo dropped git-flow and deleted `develop` on 2026-07-05):
`main` plus short-lived `feature/*`, `fix/*`, `chore/*` branches. `main` is
protected: changes land only through PRs with a passing `checks` check, never
direct pushes. Delete-branch-on-merge is enabled on GitHub; delete the local
branch after merging and `git fetch --prune`. Abandoned-but-worth-keeping work
gets an `archive/<name>` tag on its tip before its branch is deleted (see
`archive/apply-next-vote`).

Cutting a release: merge everything intended for the release into `main`
through normal PRs, then tag:

```
git checkout main && git pull --ff-only origin main
git tag -a vX.Y.Z -m "Release vX.Y.Z - ..." main
git push origin vX.Y.Z
```

Gotcha: `git reset --hard` is blocked by a safety hook (use `git branch -f`
to move a ref instead). v0.1.0 was the first tag.

The deployed checkout on the deployment host (`~/job-hound`) tracks
`main`; deploy with `git pull --ff-only origin main` there after merging.

## One database, on the deployment host

There is exactly ONE `jobs.db`: `$JOB_HOST:~/job-hound/jobs.db`. The daily
scan, the digest, the lead inbox and the MCP server all read and write
that one file.

**Never run `python job_cli.py` directly on your workstation.** With `JOB_DB`
unset and
no `jobs.db` in the working directory it now refuses to start and points you at
`bin/jh` (`jobdb.resolve_db_path`), but if you set `JOB_DB` or drop a `jobs.db`
in the checkout it will happily use that instead, and that is a second,
divergent database. Drive the host:

```
bin/jh list --state applied     any job_cli command, run against the host
bin/jh draft <ident>            package is generated ON the host
bin/jh-pull                     rsync the packages down locally to submit
bin/jh apply <ident>
```

**Copying that file is not `cp jobs.db` any more.** It runs in WAL, so
committed rows can be sitting in `jobs.db-wal` while `jobs.db` itself looks
almost empty, and a copy of `jobs.db` alone can be an incomplete database. Use
`sqlite3 ~/job-hound/jobs.db ".backup out.db"`, which folds the WAL in, or copy
`jobs.db*` so the `-wal` and `-shm` sidecars come along. This applies to
backups and to pulling a copy down for local work.

**Ad-hoc `sqlite3` queries need a timeout now.** Everything in Python opens the
database with `busy_timeout = 5000` and waits its turn, but the `sqlite3`
command line tool defaults to a timeout of 0 and gives up the instant another
process holds the write lock. The ingest timer takes that lock every five
minutes, so a bare `sqlite3 jobs.db "SELECT ..."` fails with
`database is locked` at roughly that cadence. Observed live on 2026-07-25, two
queries out of four during a timer tick. Use:

```
sqlite3 -cmd ".timeout 5000" ~/job-hound/jobs.db "SELECT ..."
```

`bin/jh` and anything going through `JobDB` are unaffected.

For any "what's my status / how many have I applied to" question, **read the
host**. Until 2026-07-11 a local working copy was merged up by a scheduled
sync; that merge resolved conflicts on `updated_at`, which the host's nightly
scoring
pass bumps without changing state, so a locally recorded `apply` could be
silently discarded and never recover. Both the second DB and the sync are gone.
Full writeup: docs/single-source-of-truth.md.

## Deploying the Fit Gate

The additive schema migration (`gate_decision`, `gate_json`, `gate_report_path`,
`gate_at`, `gate_override_reason`, `gate_overridden_at` on jobs, plus the whole
gaps table) runs automatically the next time `JobDB` opens on the host, no
manual step needed.

After that migration, `gate_decision` is NULL on every row that existed before
the deploy, which was all 363 rows in the production DB at the time.
`require_pass()`
blocks drafting on a NULL decision exactly like any other fail-closed path, so
after deploy, every existing job actually worth drafting needs to be gated
once (`bin/jh gate <ident>`, one API call) before `bin/jh draft` will work on
it. There is deliberately NO backfill: gating hundreds of jobs, most of which
will never be pursued, is exactly the waste this design exists to avoid.

Deploy as usual: merge to `main` via PR, then on the host
`git pull --ff-only origin main`.

## Environment

```
ANTHROPIC_API_KEY   required for draft (keep it in your shell env, never in the
                    repo)
JOB_DB              SQLite path. Required unless a jobs.db already exists in
                    the working directory; there is no per-user default and
                    nothing invents one. On the host this is ~/job-hound/jobs.db
                    (bin/jh and bin/daily.sh set it). Do not point it at a path
                    on your workstation.
JOB_APPS_DIR        application packages root (default ~/job-applications)
JOB_CONTACT_EMAIL   contact address advertised in the polite User-Agent that
                    job_monitor.py sends, so a board operator can reach you
JOB_FIT_MODEL       optional ranking model (default claude-haiku-4-5)
JOB_GATE_MODEL      optional gate model (default claude-opus-4-8)
JOB_DRAFT_MODEL     optional drafting model (default claude-opus-4-8)
JOB_MODEL           global model override; component variables win when both set
JOB_LLM_USAGE_LOG   usage log path (default
                    ${LOG_DIR:-$HOME/logs}/job-hound/llm-usage.log)
JOB_PDF=off         skip PDF generation
JOB_IDEAL_JD        wide-net query document (default ./ideal-jd.md)
JOB_OPENJOBS_CACHE  manifest/centroid/group cache (default beside jobs.db)
JOB_OPENJOBS_URL    corpus endpoint (default the public open-jobs worker)
OPENJOBS_ENABLED=0  switch the wide net off in bin/daily.sh
OPENJOBS_TOP        new leads the wide net may ingest per run (default 15)
OPENJOBS_TIMEOUT    wall-clock cap on the wide net in daily.sh (default 10m)
JOB_HOST            ssh target for the deployment host (no default; bin/jh and
                    bin/jh-pull need it)
JOB_API_TOKEN       bearer token for the local write API (host env file only)
JOB_API_PORT        write API port (default 8765)
PATH                LibreOffice (soffice) must be on PATH for PDFs
```

Run from the venv: `source .venv/bin/activate`. Deps in requirements.txt.

## File and folder conventions

Package folder per application:
```
<JOB_APPS_DIR>/2026-06-16__company__role-slug__shortid/
    <Name>_<Role>_Resume_v1.docx
    <Name>_<Role>_Cover_Letter_v1.docx
    (PDFs alongside if LibreOffice present)
    job-description.md
    tailoring-note.md
```
`<Name>` comes from master_resume.yaml. Recruiter-facing filenames are
Name_Role_DocType_vN: name first (findable in a
downloads folder), role next (signals tailoring), no company in the filename
(the folder carries it), version as a strippable suffix. Re-drafting bumps the
version; nothing is overwritten. Note: ATS systems parse file CONTENTS, not
filenames, so the filename convention is for the human recruiter and for the
operator's own organization, not for ATS ranking. Do not claim otherwise.

`ideal-jd.md` at the repo root is the wide net's query: prose in the shape of a
real posting, because it is embedded into the same vector space as real
postings. It says what the operator WANTS; `master_resume.yaml` says what they
HAVE, and
they are deliberately different documents (the resume records exposure that
`do_not_claim` forbids claiming, and says nothing about the salary floor or the
on-site radius, which live in `profile.yaml`). The live copy is gitignored, but
keep it under version control of your own,
and note that the cached vector records the
exact text that produced it, because a 2026-08-31 hand-run of the upstream
toolchain left two ideal-JD versions on disk with different salary targets
and no record of which one produced the shortlist.

## Posting dates (freshness)

Date quality varies by ATS and the code is deliberately honest about it:
- lever (createdAt), ashby (publishedAt), smartrecruiters (releasedDate): true
  first-posted dates, reliable.
- greenhouse: the board listing only has updated_at (changes on any edit, so
  approximate, flagged with ~). At scan time the code fetches the per-job
  first_published to upgrade it when possible.

Policy: undatable postings are KEPT and labeled "age unknown", never silently
dropped. So are committed leads (queued, drafted, ready, interviewing), at any
posting age: posting age triages the discovery firehose, and a lead the
operator
already decided to pursue is not the firehose. `--limit` is a budget on
discoveries for the same reason, so a limited list can exceed it by the
committed set. All three surfaces (`bin/jh list`, the MCP `job_list`, and the
lead inbox UI) apply both rules the same way and have to stay in agreement.
Default views hide roles older than 48h; --all overrides; --max-age
tunes the window. Age is a freshness signal, not a hard quality gate: a strong
role posted 4 days ago is still worth applying to.

## Known rough edges

- Company slugs in companies.yaml are educated guesses. Wrong ones return
  404/403 (logged, scan continues). Confirm against the real careers page.
- Filters are still loose. "infrastructure" and bare "cloud" pull in legal,
  tax, comms, PM, and procurement roles. Tightening title_terms and
  exclude_terms is an open task.
- Lever, Ashby, SmartRecruiters fetchers were written to documented response
  shapes; only Greenhouse and SmartRecruiters have been exercised against live
  data so far. SmartRecruiters `ref` can be a string or a dict (handled).
- Wide-net leads on ATS families the scanner has no fetcher for (oraclecloud,
  dayforce, workable, paycom, paylocity, eightfold, breezy, recruitee, crelate,
  personio, pinpoint) have no `posting_endpoint`, so `prune` returns `unknown`
  for them and never marks them skipped. That is the fail-safe design working
  as intended, not a bug: a wrong `closed` costs a job. They age out of default
  views on freshness instead.
- Wide-net posting dates come from `seen`, the crawler's first-seen timestamp,
  not a true first-published date. Labelled `openjobs:first_seen~`, so the `~`
  marks it approximate exactly like the Greenhouse case.
- A handful of Paycom and Paylocity postings publish an opaque id and no
  company name anywhere in the record, so their `company` stays that id. Ugly
  and unique beats pretty and wrong; there is nothing to derive a name from.
- Workday IS scannable via the unauthenticated cxs endpoint when the entry has
  a `{host}/{site}` slug; an optional `search_text` narrows server-side for
  oversized tenants whose sites list far more postings than one scan should
  page through. A pasted
  `*.myworkdayjobs.com` posting URL is PARSED into that same `{host}/{site}`
  plus externalPath pair rather than scraped, so `fetch` and the scanner mint
  one uid for one posting and `prune` can ask the cxs endpoint about it. It
  has to be parsed, because the generic JSON-LD fallback silently gets the
  location wrong: on a live Workday posting (2026-09-02) the
  page's schema.org blob read "<employer name>, United States of America" while
  the cxs endpoint published "Remote - United States", and the gate's location
  overlay turned that into NOT_REMOTE on a genuinely remote, strong-fit role.
  A Workday tenant behind a vanity domain still falls through, since nothing
  in such a URL names its site slug. iCIMS, Phenom, and
  Avature have no public feed; those companies are listed as "manual" and
  surfaced for hand-checking or the Google Custom Search route.

## When changing things

- Keep run_scan and generate import-safe and side-effect-free.
- Validate any DB schema change against an existing jobs.db, or tell the
  operator to delete and re-scan (early in a search, a clean rebuild is usually
  fine).
- Don't reach for new dependencies casually; this is meant to stay a small,
  legible CLI that can grow into a skill or app later.
- Test parsing/filter/state changes with mocked data before claiming they work.
- `conftest.py` isolates `JOB_APPS_DIR` and `JOB_DB` for the whole suite
  (autouse fixtures), so tests can never write into the real
  `~/job-applications` or create a second `jobs.db` in the repo. Keep both
  fixtures if you touch test setup; they are the reason the suite is safe to
  run against this repo at all.

## Deploying (automated)

Merging to `main` deploys job-hound to the deployment host automatically. A
self-hosted GitHub Actions runner registered to this repo
runs `.github/workflows/deploy.yml`, which pulls, installs deps if
`requirements.txt` changed, restarts `job-api.service` and retires stale
MCP processes if any `.py` changed, then verifies the write API answers and
the database opens. There is no manual `git pull` step any more, and no step
that depends on remembering it.

Two deliberate choices in that workflow. It restarts on ANY `.py` change
rather than mapping files to the services that own them, because `jobapi`
imports `jobdb` and the MCP imports `job_cli` which imports six more modules,
so a file-to-service map would drift; a restart costs seconds and a service
silently running stale code cost two days once already. And it uses
`git merge --ff-only`, not `reset --hard`, because that checkout sits beside
`jobs.db`.

Gotcha worth knowing if you ever retire an MCP process by hand: `pkill -f
job_hound_mcp.py` also matches the shell running it and kills its own job.
Use `pkill -f 'job_hound[_]mcp'`.

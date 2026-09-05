# Daily Fit-Ranking + Feedback Priming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a daily, unattended discovery+ranking pass that scores every lead for fit, learns from the operator's accept/skip decisions, and pushes a ranked digest to Discord.

**Architecture:** A new `fit.py` module holds a free deterministic scorer (`score`), a decision-history corpus builder (`build_history`), an LLM verdict tier (`verdict`) that reuses the existing Anthropic call pattern, and pure digest formatting. `jobdb.py` gains nullable scoring/reason columns. `job_cli.py` gains a `refine` command, `--reason` capture on skip/close, and fit-ordered `list`. A small `notify.py` posts the digest to a Discord incoming webhook. A `bin/daily.sh` wrapper runs scan+refine on the always-on tools host via cron.

**Tech Stack:** Python 3, SQLite (stdlib `sqlite3`), `requests`, `pyyaml`, `pytest` (new dev dep). No new runtime dependencies beyond what `job_generate.py` already uses.

## Global Constraints

- **Discovery and prep only, never auto-apply.** No feature here submits, fills forms, or logs in.
- **Public endpoints only.** Reuse `job_generate.fetch_description`; keep the polite User-Agent and existing request behavior.
- **No invented experience.** The LLM verdict reads `master_resume.yaml` + the JD only; it judges fit, it does not write application content.
- **Voice: no em dashes, ever.** Applies to the digest text and every string this code emits. Use commas, parentheses, or separate sentences.
- **Keep `run_scan` and `generate` import-safe and side-effect-free.** New `fit.py` functions follow the same rule: pure where possible, I/O injected.
- **Clean rebuild, not migration.** New columns are added to `SCHEMA`; the rollout deletes `jobs.db` and re-scans. No `ALTER TABLE`.
- **Don't add runtime dependencies casually.** `pytest` is the only new dependency and it is dev-only.
- Model id default stays `claude-opus-4-8` via `JOB_MODEL` (matches `job_generate.MODEL`).

---

### Task 1: Test scaffolding + schema columns

**Files:**
- Create: `requirements.txt` (currently empty/absent — establish it)
- Modify: `jobdb.py` (the `SCHEMA` string, lines 89-138)
- Test: `test_jobdb_schema.py`

**Interfaces:**
- Consumes: existing `JobDB(path)` constructor, `JobDB.set_fields(uid, **fields)`, `JobDB.upsert_job(job)`.
- Produces: `jobs` table now has nullable columns `fit_score INTEGER`, `fit_reasons TEXT`, `llm_fit_score INTEGER`, `llm_rationale TEXT`, `llm_coding_bar TEXT`, `skip_reason TEXT`, `close_reason TEXT`. All readable via `row["<col>"]` (None when unset).

- [ ] **Step 1: Establish dev dependency**

Write `requirements.txt` (it is currently empty). Include the runtime deps `job_generate.py` already imports plus pytest:

```
anthropic
python-docx
requests
pyyaml
pytest
```

Then install pytest into the venv:

Run: `.venv/bin/pip install pytest`
Expected: `Successfully installed pytest-...`

- [ ] **Step 2: Write the failing test**

Create `test_jobdb_schema.py`:

```python
import jobdb


def test_new_scoring_columns_exist_and_are_settable(tmp_path):
    db = jobdb.JobDB(tmp_path / "t.db")
    db.upsert_job({
        "id": "1", "ats": "greenhouse", "company": "acme",
        "title": "Solutions Architect", "location": "Remote", "url": "http://x",
    })
    uid = jobdb.make_job_uid("greenhouse", "acme", "1")

    db.set_fields(
        uid,
        fit_score=72, fit_reasons="title:strong; remote",
        llm_fit_score=80, llm_rationale="strong SA fit",
        llm_coding_bar="light", skip_reason="", close_reason="",
    )
    row = db.get(uid)
    assert row["fit_score"] == 72
    assert row["fit_reasons"] == "title:strong; remote"
    assert row["llm_fit_score"] == 80
    assert row["llm_rationale"] == "strong SA fit"
    assert row["llm_coding_bar"] == "light"
    # Unset columns default to NULL on a fresh row.
    db.upsert_job({
        "id": "2", "ats": "greenhouse", "company": "acme",
        "title": "Engineer", "location": "Remote", "url": "http://y",
    })
    row2 = db.get(jobdb.make_job_uid("greenhouse", "acme", "2"))
    assert row2["fit_score"] is None
    db.close()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/pytest test_jobdb_schema.py -v`
Expected: FAIL with `sqlite3.OperationalError: no such column: fit_score`

- [ ] **Step 4: Add the columns to SCHEMA**

In `jobdb.py`, inside the `CREATE TABLE IF NOT EXISTS jobs (...)` block, add the new columns immediately after the `notes TEXT,` line (line 107):

```python
    notes         TEXT,
    fit_score     INTEGER,                -- deterministic 0-100 fit score
    fit_reasons   TEXT,                   -- short string: what drove fit_score
    llm_fit_score INTEGER,                -- 0-100 from the LLM verdict tier
    llm_rationale TEXT,                   -- one-line LLM fit rationale
    llm_coding_bar TEXT,                  -- LLM read of the hands-on coding bar
    skip_reason   TEXT,                   -- structured reason captured on skip
    close_reason  TEXT,                   -- structured reason captured on close
    discovered_at TEXT NOT NULL,
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest test_jobdb_schema.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add requirements.txt jobdb.py test_jobdb_schema.py
git commit -m "[jobdb]: add fit-score and feedback-reason columns + test scaffold"
```

---

### Task 2: Deterministic scorer + profile.yaml

**Files:**
- Create: `profile.yaml`
- Create: `fit.py`
- Test: `test_fit_score.py`

**Interfaces:**
- Consumes: a job as a plain `dict` (callers pass `dict(row)`), with keys `title`, `location`, `description` (may be `""`), `salary_min` (may be `None`); a `profile` dict loaded from `profile.yaml`.
- Produces:
  - `fit.load_profile(path=None) -> dict` — loads `profile.yaml` (default: alongside `fit.py`).
  - `fit.score(job: dict, profile: dict) -> tuple[int, str]` — returns `(0..100, reasons_string)`. Deterministic, no network. `reasons_string` is `"; "`-joined, no em dashes.

- [ ] **Step 1: Write `profile.yaml`**

```yaml
# profile.yaml - fit-scoring weights and targets for ranking leads.
# Hand-tunable. Read by fit.score(). Separate from companies.yaml (scan targets)
# and master_resume.yaml (source experience).

# Title families. A title matching a "strong" term scores higher than "good".
target_titles:
  strong:
    - solutions architect
    - principal
    - staff
    - engineering manager
    - senior manager
    - director
    - architect
  good:
    - platform lead
    - cloud lead
    - sre lead
    - devops lead
    - infrastructure lead
    - team lead
    - tech lead

# Location tokens that mean remote-US-eligible, and the on-site area the operator accepts.
remote_ok: [remote, anywhere, distributed, united states, "u.s."]
onsite_ok: [portland, beaverton]

# Salary floor. Only applied when the listing carries a salary; absent = neutral.
salary_floor: 150000

# Heavy hands-on-coding markers (the Acme Scheduling lesson). Each hit penalizes.
coding_bar_markers:
  - codes daily
  - hands-on coding
  - strong programming
  - proficient in go
  - proficient in rust
  - write production code
  - software development engineer

# Hard-noise markers that should sink a lead.
exclude_markers:
  - hardware
  - manufacturing
  - security clearance
  - active clearance

# Scoring weights. Base score is 30; signals add or subtract; result clamps 0..100.
weights:
  title_strong: 40
  title_good: 25
  remote: 20
  onsite: 10
  no_location_match: -20
  salary_meets: 15
  salary_below: -15
  coding_penalty: -15
  exclude_penalty: -40
```

- [ ] **Step 2: Write the failing test**

Create `test_fit_score.py`:

```python
import fit

PROFILE = fit.load_profile()  # uses the real profile.yaml


def _job(title, location="Remote", description="", salary_min=None):
    return {"title": title, "location": location,
            "description": description, "salary_min": salary_min}


def test_strong_remote_title_scores_high():
    score, reasons = fit.score(
        _job("Staff Solutions Architect, Enterprise"), PROFILE)
    assert score >= 80
    assert "title" in reasons
    assert "remote" in reasons


def test_heavy_coding_ic_role_scores_low():
    # The Acme Scheduling lesson: a hands-on IC role with coding markers must sink.
    job = _job("Senior Software Development Engineer",
               description="You will write production code and code daily.")
    score, _ = fit.score(job, PROFILE)
    assert score < 50


def test_excluded_hardware_role_is_penalized():
    job = _job("Solutions Architect",
               description="Defense hardware manufacturing program.")
    score, reasons = fit.score(job, PROFILE)
    assert "exclude" in reasons
    assert score < 60


def test_missing_salary_is_neutral_not_penalized():
    with_sal = fit.score(_job("Solutions Architect", salary_min=160000), PROFILE)[0]
    no_sal = fit.score(_job("Solutions Architect"), PROFILE)[0]
    below = fit.score(_job("Solutions Architect", salary_min=120000), PROFILE)[0]
    assert with_sal > no_sal > below


def test_non_remote_non_onsite_is_penalized():
    remote = fit.score(_job("Solutions Architect", location="Remote"), PROFILE)[0]
    nyc = fit.score(_job("Solutions Architect", location="New York, NY"), PROFILE)[0]
    portland = fit.score(_job("Solutions Architect", location="Portland, NC"), PROFILE)[0]
    assert remote > portland > nyc


def test_score_is_clamped_and_int():
    score, _ = fit.score(_job("Principal Staff Director Architect"), PROFILE)
    assert isinstance(score, int)
    assert 0 <= score <= 100


def test_reasons_have_no_em_dash():
    _, reasons = fit.score(_job("Solutions Architect"), PROFILE)
    assert "—" not in reasons
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/pytest test_fit_score.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'fit'`

- [ ] **Step 4: Write `fit.py` with `load_profile` and `score`**

```python
#!/usr/bin/env python3
"""
fit.py - lead fit-ranking.

Two tiers, mirroring the side-effect-free convention of run_scan/generate:

  score()   - deterministic, free, no network. Ranks every lead from title,
              location, salary, and noise markers. Runs on every refine.
  verdict()  - LLM tier (added in a later task). Reads the full JD and the
              candidate's decision history, returns a fit verdict. Top-N only.

score() takes a plain dict (callers pass dict(row)) so it stays trivially
testable. No em dashes in any emitted string.
"""

from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent


def load_profile(path=None):
    p = Path(path) if path else (HERE / "profile.yaml")
    return yaml.safe_load(p.read_text())


def _haystack(job):
    return " ".join([
        (job.get("title") or ""),
        (job.get("location") or ""),
        (job.get("description") or ""),
    ]).lower()


def score(job, profile):
    """Return (int 0..100, reasons string). Deterministic, no network."""
    w = profile["weights"]
    title = (job.get("title") or "").lower()
    location = (job.get("location") or "").lower()
    hay = _haystack(job)
    reasons = []
    total = 30  # base

    tt = profile["target_titles"]
    if any(t in title for t in tt["strong"]):
        total += w["title_strong"]; reasons.append("title:strong")
    elif any(t in title for t in tt["good"]):
        total += w["title_good"]; reasons.append("title:good")

    if any(t in location for t in profile["remote_ok"]):
        total += w["remote"]; reasons.append("remote")
    elif any(t in location for t in profile["onsite_ok"]):
        total += w["onsite"]; reasons.append("onsite-NC")
    else:
        total += w["no_location_match"]; reasons.append("location:no-match")

    sal = job.get("salary_min")
    if sal:
        if sal >= profile["salary_floor"]:
            total += w["salary_meets"]; reasons.append("salary:meets")
        else:
            total += w["salary_below"]; reasons.append("salary:below")

    coding_hits = [m for m in profile["coding_bar_markers"] if m in hay]
    if coding_hits:
        total += w["coding_penalty"] * len(coding_hits)
        reasons.append(f"coding-bar:{len(coding_hits)}")

    if any(m in hay for m in profile["exclude_markers"]):
        total += w["exclude_penalty"]; reasons.append("exclude-marker")

    total = max(0, min(100, total))
    return int(total), "; ".join(reasons)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest test_fit_score.py -v`
Expected: PASS (7 passed)

- [ ] **Step 6: Commit**

```bash
git add profile.yaml fit.py test_fit_score.py
git commit -m "[fit]: deterministic fit scorer + tunable profile.yaml"
```

---

### Task 3: Decision-history corpus builder

**Files:**
- Modify: `fit.py`
- Test: `test_fit_history.py`

**Interfaces:**
- Consumes: a `JobDB` instance (uses `db.conn.execute`); the columns from Task 1 (`skip_reason`, `close_reason`) and existing `state`, `outcome`, `notes`, `updated_at`.
- Produces: `fit.build_history(db, limit=20) -> list[dict]`. Each item: `{"title": str, "company": str, "decision": "pursued"|"rejected", "reason": str}`. Ordered most-recently-decided first, capped at `limit`.

- [ ] **Step 1: Write the failing test**

Create `test_fit_history.py`:

```python
import jobdb
import fit


def _seed(db, ext, title, company="acme"):
    db.upsert_job({"id": ext, "ats": "greenhouse", "company": company,
                   "title": title, "location": "Remote", "url": "http://x"})
    return jobdb.make_job_uid("greenhouse", company, ext)


def test_build_history_buckets_pursued_and_rejected(tmp_path):
    db = jobdb.JobDB(tmp_path / "t.db")
    q = _seed(db, "1", "Solutions Architect")
    db.set_state(q, "queued")

    s = _seed(db, "2", "Senior SRE")
    db.set_state(s, "skipped")
    db.set_fields(s, skip_reason="too code-heavy")

    db.upsert_job({"id": "3", "ats": "greenhouse", "company": "acme",
                   "title": "Untriaged", "location": "Remote", "url": "http://z"})

    hist = fit.build_history(db)
    by_title = {h["title"]: h for h in hist}

    assert by_title["Solutions Architect"]["decision"] == "pursued"
    assert by_title["Senior SRE"]["decision"] == "rejected"
    assert by_title["Senior SRE"]["reason"] == "too code-heavy"
    # Untriaged (still 'discovered') is not a decision, so it is excluded.
    assert "Untriaged" not in by_title
    db.close()


def test_build_history_respects_limit(tmp_path):
    db = jobdb.JobDB(tmp_path / "t.db")
    for i in range(25):
        uid = _seed(db, str(i), f"Solutions Architect {i}")
        db.set_state(uid, "queued")
    hist = fit.build_history(db, limit=20)
    assert len(hist) == 20
    db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest test_fit_history.py -v`
Expected: FAIL with `AttributeError: module 'fit' has no attribute 'build_history'`

- [ ] **Step 3: Implement `build_history` in `fit.py`**

Append to `fit.py`:

```python
# States that count as a positive ("pursue") signal.
_PURSUED_STATES = ("queued", "drafted", "ready", "applied", "interviewing")
# Outcomes on a closed job that count as negative.
_REJECTED_OUTCOMES = ("rejected", "withdrawn", "ghosted")


def build_history(db, limit=20):
    """Build the few-shot decision corpus from the DB, newest decision first.

    Pursued: jobs the operator moved into the work pipeline (or closed favorably).
    Rejected: jobs skipped, or closed with a rejecting outcome.
    Untriaged 'discovered' jobs carry no decision and are excluded.
    """
    rows = db.conn.execute(
        "SELECT * FROM jobs "
        "WHERE state IN ('queued','drafted','ready','applied','interviewing',"
        "'skipped','closed') "
        "ORDER BY updated_at DESC"
    ).fetchall()

    out = []
    for r in rows:
        state = r["state"]
        if state in _PURSUED_STATES:
            decision, reason = "pursued", (r["notes"] or "")
        elif state == "skipped":
            decision, reason = "rejected", (r["skip_reason"] or "")
        elif state == "closed":
            if (r["outcome"] or "") in _REJECTED_OUTCOMES:
                decision = "rejected"
            else:
                decision = "pursued"
            reason = r["close_reason"] or ""
        else:
            continue
        out.append({"title": r["title"], "company": r["company"],
                    "decision": decision, "reason": reason})
        if len(out) >= limit:
            break
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest test_fit_history.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add fit.py test_fit_history.py
git commit -m "[fit]: build feedback corpus from decision history"
```

---

### Task 4: LLM verdict tier

**Files:**
- Modify: `fit.py`
- Test: `test_fit_verdict.py`

**Interfaces:**
- Consumes: a job `dict`; `master` (parsed `master_resume.yaml`); `history` (output of `build_history`); `api_key` string. JD fetch defaults to `job_generate.fetch_description` but is injectable; the Anthropic call is injectable for tests.
- Produces: `fit.verdict(job, master, history, api_key, jd_text=None, fetch_jd=None, call=None) -> dict` returning `{"llm_fit_score": int, "llm_rationale": str, "llm_coding_bar": str}`. Reuses the Anthropic request shape from `job_generate.call_model`.

- [ ] **Step 1: Write the failing test**

Create `test_fit_verdict.py`:

```python
import json
import fit


def test_verdict_prompt_carries_jd_and_history_and_parses():
    captured = {}

    def fake_call(system, user, api_key):
        captured["system"] = system
        captured["user"] = user
        return json.dumps({
            "llm_fit_score": 78,
            "llm_rationale": "Strong SA fit, light coding bar.",
            "llm_coding_bar": "light",
        })

    job = {"title": "Solutions Architect", "company": "temporal",
           "location": "Remote", "ats": "greenhouse", "ext_id": "9"}
    master = {"contact": {"name": "Jordan Rivers"}, "summary": "SA"}
    history = [
        {"title": "Senior SRE", "company": "acme-scheduling",
         "decision": "rejected", "reason": "too code-heavy"},
        {"title": "Solutions Architect", "company": "x",
         "decision": "pursued", "reason": ""},
    ]

    result = fit.verdict(
        job, master, history, api_key="k",
        jd_text="Design reference architectures for customers.",
        call=fake_call,
    )

    assert result["llm_fit_score"] == 78
    assert result["llm_coding_bar"] == "light"
    # History and JD must reach the model.
    assert "too code-heavy" in captured["user"]
    assert "reference architectures" in captured["user"]


def test_verdict_strips_markdown_fences():
    def fake_call(system, user, api_key):
        return "```json\n{\"llm_fit_score\": 50, \"llm_rationale\": \"ok\", \"llm_coding_bar\": \"medium\"}\n```"

    result = fit.verdict(
        {"title": "X", "company": "y"}, {"contact": {"name": "A"}}, [],
        api_key="k", jd_text="jd", call=fake_call)
    assert result["llm_fit_score"] == 50


def test_verdict_system_prompt_forbids_em_dash_and_invention():
    captured = {}

    def fake_call(system, user, api_key):
        captured["system"] = system
        return "{\"llm_fit_score\": 1, \"llm_rationale\": \"x\", \"llm_coding_bar\": \"y\"}"

    fit.verdict({"title": "X", "company": "y"}, {"contact": {"name": "A"}}, [],
                api_key="k", jd_text="jd", call=fake_call)
    assert "em dash" in captured["system"].lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest test_fit_verdict.py -v`
Expected: FAIL with `AttributeError: module 'fit' has no attribute 'verdict'`

- [ ] **Step 3: Implement the verdict tier in `fit.py`**

Add these imports at the top of `fit.py` (below the existing `import yaml`):

```python
import json
import os
import re

import requests
```

Append to `fit.py`:

```python
VERDICT_MODEL = os.environ.get("JOB_MODEL", "claude-opus-4-8")

VERDICT_SYSTEM = """You score how well a job fits one candidate, learning from
the candidate's own past decisions. You are a triage judge, not a writer.

You are given the candidate's master resume, the full job description, and a
list of roles the candidate previously chose to PURSUE or REJECT (with reasons).
Score this role the way the candidate would, paying attention to the real
hands-on-coding bar: this candidate favors solutions-architect, platform, and
leadership roles and avoids heavy individual-contributor coding jobs.

Judge only from the resume and JD. Do not invent candidate experience.

Hard voice rule: never use em dashes or double hyphens. Use commas or separate
sentences.

Return ONLY valid JSON, no markdown fences, with this exact shape:
{
  "llm_fit_score": <integer 0-100>,
  "llm_rationale": "one sentence on why it fits or does not",
  "llm_coding_bar": "light | medium | heavy, plus a few words"
}"""


def _default_fetch(job):
    import job_generate
    return job_generate.fetch_description(job)


def _call_anthropic(system, user, api_key):
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": VERDICT_MODEL,
            "max_tokens": 1000,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    return "".join(b.get("text", "") for b in data.get("content", [])
                   if b.get("type") == "text")


def _history_block(history):
    if not history:
        return "(no past decisions yet)"
    lines = []
    for h in history:
        tail = f" - {h['reason']}" if h.get("reason") else ""
        lines.append(f"- {h['decision'].upper()}: {h['title']} @ {h['company']}{tail}")
    return "\n".join(lines)


def _parse_verdict(raw):
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(json)?", "", text).rsplit("```", 1)[0].strip()
    data = json.loads(text)
    return {
        "llm_fit_score": int(data["llm_fit_score"]),
        "llm_rationale": data.get("llm_rationale", ""),
        "llm_coding_bar": data.get("llm_coding_bar", ""),
    }


def verdict(job, master, history, api_key, jd_text=None,
            fetch_jd=None, call=None):
    """LLM fit verdict for one job. Top-N use only; one API call per job.

    jd_text/fetch_jd/call are injectable so the function is testable offline.
    """
    if jd_text is None:
        fetch_jd = fetch_jd or _default_fetch
        jd_text = fetch_jd(job)
    call = call or _call_anthropic

    user = f"""CANDIDATE MASTER RESUME:
{json.dumps(master, indent=2)}

PAST DECISIONS (learn the candidate's taste from these):
{_history_block(history)}

ROLE TO SCORE:
Company: {job.get('company')}
Title: {job.get('title')}
Location: {job.get('location')}

JOB DESCRIPTION:
{(jd_text or '')[:12000]}

Return the verdict JSON now."""

    return _parse_verdict(call(VERDICT_SYSTEM, user, api_key))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest test_fit_verdict.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add fit.py test_fit_verdict.py
git commit -m "[fit]: LLM verdict tier primed by decision history"
```

---

### Task 5: Capture skip/close reasons in the CLI

**Files:**
- Modify: `job_cli.py` (`cmd_skip` line 200, `cmd_close` line 216, and `build_parser` around lines 302-320)
- Test: `test_cli_reasons.py`

**Interfaces:**
- Consumes: existing `_transition`, `db.set_fields`, `db.resolve`.
- Produces: `job skip IDENT --reason "..."` stores `skip_reason`; `job close IDENT --outcome O --reason "..."` stores `close_reason`. `--reason` is optional; absent leaves the column NULL.

- [ ] **Step 1: Write the failing test**

Create `test_cli_reasons.py`:

```python
import argparse
import jobdb
import job_cli


def _db_with_job(tmp_path):
    db = jobdb.JobDB(tmp_path / "t.db")
    db.upsert_job({"id": "1", "ats": "greenhouse", "company": "acme",
                   "title": "Senior SRE", "location": "Remote", "url": "http://x"})
    return db, jobdb.make_job_uid("greenhouse", "acme", "1")


def test_skip_stores_reason(tmp_path):
    db, uid = _db_with_job(tmp_path)
    args = argparse.Namespace(ident="acme", note=None, reason="too code-heavy")
    job_cli.cmd_skip(db, args)
    assert db.get(uid)["skip_reason"] == "too code-heavy"
    db.close()


def test_close_stores_reason(tmp_path):
    db, uid = _db_with_job(tmp_path)
    db.set_state(uid, "queued"); db.set_state(uid, "drafted")
    db.set_state(uid, "ready"); db.set_state(uid, "applied")
    args = argparse.Namespace(ident="acme", note=None,
                              outcome="rejected", reason="no response after screen")
    job_cli.cmd_close(db, args)
    row = db.get(uid)
    assert row["state"] == "closed"
    assert row["close_reason"] == "no response after screen"
    db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest test_cli_reasons.py -v`
Expected: FAIL with `AttributeError: 'Namespace' object has no attribute ...` or an `AssertionError` (skip_reason is None)

- [ ] **Step 3: Update `cmd_skip` and `cmd_close`**

In `job_cli.py`, replace `cmd_skip` (line 200-201):

```python
def cmd_skip(db, args):
    r = need(db, args.ident)
    _transition(db, args.ident, "skipped", note=args.note)
    if getattr(args, "reason", None):
        db.set_fields(r["uid"], skip_reason=args.reason)
```

Replace `cmd_close` (line 216-217):

```python
def cmd_close(db, args):
    r = need(db, args.ident)
    _transition(db, args.ident, "closed", note=args.note, outcome=args.outcome)
    if getattr(args, "reason", None):
        db.set_fields(r["uid"], close_reason=args.reason)
```

- [ ] **Step 4: Add `--reason` to the parsers**

In `build_parser`, the skip subparser is created in the loop at lines 302-308 (shared with queue/ready/apply). Pull `skip` out of that loop so it can take `--reason`. Change the loop list to drop `skip`:

```python
    for verb, helptext in [("queue", "Mark a job to pursue"),
                           ("ready", "Mark package ready to submit"),
                           ("apply", "Mark as applied (date stamped)")]:
        sp = sub.add_parser(verb, help=helptext)
        sp.add_argument("ident")
        sp.add_argument("--note")

    sp = sub.add_parser("skip", help="Drop a job (optionally with a reason)")
    sp.add_argument("ident")
    sp.add_argument("--note")
    sp.add_argument("--reason", help="Why skipped (feeds fit ranking)")
```

Then add `--reason` to the existing `close` subparser (after line 320, `sp.add_argument("--note")` for close):

```python
    sp = sub.add_parser("close", help="Close a job with an outcome")
    sp.add_argument("ident")
    sp.add_argument("--outcome", choices=OUTCOMES, required=True)
    sp.add_argument("--note")
    sp.add_argument("--reason", help="Why closed (feeds fit ranking)")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest test_cli_reasons.py -v`
Expected: PASS (2 passed)

Also confirm the parser still builds:

Run: `.venv/bin/python job_cli.py --help`
Expected: usage text listing `skip` and `close`, exit 0

- [ ] **Step 6: Commit**

```bash
git add job_cli.py test_cli_reasons.py
git commit -m "[job_cli]: capture structured skip/close reasons for fit feedback"
```

---

### Task 6: Digest formatting + ranking key

**Files:**
- Modify: `fit.py`
- Test: `test_fit_digest.py`

**Interfaces:**
- Consumes: job `dict`s carrying `fit_score` and optionally `llm_fit_score`.
- Produces:
  - `fit.rank_key(job) -> int` — `llm_fit_score` when present, else `fit_score`, else 0. Use as a sort key (descending).
  - `fit.build_digest(ranked: list[dict], counts: dict, limit=10) -> str` — plain-text Discord digest, no em dashes, capped to `limit` leads. Includes per-lead score, title @ company, coding bar, age label, and link, plus a pipeline-counts footer.

- [ ] **Step 1: Write the failing test**

Create `test_fit_digest.py`:

```python
import fit


def test_rank_key_prefers_llm_then_deterministic():
    assert fit.rank_key({"fit_score": 40, "llm_fit_score": 90}) == 90
    assert fit.rank_key({"fit_score": 40, "llm_fit_score": None}) == 40
    assert fit.rank_key({}) == 0


def test_build_digest_orders_and_caps_and_is_clean():
    jobs = [
        {"title": "Solutions Architect", "company": "temporal",
         "fit_score": 70, "llm_fit_score": 88, "llm_coding_bar": "light",
         "url": "http://t", "posted_at": "", "date_source": ""},
        {"title": "Cloud Engineer", "company": "clickhouse",
         "fit_score": 60, "llm_fit_score": None, "llm_coding_bar": None,
         "url": "http://c", "posted_at": "", "date_source": ""},
    ]
    text = fit.build_digest(jobs, {"discovered": 50, "queued": 2}, limit=10)
    assert "—" not in text  # no em dash
    # Highest-ranked appears before the lower one.
    assert text.index("Solutions Architect") < text.index("Cloud Engineer")
    assert "88" in text
    assert "discovered" in text


def test_build_digest_respects_limit():
    jobs = [{"title": f"Role {i}", "company": "x", "fit_score": i,
             "llm_fit_score": None, "llm_coding_bar": None, "url": "u",
             "posted_at": "", "date_source": ""} for i in range(20)]
    text = fit.build_digest(jobs, {}, limit=5)
    assert text.count("Role ") == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest test_fit_digest.py -v`
Expected: FAIL with `AttributeError: module 'fit' has no attribute 'rank_key'`

- [ ] **Step 3: Implement `rank_key` and `build_digest` in `fit.py`**

Add `import freshness as _fr` near the top of `fit.py` (with the other imports), then append:

```python
def rank_key(job):
    v = job.get("llm_fit_score")
    if v is not None:
        return v
    return job.get("fit_score") or 0


def _no_dash(s):
    return (s or "").replace("—", ", ").replace("--", ", ")


def build_digest(ranked, counts, limit=10):
    """Plain-text Discord digest. ranked is pre-sorted or sorted here."""
    ordered = sorted(ranked, key=rank_key, reverse=True)[:limit]
    lines = ["Job-hound daily digest", ""]
    for j in ordered:
        score = rank_key(j)
        bar = j.get("llm_coding_bar") or ""
        try:
            age = _fr.freshness_label(j.get("posted_at"), j.get("date_source"))
        except (KeyError, IndexError, TypeError):
            age = "age unknown"
        head = f"[{score:>3}] {j.get('title')} @ {j.get('company')}"
        lines.append(_no_dash(head))
        detail = f"      {age}"
        if bar:
            detail += f" | coding bar: {bar}"
        lines.append(_no_dash(detail))
        if j.get("url"):
            lines.append(f"      {j['url']}")
        lines.append("")
    if counts:
        footer = ", ".join(f"{k}: {v}" for k, v in counts.items())
        lines.append(f"Pipeline: {footer}")
    return _no_dash("\n".join(lines))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest test_fit_digest.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add fit.py test_fit_digest.py
git commit -m "[fit]: digest formatting and fit ranking key"
```

---

### Task 7: Discord webhook sender

**Files:**
- Create: `notify.py`
- Test: `test_notify.py`

**Interfaces:**
- Consumes: nothing internal; `requests` only.
- Produces: `notify.post_discord(webhook_url: str, text: str, post_fn=None) -> bool`. Truncates `text` to 1900 chars (Discord's 2000 limit minus headroom). Returns `True` on a 2xx, `False` on failure or empty webhook. `post_fn` injectable for tests.

- [ ] **Step 1: Write the failing test**

Create `test_notify.py`:

```python
import notify


class _Resp:
    def __init__(self, code): self.status_code = code


def test_post_discord_sends_content():
    sent = {}

    def fake_post(url, json, timeout):
        sent["url"] = url; sent["json"] = json
        return _Resp(204)

    ok = notify.post_discord("http://hook", "hello", post_fn=fake_post)
    assert ok is True
    assert sent["url"] == "http://hook"
    assert sent["json"]["content"] == "hello"


def test_post_discord_truncates_long_text():
    sent = {}

    def fake_post(url, json, timeout):
        sent["json"] = json
        return _Resp(204)

    notify.post_discord("http://hook", "x" * 5000, post_fn=fake_post)
    assert len(sent["json"]["content"]) <= 1900


def test_post_discord_no_webhook_is_noop():
    assert notify.post_discord("", "hello", post_fn=None) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest test_notify.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'notify'`

- [ ] **Step 3: Write `notify.py`**

```python
#!/usr/bin/env python3
"""
notify.py - push a plain-text digest to a Discord incoming webhook.

Used by the unattended daily run, which is a cron job (not Claude Code) and so
cannot use the Discord MCP. A webhook is a plain HTTPS POST. The URL is secret;
it lives in the host environment or companies.yaml (discord_webhook), never in
version control with a real value.
"""

import requests

DISCORD_LIMIT = 1900  # 2000 hard limit, leave headroom


def post_discord(webhook_url, text, post_fn=None):
    if not webhook_url:
        return False
    post_fn = post_fn or (lambda url, json, timeout:
                          requests.post(url, json=json, timeout=timeout))
    body = text[:DISCORD_LIMIT]
    try:
        resp = post_fn(webhook_url, {"content": body}, 20)
    except requests.RequestException:
        return False
    return 200 <= resp.status_code < 300
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest test_notify.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add notify.py test_notify.py
git commit -m "[notify]: Discord webhook digest sender"
```

---

### Task 8: `refine` CLI command + fit-ordered list

**Files:**
- Modify: `job_cli.py` (add `cmd_refine`, register in `build_parser` and `DISPATCH`, add fit-ordering to `cmd_list`/`fmt_row`)
- Test: `test_cli_refine.py`

**Interfaces:**
- Consumes: `fit.load_profile`, `fit.score`, `fit.build_history`, `fit.verdict`, `fit.rank_key`, `fit.build_digest`; `notify.post_discord`; `db.list`, `db.set_fields`, `db.counts`.
- Produces: `job refine [--top N] [--no-llm] [--digest] [--profile PATH] [--master PATH]`. Scores all active leads (discovered+queued) deterministically; runs `verdict` on the top-N by `fit_score` that lack a cached `llm_fit_score` (unless `--no-llm`); prints a ranked digest; posts to Discord when `--digest` and a webhook are configured. `cmd_list` orders by `fit.rank_key` descending and `fmt_row` shows the score.

- [ ] **Step 1: Write the failing test**

Create `test_cli_refine.py`:

```python
import argparse
import jobdb
import job_cli
import fit


def _seed(db, ext, title):
    db.upsert_job({"id": ext, "ats": "greenhouse", "company": "acme",
                   "title": title, "location": "Remote", "url": "http://x"})


def test_refine_scores_all_and_verdicts_top_n(tmp_path, monkeypatch):
    db = jobdb.JobDB(tmp_path / "t.db")
    _seed(db, "1", "Staff Solutions Architect")
    _seed(db, "2", "Senior Software Development Engineer")
    _seed(db, "3", "Principal Architect")

    calls = []

    def fake_verdict(job, master, history, api_key, **kw):
        calls.append(job["title"])
        return {"llm_fit_score": 85, "llm_rationale": "ok", "llm_coding_bar": "light"}

    monkeypatch.setattr(fit, "verdict", fake_verdict)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    args = argparse.Namespace(top=2, no_llm=False, digest=False,
                              profile=None, master=None, config=None)
    job_cli.cmd_refine(db, args)

    # Every active lead got a deterministic score.
    assert all(db.get(jobdb.make_job_uid("greenhouse", "acme", e))["fit_score"]
               is not None for e in ("1", "2", "3"))
    # Only the top 2 by deterministic score got an LLM verdict.
    assert len(calls) == 2
    db.close()


def test_refine_no_llm_skips_verdict(tmp_path, monkeypatch):
    db = jobdb.JobDB(tmp_path / "t.db")
    _seed(db, "1", "Solutions Architect")

    def boom(*a, **k):
        raise AssertionError("verdict must not be called with --no-llm")

    monkeypatch.setattr(fit, "verdict", boom)
    args = argparse.Namespace(top=10, no_llm=True, digest=False,
                              profile=None, master=None, config=None)
    job_cli.cmd_refine(db, args)
    assert db.get(jobdb.make_job_uid("greenhouse", "acme", "1"))["fit_score"] is not None
    db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest test_cli_refine.py -v`
Expected: FAIL with `AttributeError: module 'job_cli' has no attribute 'cmd_refine'`

- [ ] **Step 3: Add imports and `cmd_refine` to `job_cli.py`**

Add to the import block (after `import freshness as fr`, line 36):

```python
import fit
import notify
```

Add the command function (place it after `cmd_stats`, before `build_parser`):

```python
def cmd_refine(db, args):
    profile = fit.load_profile(args.profile)
    master_path = Path(args.master or (HERE / "master_resume.yaml"))
    master = yaml.safe_load(master_path.read_text())

    active = [dict(r) for r in db.list()
              if r["state"] in ("discovered", "queued")]
    if not active:
        print("No active leads to refine.")
        return

    # 1. Deterministic score for every active lead (free, always re-run).
    for j in active:
        sc, reasons = fit.score(j, profile)
        db.set_fields(j["uid"], fit_score=sc, fit_reasons=reasons)
        j["fit_score"] = sc

    # 2. LLM verdict on the top-N by deterministic score that lack one.
    if not args.no_llm:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            print("ANTHROPIC_API_KEY not set; skipping LLM verdicts.")
        else:
            history = fit.build_history(db)
            topn = sorted(active, key=lambda x: x["fit_score"], reverse=True)
            done = 0
            for j in topn:
                if done >= args.top:
                    break
                if j.get("llm_fit_score") is not None:
                    continue
                try:
                    v = fit.verdict(j, master, history, api_key)
                except Exception as e:
                    print(f"  verdict failed for {j['slug']}: {e}")
                    done += 1
                    continue
                db.set_fields(j["uid"], llm_fit_score=v["llm_fit_score"],
                              llm_rationale=v["llm_rationale"],
                              llm_coding_bar=v["llm_coding_bar"])
                j.update(v)
                done += 1

    # 3. Build and show the ranked digest.
    fresh = [dict(db.get(j["uid"])) for j in active]
    digest = fit.build_digest(fresh, db.counts(), limit=10)
    print(digest)

    # 4. Push to Discord when asked and a webhook is configured.
    if args.digest:
        cfg = load_cfg(args.config) if args.config else {}
        hook = os.environ.get("DISCORD_WEBHOOK_URL") or cfg.get("discord_webhook", "")
        if notify.post_discord(hook, digest):
            print("\n(posted digest to Discord)")
        else:
            print("\n(no Discord webhook configured; digest not pushed)")
```

- [ ] **Step 4: Register `refine` in the parser and dispatch**

In `build_parser`, before `return p`:

```python
    sp = sub.add_parser("refine", help="Score and rank leads; optional Discord digest")
    sp.add_argument("--top", type=int, default=10,
                    help="Max LLM verdicts to run this pass (default 10)")
    sp.add_argument("--no-llm", action="store_true", dest="no_llm",
                    help="Deterministic scoring only, no API calls")
    sp.add_argument("--digest", action="store_true",
                    help="Push the ranked digest to Discord")
    sp.add_argument("--profile", default=None, help="Path to profile.yaml")
    sp.add_argument("--master", default=None, help="Path to master_resume.yaml")
```

Add to `DISPATCH`:

```python
    "refine": cmd_refine,
```

The `refine` command reads `args.config`, which the top-level parser already defines (line 283), so `--config` resolves the webhook from `companies.yaml`.

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest test_cli_refine.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Add fit ordering to `cmd_list` and `fmt_row`**

In `fmt_row` (line 67-75), surface the score. Change the first line to include it:

```python
def fmt_row(r, show_age=True):
    score = fit.rank_key(dict(r))
    line = f"[{r['state']:>12}] [{score:>3}] {r['slug']}\n               {r['title']} @ {r['company']} ({r['location'] or 'n/a'})"
    if show_age:
        try:
            label = fr.freshness_label(r["posted_at"], r["date_source"])
        except (KeyError, IndexError, TypeError):
            label = "age unknown"
        line += f"\n               {label}"
    return line
```

In `cmd_list` (line 140-142), sort the kept rows by fit before printing. After `rows, hidden = _fresh_filter(...)`:

```python
def cmd_list(db, args):
    rows = db.list(state=args.state, limit=None)
    rows, hidden = _fresh_filter(rows, args.max_age, args.all)
    rows = sorted(rows, key=lambda r: fit.rank_key(dict(r)), reverse=True)
    if args.limit:
        rows = rows[:args.limit]
```

- [ ] **Step 7: Verify the full suite and the list command still run**

Run: `.venv/bin/pytest -v`
Expected: all tests PASS

Run: `.venv/bin/python job_cli.py --help`
Expected: usage lists `refine`, exit 0

- [ ] **Step 8: Commit**

```bash
git add job_cli.py test_cli_refine.py
git commit -m "[job_cli]: refine command + fit-ordered list"
```

---

### Task 9: Daily automation wrapper + deploy runbook

**Files:**
- Create: `bin/daily.sh`
- Create: `docs/deploy-tools-host.md`
- Modify: `CLAUDE.md` (add `refine` to the Commands block and note the daily run)

**Interfaces:**
- Consumes: `job_cli.py scan`, `job_cli.py refine --digest`.
- Produces: an idempotent shell entrypoint for cron on the always-on tools host, plus a runbook covering env, cron line, clean rebuild, and re-seeding the Acme Scheduling skip.

- [ ] **Step 1: Write `bin/daily.sh`**

```bash
#!/usr/bin/env bash
# daily.sh - unattended daily discovery + fit-ranking for job-hound.
# Runs on the always-on tools host via cron. Posts a ranked digest to Discord.
#
# Required env (set in the host environment, never committed):
#   ANTHROPIC_API_KEY     for the LLM verdict tier
#   DISCORD_WEBHOOK_URL   incoming webhook for the digest (or set in companies.yaml)
#   JOB_DB                path to jobs.db (the single source of truth on this host)
#   JOB_APPS_DIR          application packages root
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

LOG_DIR="${LOG_DIR:-$HOME/logs}/job-hound"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/daily.log"

PY="$HERE/.venv/bin/python"

{
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) daily run start ==="
  "$PY" job_cli.py scan
  "$PY" job_cli.py refine --digest
  echo "=== done ==="
} >>"$LOG" 2>&1
```

Make it executable:

Run: `chmod +x bin/daily.sh`
Expected: no output, exit 0

- [ ] **Step 2: Verify the wrapper parses and dry-runs locally**

Run: `JOB_DB=./jobs.db bash -n bin/daily.sh && echo OK`
Expected: `OK` (syntax check only; `bash -n` does not execute)

- [ ] **Step 3: Write `docs/deploy-tools-host.md`**

```markdown
# Deploying the daily run on the tools host

The tools host is the single source of truth: the repo, `jobs.db`, and
`JOB_APPS_DIR` all live here. The Mac pulls finished packages to submit.

## One-time setup

1. Clone the repo and create the venv:
       git clone <repo> job-hound && cd job-hound
       python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
2. Create a Discord incoming webhook (Server Settings -> Integrations ->
   Webhooks -> New Webhook) and copy its URL.
3. Set host environment (e.g. in the service user's profile), never committed:
       export ANTHROPIC_API_KEY=sk-ant-...
       export DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
       export JOB_DB=$HOME/job-hound/jobs.db
       export JOB_APPS_DIR=$HOME/job-applications
4. Clean rebuild the DB (we chose rebuild over migration):
       rm -f "$JOB_DB"
       .venv/bin/python job_cli.py scan
5. Re-seed the one known decision so feedback priming has a negative example:
       .venv/bin/python job_cli.py skip <acme-scheduling-sre-ident> --reason "too code-heavy, IC coding role"

## Schedule it (cron)

Run once daily at 06:30 host time:

    30 6 * * * /home/<user>/job-hound/bin/daily.sh

## Verify

    tail -n 40 ~/logs/job-hound/daily.log

A successful run logs the scan summary, the ranked digest, and posts that digest
to Discord.
```

- [ ] **Step 4: Update `CLAUDE.md` Commands block**

In `~/code/job-hound/CLAUDE.md`, in the Commands code block, add after the `draft` line:

```
python job_cli.py refine            score + rank leads, push Discord digest
python job_cli.py refine --no-llm   deterministic scoring only (no API spend)
```

And add a one-line note under Commands:

```
The daily run on the tools host is `bin/daily.sh` (cron): scan then refine
--digest. See docs/deploy-tools-host.md.
```

- [ ] **Step 5: Commit**

```bash
git add bin/daily.sh docs/deploy-tools-host.md CLAUDE.md
git commit -m "[ops]: daily scan+refine wrapper and tools-host deploy runbook"
```

---

### Task 10: Clean rebuild + end-to-end local smoke

**Files:** none (operational verification)

**Interfaces:** exercises the whole pipeline against the real local DB.

- [ ] **Step 1: Back up the current DB**

```bash
cp jobs.db jobs.db.bak
```

- [ ] **Step 2: Clean rebuild**

Run:
```bash
rm -f jobs.db && JOB_DB=./jobs.db .venv/bin/python job_cli.py scan
```
Expected: `Scan complete. N matches, N new -> discovered.`

- [ ] **Step 3: Deterministic-only refine (no API spend) and confirm ranking**

Run:
```bash
JOB_DB=./jobs.db .venv/bin/python job_cli.py refine --no-llm
```
Expected: a "Job-hound daily digest" with leads ordered by score; Temporal/ClickHouse Solutions Architect roles near the top, any hardware/IC-coding roles near the bottom. No em dashes anywhere in the output.

- [ ] **Step 4: Confirm fit-ordered list**

Run:
```bash
JOB_DB=./jobs.db .venv/bin/python job_cli.py list --all --limit 10
```
Expected: each row shows `[state] [score] slug`, highest score first.

- [ ] **Step 5: Re-seed the Acme Scheduling skip decision**

Identify the Acme Scheduling SRE slug from the list, then:
```bash
JOB_DB=./jobs.db .venv/bin/python job_cli.py skip <acme-scheduling-ident> --reason "too code-heavy, IC coding role"
```
Expected: `... -> skipped`. Confirm with `show`:
```bash
JOB_DB=./jobs.db .venv/bin/python job_cli.py show <acme-scheduling-ident>
```

- [ ] **Step 6: Full test suite green**

Run: `.venv/bin/pytest -v`
Expected: all tests PASS

- [ ] **Step 7: Remove the backup and commit nothing**

```bash
rm -f jobs.db.bak
```
(`jobs.db` is gitignored per CLAUDE.md, so there is nothing to commit here. This task is verification only.)

---

## Self-Review

**Spec coverage check (against `2026-06-18-daily-fit-ranking-design.md`):**
- Daily unattended run → Task 9 (`bin/daily.sh` + cron).
- Deterministic prefilter with the documented signals → Task 2 (`fit.score`, `profile.yaml`).
- LLM verdict tier, top-N, primed by history, reuses Anthropic client, no new dep → Task 4 + Task 8.
- Feedback corpus from `state_log` + skip/close reasons → Task 3 + Task 5.
- Schema columns, clean rebuild → Task 1 + Task 10.
- `refine` CLI, fit-ordered `list`, `--no-llm` knob → Task 8.
- Discord via incoming webhook (not MCP) → Task 7 + Task 8.
- Two separate cutoffs (LLM `--top` vs digest `limit=10`) → kept distinct in Task 8 (`--top`) and Task 6 (`build_digest(limit=10)`).
- Testing matrix (Acme Scheduling low, Temporal high; corpus; mocked verdict; e2e) → Tasks 2, 3, 4, 8, 10.
- Deployment model A (host is source of truth, pull to submit) → Task 9 runbook.

**Placeholder scan:** none. Every code step shows complete code; every run step shows the command and expected output.

**Type consistency:** `fit.score` returns `(int, str)` everywhere; `fit.verdict` returns the three `llm_*` keys consumed verbatim by `cmd_refine` and stored via `set_fields`; `fit.rank_key` used identically in `build_digest`, `cmd_list`, and `fmt_row`; `notify.post_discord(webhook, text, post_fn)` signature matches its test and its `cmd_refine` call.

# Model Switching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make job-hound's three LLM call sites route through one provider-configurable path so Kimi (Moonshot) can be selected as a manual fallback when Anthropic is down, record which model produced each gate result, and add a read-only bench that compares model decisions.

**Architecture:** A new `llm.py` owns provider resolution (`JOB_PROVIDER` + overrides) and the single shared Messages POST. `gate.py`, `fit.py`, and `job_generate.py` keep their thin wrappers but delegate the HTTP to `llm.call_messages`. A `gate_model` column records provenance. `bench_models.py` re-runs the gate's decision logic per model against stored JDs without touching the pipeline.

**Tech Stack:** Python 3.12, `requests`, SQLite (stdlib `sqlite3`), `pytest`. Kimi via Moonshot's Anthropic-compatible `/v1/messages` endpoint.

## Global Constraints

- **Manual switch only.** `JOB_PROVIDER` selects the provider. No auto-failover anywhere. Copied from spec: "The Fit Gate is safety-critical and must never change models without the operator choosing to."
- **Backward compatible.** With `JOB_PROVIDER` unset, behavior and the `ANTHROPIC_API_KEY` source are unchanged. The daily cron and the lead inbox UI ingest (which thread `ANTHROPIC_API_KEY` explicitly) must keep working.
- **Fail closed.** A missing provider key is never a silent pass; the gate's ERROR path still blocks drafting.
- **No em dashes** in any emitted or committed string (repo-wide voice rule).
- **Provenance is a fact, not a guess:** existing gated rows backfill to `claude-opus-4-8` (the only model ever run in production).
- **Bench is read-only on job data.** It writes only report files, never mutating a job's `gate_decision`/`gate_json`.
- Run tests from the venv: `source .venv/bin/activate`.

---

### Task 1: `llm.py` provider resolver and shared call

**Files:**
- Create: `llm.py`
- Test: `test_llm.py`

**Interfaces:**
- Produces:
  - `Provider = namedtuple("Provider", "name base_url api_key model version")`
  - `resolve_provider(name=None) -> Provider`
  - `call_messages(system, user, *, max_tokens, provider=None, raise_on_truncation=False, timeout=180) -> str`
  - `class OutputTruncated(Exception)` with attribute `.max_tokens`
  - `DEFAULT_PROVIDER = "anthropic"`

- [ ] **Step 1: Write the failing test**

```python
# test_llm.py
import os
import pytest
import llm


def _clear(monkeypatch):
    for k in ("JOB_PROVIDER", "JOB_MODEL", "JOB_LLM_BASE_URL",
              "JOB_LLM_API_KEY", "ANTHROPIC_API_KEY", "MOONSHOT_API_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_default_provider_is_anthropic(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    p = llm.resolve_provider()
    assert p.name == "anthropic"
    assert p.base_url == "https://api.anthropic.com"
    assert p.api_key == "sk-ant"
    assert p.model == "claude-opus-4-8"


def test_kimi_provider_uses_moonshot(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("JOB_PROVIDER", "kimi")
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-moon")
    p = llm.resolve_provider()
    assert p.name == "kimi"
    assert p.base_url == "https://api.moonshot.ai/anthropic"
    assert p.api_key == "sk-moon"
    assert p.model.startswith("kimi")


def test_job_model_overrides_provider_model(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("JOB_PROVIDER", "kimi")
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-moon")
    monkeypatch.setenv("JOB_MODEL", "kimi-custom")
    assert llm.resolve_provider().model == "kimi-custom"


def test_per_piece_overrides_win(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("JOB_LLM_BASE_URL", "https://proxy.local/")
    monkeypatch.setenv("JOB_LLM_API_KEY", "sk-proxy")
    p = llm.resolve_provider()
    assert p.base_url == "https://proxy.local"   # trailing slash stripped
    assert p.api_key == "sk-proxy"


def test_unknown_provider_raises(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("JOB_PROVIDER", "bogus")
    with pytest.raises(ValueError):
        llm.resolve_provider()


def test_call_messages_truncation_raises(monkeypatch):
    prov = llm.Provider("anthropic", "https://x", "k", "m", "2023-06-01")

    class FakeResp:
        def raise_for_status(self): pass
        def json(self):
            return {"stop_reason": "max_tokens",
                    "content": [{"type": "text", "text": "partial"}]}

    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: FakeResp())
    with pytest.raises(llm.OutputTruncated) as e:
        llm.call_messages("s", "u", max_tokens=16000, provider=prov,
                          raise_on_truncation=True)
    assert e.value.max_tokens == 16000


def test_call_messages_returns_text(monkeypatch):
    prov = llm.Provider("anthropic", "https://x", "k", "m", "2023-06-01")

    class FakeResp:
        def raise_for_status(self): pass
        def json(self):
            return {"stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "hello "},
                                {"type": "text", "text": "world"}]}

    captured = {}
    def fake_post(url, headers, json, timeout):
        captured.update(url=url, headers=headers, body=json)
        return FakeResp()

    monkeypatch.setattr(llm.requests, "post", fake_post)
    out = llm.call_messages("sys", "usr", max_tokens=1000, provider=prov)
    assert out == "hello world"
    assert captured["url"] == "https://x/v1/messages"
    assert captured["headers"]["x-api-key"] == "k"
    assert captured["body"]["model"] == "m"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest test_llm.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'llm'`

- [ ] **Step 3: Write the implementation**

```python
# llm.py
"""Single provider-configurable path to an Anthropic-shaped Messages API.

All three LLM call sites (gate, fit, generate) route their HTTP through here so a
second provider can be selected by env without touching each call site. Kimi K2
(Moonshot) works because it exposes an Anthropic-compatible /v1/messages
endpoint, so only base_url + key + model change.

Manual switch only: JOB_PROVIDER selects the provider. Nothing here auto-fails-over.
No em dashes in any emitted string.
"""
import os
from collections import namedtuple

import requests

Provider = namedtuple("Provider", "name base_url api_key model version")

DEFAULT_PROVIDER = "anthropic"
ANTHROPIC_VERSION = "2023-06-01"

# Per-provider defaults. api_key is read from the named env var; model is the
# provider default and JOB_MODEL overrides it for whichever provider is active.
_REGISTRY = {
    "anthropic": {
        "base_url": "https://api.anthropic.com",
        "key_env": "ANTHROPIC_API_KEY",
        "model": "claude-opus-4-8",
    },
    "kimi": {
        # Moonshot's Anthropic-compatible endpoint. Confirm the current model id
        # against Moonshot docs at key-insertion time; JOB_MODEL overrides it.
        "base_url": "https://api.moonshot.ai/anthropic",
        "key_env": "MOONSHOT_API_KEY",
        "model": "kimi-k2-0711-preview",
    },
}


class OutputTruncated(Exception):
    """Raised when the model stopped because it hit max_tokens."""

    def __init__(self, max_tokens):
        self.max_tokens = max_tokens
        super().__init__(f"output truncated at {max_tokens} tokens")


def resolve_provider(name=None):
    """Resolve the active provider from env (or an explicit name).

    Precedence for each piece: explicit JOB_LLM_* override, then the registry.
    JOB_MODEL overrides the model for whichever provider is active. api_key is
    NOT validated here; a missing key is the caller's fail-closed path.
    """
    name = name or os.environ.get("JOB_PROVIDER", DEFAULT_PROVIDER)
    reg = _REGISTRY.get(name)
    if reg is None:
        raise ValueError(f"unknown JOB_PROVIDER '{name}'; known: {sorted(_REGISTRY)}")
    base_url = os.environ.get("JOB_LLM_BASE_URL", reg["base_url"]).rstrip("/")
    api_key = os.environ.get("JOB_LLM_API_KEY") or os.environ.get(reg["key_env"], "")
    model = os.environ.get("JOB_MODEL", reg["model"])
    return Provider(name, base_url, api_key, model, ANTHROPIC_VERSION)


def call_messages(system, user, *, max_tokens, provider=None,
                  raise_on_truncation=False, timeout=180):
    """POST one system+user turn to an Anthropic-shaped Messages API; return text.

    provider defaults to resolve_provider(). raise_on_truncation surfaces a
    max_tokens cutoff as OutputTruncated (the gate wants this; the other sites
    tolerate a cut response). The key rides on the provider; a missing key lets
    the HTTP layer 401 exactly as before.
    """
    provider = provider or resolve_provider()
    resp = requests.post(
        f"{provider.base_url}/v1/messages",
        headers={"x-api-key": provider.api_key,
                 "anthropic-version": provider.version,
                 "content-type": "application/json"},
        json={"model": provider.model, "max_tokens": max_tokens,
              "system": system,
              "messages": [{"role": "user", "content": user}]},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    if raise_on_truncation and data.get("stop_reason") == "max_tokens":
        raise OutputTruncated(max_tokens)
    return "".join(b.get("text", "") for b in data.get("content", [])
                   if b.get("type") == "text")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest test_llm.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add llm.py test_llm.py
git commit -m "[llm]: provider-configurable Messages call path"
```

---

### Task 2: Route `gate.py` through `llm.py` (provider-aware, truncation preserved)

**Files:**
- Modify: `gate.py` (imports; `_call_anthropic`; `run_gate` key guard)
- Test: `test_gate_provider.py` (new)

**Interfaces:**
- Consumes: `llm.resolve_provider`, `llm.call_messages`, `llm.OutputTruncated`, `llm.DEFAULT_PROVIDER`
- Produces: unchanged public `extract`/`run_gate` behavior; a provider-aware key guard.

- [ ] **Step 1: Write the failing test**

```python
# test_gate_provider.py
import os
import gate
import llm


def test_gate_key_guard_uses_active_provider(monkeypatch, tmp_path):
    # JOB_PROVIDER=kimi with only MOONSHOT_API_KEY set must NOT fail with
    # "ANTHROPIC_API_KEY not set".
    for k in ("JOB_PROVIDER", "ANTHROPIC_API_KEY", "MOONSHOT_API_KEY", "JOB_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("JOB_PROVIDER", "kimi")
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-moon")

    from jobdb import JobDB
    db = JobDB(str(tmp_path / "t.db"))
    db.upsert_job({"ats": "manual", "company": "acme", "id": "x1",
                   "title": "Platform Engineer", "location": "Remote", "url": ""})
    row = db.get(db.resolve("acme")["uid"])

    captured = {}
    def fake_call(system, user, api_key):
        captured["called"] = True
        return '{"requirements": []}'

    master = {"capabilities": [], "do_not_claim": []}
    out = gate.run_gate(db, row, master, jd_text="Build platforms on AWS.",
                        call=fake_call)
    # It reached extract (did not short-circuit on a missing anthropic key).
    assert captured.get("called") is True
    assert out["decision"] != gate.ERROR or "not set" not in (out.get("error") or "")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest test_gate_provider.py -v`
Expected: FAIL. `run_gate` currently resolves `ANTHROPIC_API_KEY` and `_fail`s "ANTHROPIC_API_KEY not set", so `fake_call` is never reached.

- [ ] **Step 3: Add the import**

In `gate.py`, next to the other imports (after `import requests`), add:

```python
import llm
```

- [ ] **Step 4: Replace `_call_anthropic`**

Replace the whole `_call_anthropic` function body (currently a raw `requests.post`) with:

```python
def _call_anthropic(system, user, api_key):
    # api_key is honored as an override for the DEFAULT provider only (the CLI and
    # ingest thread ANTHROPIC_API_KEY here). JOB_PROVIDER still selects base_url +
    # model. A non-default provider carries its own key from env.
    prov = llm.resolve_provider()
    if api_key and prov.name == llm.DEFAULT_PROVIDER:
        prov = prov._replace(api_key=api_key)
    try:
        return llm.call_messages(system, user, max_tokens=GATE_MAX_TOKENS,
                                 provider=prov, raise_on_truncation=True)
    except llm.OutputTruncated:
        raise TruncatedResponse(
            f"the model hit the {GATE_MAX_TOKENS} token output cap and the JSON was "
            "cut off mid-response. The job description is long enough that the "
            "requirement list did not fit. Raise GATE_MAX_TOKENS in gate.py.")
```

- [ ] **Step 5: Make `run_gate`'s key guard provider-aware**

In `run_gate`, replace this line near the top:

```python
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
```

with:

```python
    provider = llm.resolve_provider()
    if api_key and provider.name == llm.DEFAULT_PROVIDER:
        provider = provider._replace(api_key=api_key)
    api_key = provider.api_key
```

and replace the guard:

```python
    if not api_key:
        return _fail("ANTHROPIC_API_KEY not set")
```

with:

```python
    if not api_key:
        return _fail(f"{provider.name} API key not set")
```

- [ ] **Step 6: Remove the now-unused `requests` import if orphaned**

Run: `grep -n "requests\." gate.py`
If there are no remaining `requests.` usages, delete the `import requests` line. If any remain, leave it.

- [ ] **Step 7: Run the gate suite**

Run: `pytest test_gate_provider.py test_gate_run.py test_gate_block.py test_gate_truncation.py -v`
Expected: PASS. The truncation test still passes because `_call_anthropic` re-raises `TruncatedResponse` with the same message.

- [ ] **Step 8: Commit**

```bash
git add gate.py test_gate_provider.py
git commit -m "[gate]: route model calls through llm.py, provider-aware key guard"
```

---

### Task 3: Route `fit.py` and `job_generate.py` through `llm.py`

**Files:**
- Modify: `fit.py` (`_call_anthropic`, imports)
- Modify: `job_generate.py` (the `requests.post` block, imports)
- Test: reuse `test_fit_verdict.py` (inject `call`), add one assertion file `test_generate_provider.py`

**Interfaces:**
- Consumes: `llm.resolve_provider`, `llm.call_messages`, `llm.DEFAULT_PROVIDER`
- Produces: unchanged `fit.verdict` and `job_generate` text handling.

- [ ] **Step 1: Write the failing test**

```python
# test_generate_provider.py
import job_generate
import llm


def test_generate_call_uses_llm(monkeypatch):
    prov = llm.Provider("anthropic", "https://x", "k", "m", "2023-06-01")
    seen = {}

    def fake_call(system, user, *, max_tokens, provider=None, **kw):
        seen["max_tokens"] = max_tokens
        return '{"summary": "ok"}'

    monkeypatch.setattr(job_generate.llm, "call_messages", fake_call)
    monkeypatch.setattr(job_generate.llm, "resolve_provider", lambda: prov)
    out = job_generate._call_model("SYS", "USER", "sk-ant")
    assert out == {"summary": "ok"}
    assert seen["max_tokens"] == 4000
```

Note: this test assumes the drafting POST is extracted into a helper
`job_generate._call_model(system, user, api_key) -> dict`. Step 4 creates it.

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest test_generate_provider.py -v`
Expected: FAIL with `AttributeError: module 'job_generate' has no attribute 'llm'` (and no `_call_model`).

- [ ] **Step 3: Update `fit.py`**

Add `import llm` next to the imports. Replace `fit.py`'s `_call_anthropic` with:

```python
def _call_anthropic(system, user, api_key):
    prov = llm.resolve_provider()
    if api_key and prov.name == llm.DEFAULT_PROVIDER:
        prov = prov._replace(api_key=api_key)
    return llm.call_messages(system, user, max_tokens=1000, provider=prov)
```

Run `grep -n "requests\." fit.py`; if none remain, delete `import requests`.

- [ ] **Step 4: Update `job_generate.py`**

Add `import llm` next to the imports. Find the drafting block that does
`resp = requests.post("https://api.anthropic.com/v1/messages", ...)` through
`return json.loads(text)`. Extract it into a helper and replace the POST:

```python
def _call_model(system, user, api_key):
    prov = llm.resolve_provider()
    if api_key and prov.name == llm.DEFAULT_PROVIDER:
        prov = prov._replace(api_key=api_key)
    text = llm.call_messages(system, user, max_tokens=4000, provider=prov).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(json)?", "", text).rsplit("```", 1)[0].strip()
    return json.loads(text)
```

Then, at the original call site, replace the inline `requests.post(...)` ...
`return json.loads(text)` block with a call to `_call_model(GEN_SYSTEM, user, api_key)`
(returning its result). Keep the surrounding `user = f"""..."""` prompt build intact.

Run `grep -n "requests\." job_generate.py`; if none remain, delete `import requests`.

- [ ] **Step 5: Run the tests**

Run: `pytest test_generate_provider.py test_fit_verdict.py test_fit_score.py -v`
Expected: PASS.

- [ ] **Step 6: Full suite regression check**

Run: `pytest -q`
Expected: PASS (same count as before plus the new tests).

- [ ] **Step 7: Commit**

```bash
git add fit.py job_generate.py test_generate_provider.py
git commit -m "[fit,generate]: route model calls through llm.py"
```

---

### Task 4: Record `gate_model` provenance

**Files:**
- Modify: `jobdb.py` (`ADDED_COLUMNS`, `_GATE_COLUMNS`, `_migrate` backfill, `set_gate`)
- Modify: `gate.py` (`_persist`, `_fail`, `run_gate` to pass the model)
- Modify: `job_cli.py` (`_print_gate` surfaces the model)
- Test: `test_gate_model_provenance.py` (new)

**Interfaces:**
- Consumes: `provider.model` from Task 2's `run_gate`.
- Produces: `JobDB.set_gate(uid, decision, gate_json, report_path, model=None)`; `gate_model` column; `run_gate` return dict gains `"model"`.

- [ ] **Step 1: Write the failing test**

```python
# test_gate_model_provenance.py
import json
from jobdb import JobDB


def test_backfill_stamps_existing_gated_rows(tmp_path):
    db = JobDB(str(tmp_path / "t.db"))
    db.upsert_job({"ats": "manual", "company": "acme", "id": "x1",
                   "title": "Platform Engineer", "location": "Remote", "url": ""})
    uid = db.resolve("acme")["uid"]
    # Simulate a pre-provenance gated row: gate_decision set, gate_model absent.
    db.set_gate(uid, "PROCEED", json.dumps({"requirements": []}), "/tmp/r.md")
    db.conn.execute("UPDATE jobs SET gate_model = NULL WHERE uid = ?", (uid,))
    db.conn.commit()

    # Re-open triggers _migrate, which backfills.
    db2 = JobDB(str(tmp_path / "t.db"))
    row = db2.get(uid)
    assert row["gate_model"] == "claude-opus-4-8"


def test_set_gate_records_model(tmp_path):
    db = JobDB(str(tmp_path / "t.db"))
    db.upsert_job({"ats": "manual", "company": "beta", "id": "y1",
                   "title": "SRE", "location": "Remote", "url": ""})
    uid = db.resolve("beta")["uid"]
    db.set_gate(uid, "PROCEED", json.dumps({"requirements": []}), "/tmp/r.md",
                model="kimi-k2-0711-preview")
    assert db.get(uid)["gate_model"] == "kimi-k2-0711-preview"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest test_gate_model_provenance.py -v`
Expected: FAIL (`no such column: gate_model`, and `set_gate()` takes no `model`).

- [ ] **Step 3: Add the column and backfill in `jobdb.py`**

In `ADDED_COLUMNS`, add after `"gate_overridden_at": "TEXT",`:

```python
    "gate_model": "TEXT",
```

In `_GATE_COLUMNS`, add `"gate_model"` to the set.

In `_migrate`, after the jobs-column `for` loop that runs `ALTER TABLE jobs ADD COLUMN`, add the backfill:

```python
        # Every row gated before provenance existed was evaluated with the only
        # model ever run in production. Stamp it once; this is a fact, not a guess.
        self.conn.execute(
            "UPDATE jobs SET gate_model = 'claude-opus-4-8' "
            "WHERE gate_decision IS NOT NULL AND gate_model IS NULL")
        self.conn.commit()
```

- [ ] **Step 4: Extend `set_gate` to write the model**

Change the signature:

```python
    def set_gate(self, uid, decision, gate_json, report_path, model=None):
```

In its `UPDATE`, add `gate_model = ?` to the SET list and `model` to the params
(place it right after `gate_json = ?`):

```python
        self.conn.execute(
            "UPDATE jobs SET gate_decision = ?, gate_json = ?, gate_model = ?, "
            "gate_report_path = ?, gate_at = ?, updated_at = ?, "
            "gate_override_reason = NULL, gate_overridden_at = NULL "
            "WHERE uid = ?",
            (decision, gate_json, model, str(report_path), ts, ts, uid))
```

- [ ] **Step 5: Run the DB test to verify it passes**

Run: `pytest test_gate_model_provenance.py -v`
Expected: PASS.

- [ ] **Step 6: Thread the model through `gate.py`**

In `_persist`, add a `model=None` parameter, include it in the json dict, and pass
it to `set_gate`:

```python
def _persist(db, job_row, requirements, title, decision, cnt,
             loc=None, skills_decision=None, model=None):
    path = _report_path(job_row)
    path.write_text(render_report(job_row, requirements, title, decision, cnt,
                                  loc=loc, skills_decision=skills_decision))
    db.set_gate(job_row["uid"], decision,
                json.dumps({"requirements": requirements, "title": title,
                            "location": loc, "skills_decision": skills_decision,
                            "model": model}),
                str(path), model=model)
    return path
```

In `run_gate`, `provider` is already resolved (Task 2). Pass its model to
`_persist`:

```python
    path = _persist(db, job_row, reqs, title, decision, cnt,
                    loc=loc, skills_decision=skills_decision,
                    model=provider.model)
```

and add the model to the return dict:

```python
    return {"decision": decision, "requirements": reqs, "title": title,
            "report_path": path, "counts": cnt, "location": loc,
            "skills_decision": skills_decision, "model": provider.model}
```

In `run_gate`'s inner `_fail`, pass the attempted model to `set_gate` so an ERROR
row still records which provider was tried. `_fail` closes over `provider`
(defined before the first `_fail` call in Task 2's ordering), so change its
`set_gate` call to:

```python
        db.set_gate(job_row["uid"], ERROR,
                    json.dumps({"requirements": [], "title": title,
                                "error": reason, "model": provider.model}),
                    str(path), model=provider.model)
```

Note: verify `provider` is assigned before the first `_fail` invocation. In
Task 2 the `provider = llm.resolve_provider()` line replaced the old
`api_key = ...` line, which sits above the profile-load `_fail`. If any `_fail`
can fire before `provider` exists, move the `provider` resolution to the very top
of `run_gate` (right after `title = title_check(...)`).

- [ ] **Step 7: Surface the model in the CLI**

In `job_cli.py` `_print_gate`, after the `print(f"\nGate: {label}")` line, add:

```python
    if out.get("model"):
        print(f"  model: {out['model']}")
```

- [ ] **Step 8: Run gate tests**

Run: `pytest test_gate_model_provenance.py test_gate_run.py test_gate_block.py test_gate_cli.py -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add jobdb.py gate.py job_cli.py test_gate_model_provenance.py
git commit -m "[gate]: record gate_model provenance, backfill existing rows"
```

---

### Task 5: `bench_models.py` decision-agreement bench

**Files:**
- Create: `bench_models.py`
- Test: `test_bench_models.py`

**Interfaces:**
- Consumes: `gate.extract`, `gate.enforce`, `gate.decide`, `gate.build_evidence`, `gate.load_profile`, `gate.location_ok`, `gate._apply_location`, `llm.resolve_provider`, `llm.call_messages`, `gate.GATE_MAX_TOKENS`
- Produces:
  - `evaluate_job(job_row, jd_text, master, provider) -> {"decision": str, ...}`
  - `run_bench(db, idents, model_names, master) -> list[dict]` (per-job rows with each model's decision)
  - `render_report(results) -> str` (markdown; disagreements first)

- [ ] **Step 1: Write the failing test**

```python
# test_bench_models.py
import bench_models
import gate


def test_evaluate_job_is_side_effect_free(monkeypatch, tmp_path):
    from jobdb import JobDB
    db = JobDB(str(tmp_path / "t.db"))
    db.upsert_job({"ats": "manual", "company": "acme", "id": "x1",
                   "title": "Platform Engineer", "location": "Remote", "url": ""})
    uid = db.resolve("acme")["uid"]
    row = db.get(uid)

    def fake_call(system, user, api_key):
        return '{"requirements": []}'

    prov = type("P", (), {"model": "fake-model", "name": "anthropic",
                          "api_key": "k", "base_url": "https://x",
                          "version": "2023-06-01"})()
    master = {"capabilities": [], "do_not_claim": []}
    res = bench_models.evaluate_job(row, "Build platforms on AWS.", master,
                                    prov, call=fake_call)
    assert "decision" in res
    # No gate row was written: evaluate_job must not persist.
    assert db.get(uid)["gate_decision"] is None


def test_render_report_flags_disagreements():
    results = [
        {"slug": "a", "title": "SRE", "decisions": {"opus": "PROCEED", "kimi": "PROCEED"}},
        {"slug": "b", "title": "PE", "decisions": {"opus": "PROCEED", "kimi": "DO_NOT_APPLY"}},
    ]
    md = bench_models.render_report(results)
    assert "Disagreements" in md
    assert "b" in md.split("Disagreements", 1)[1].split("\n\n", 1)[0]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest test_bench_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bench_models'`.

- [ ] **Step 3: Write `bench_models.py`**

```python
#!/usr/bin/env python3
"""Compare gate decisions across models on already-evaluated jobs.

READ-ONLY on job data. Runs the gate's own decision logic (extract -> enforce ->
decide + location overlay) against the stored JD for each model, and reports
where the models disagree. Never writes a job's gate_decision; the only output is
a markdown report. the operator adjudicates disagreements.

Usage:
    python bench_models.py --models claude-opus-4-8,kimi-k2-0711-preview [ident ...]
With no idents, benches every row that already has a gate_decision.
"""
import argparse
import json
import os
import sys
from pathlib import Path

import yaml

import gate
import llm
from jobdb import JobDB


def _provider_for(model_name):
    """Resolve a provider whose model is model_name. Picks the registry provider
    whose default model shares the family prefix, then overrides the model."""
    name = "kimi" if model_name.startswith("kimi") else "anthropic"
    prov = llm.resolve_provider(name)
    return prov._replace(model=model_name)


def evaluate_job(job_row, jd_text, master, provider, call=None):
    """Run the gate decision path once for one model. No persistence."""
    caps_dnc = gate.load_profile(master)
    _caps, dnc = caps_dnc
    evidence = gate.build_evidence(master)

    def _default_call(system, user, api_key):
        return llm.call_messages(system, user, max_tokens=gate.GATE_MAX_TOKENS,
                                 provider=provider, raise_on_truncation=True)

    call = call or _default_call
    raw = gate.extract(dict(job_row), jd_text, evidence, provider.api_key, call=call)
    reqs = gate.enforce(raw, dnc)
    skills_decision = gate.decide(reqs)
    loc = gate.location_ok(dict(job_row), jd_text)
    decision = gate._apply_location(skills_decision, loc)
    return {"decision": decision, "skills_decision": skills_decision,
            "counts": gate.counts(reqs)}


def run_bench(db, idents, model_names, master):
    if idents:
        rows = [db.get(db.resolve(i)["uid"]) for i in idents]
    else:
        rows = [r for r in db.all_jobs() if r["gate_decision"] is not None]
    results = []
    for row in rows:
        jd = row["description"]
        if not (jd or "").strip():
            continue
        decisions = {}
        for m in model_names:
            prov = _provider_for(m)
            try:
                decisions[m] = evaluate_job(row, jd, master, prov)["decision"]
            except Exception as e:  # one model failing must not sink the run
                decisions[m] = f"ERROR: {e}"
        results.append({"slug": row["slug"], "title": row["title"],
                        "decisions": decisions})
    return results


def render_report(results):
    disagree = [r for r in results
                if len(set(r["decisions"].values())) > 1]
    lines = ["# Model bench: gate decision agreement", ""]
    lines.append(f"{len(results)} job(s) benched, {len(disagree)} disagreement(s).")
    lines.append("")
    lines.append("## Disagreements")
    lines.append("")
    if not disagree:
        lines.append("(none)")
    for r in disagree:
        lines.append(f"- {r['slug']} ({r['title']})")
        for m, d in r["decisions"].items():
            lines.append(f"    - {m}: {d}")
    lines.append("")
    lines.append("## All results")
    lines.append("")
    for r in results:
        cells = ", ".join(f"{m}={d}" for m, d in r["decisions"].items())
        lines.append(f"- {r['slug']}: {cells}")
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("idents", nargs="*", help="job idents; default: all gated jobs")
    ap.add_argument("--models", required=True,
                    help="comma-separated model ids to compare")
    ap.add_argument("--out", default=None, help="report path (default: stdout)")
    args = ap.parse_args(argv)

    db = JobDB(os.environ.get("JOB_DB") or str(Path.cwd() / "jobs.db"))
    master = yaml.safe_load((Path(__file__).resolve().parent
                             / "master_resume.yaml").read_text())
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    results = run_bench(db, args.idents, models, master)
    report = render_report(results)
    if args.out:
        Path(args.out).expanduser().write_text(report)
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Confirm `db.all_jobs()` exists (or adjust)**

Run: `grep -n "def all_jobs\|def list_jobs\|def all(" jobdb.py`
If the method has a different name (e.g. `list_jobs`), update the one call in
`run_bench` to match. Do not add a new DB method if a lister already exists.

- [ ] **Step 5: Run the bench tests**

Run: `pytest test_bench_models.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite**

Run: `pytest -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add bench_models.py test_bench_models.py
git commit -m "[bench]: read-only model decision-agreement bench"
```

---

## Ops follow-ups (not code cards; done after merge)

These are operator steps, tracked on the board but not TDD tasks:

1. **Provision the Moonshot key.** Add `MOONSHOT_API_KEY=sk-...` to
   `~/.config/job-hound/job-hound.env` on the host. No code change (this is the "key is the
   only remaining step" contract).
2. **Deploy.** Merge each task's PR to `main`; on the host `git pull --ff-only
   origin main`. The `gate_model` migration + backfill runs automatically on next
   `JobDB` open.
3. **Run the bench** once the key exists:
   `JOB_DB=~/job-hound/jobs.db .venv/bin/python bench_models.py --models
   claude-opus-4-8,kimi-k2-0711-preview --out ~/model-bench.md`, then adjudicate
   disagreements.
4. **Confirm the Kimi model id** against Moonshot docs at key-insertion time;
   override with `JOB_MODEL` if it differs from the registry default.

## Self-review notes

- Spec A (provider switch) -> Tasks 1-3. Spec B (provenance) -> Task 4. Spec C
  (bench) -> Task 5. Spec D (testing) -> tests in every task. Spec E (kanban) is
  the tracking layer that wraps this plan, set up separately.
- Backward-compat constraint verified: default provider path unchanged; the only
  behavior change under `JOB_PROVIDER` unset is that the gate now raises
  `TruncatedResponse` via `llm.OutputTruncated` (same message, same effect).
- The `_fail`/`provider` ordering caveat is called out explicitly in Task 4 Step 6.

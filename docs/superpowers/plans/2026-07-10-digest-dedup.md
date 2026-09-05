# Digest Dedup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the daily Discord digest stop repeating identical leads by splitting it into a "New since last digest" section and a compact "Still open" recap, backed by a per-lead `digested_at` marker.

**Architecture:** Add a `digested_at` column to `jobs` (NULL = never sent). `refine_pipeline` partitions the fresh, `discovered`-only candidate set into New (`digested_at IS NULL`) and Still-open (set), and returns both a rendered two-section digest and the list of uids it displayed. `cmd_refine` stamps `digested_at` on those uids only after a successful Discord post, so today's New becomes tomorrow's Still-open. `refine_pipeline` itself never stamps (the MCP adapter, which does not deliver, stays side-effect-clean).

**Tech Stack:** Python 3, sqlite3, pytest. No new dependencies.

## Global Constraints

- **No em dashes, ever** in any generated output. The digest already sanitizes display text via `fit._no_dash`; keep every new display path routed through it. Job URLs must NOT be sanitized (Workday URLs contain `--`).
- **`refine_pipeline` stays import-safe and free of the "sent" side effect.** It may keep writing `fit_score` (existing behavior) but must not stamp `digested_at`. Only `cmd_refine`, after a successful post, stamps.
- **DB migrations are additive only.** New columns go in both `SCHEMA` (for fresh DBs) and `ADDED_COLUMNS` (to migrate the existing host `jobs.db` on open). Never destructive.
- **Digest sections show only `discovered` leads.** `queued`/`drafted`/`ready`/terminal states are excluded from both sections.
- Section caps: New = 12, Still-open = 10 (remainder as a `(+N more)` tail).
- Empty New renders the literal line `No new leads today.` and still posts (never silent).

---

### Task 1: `digested_at` column, migration, and `mark_digested`

**Files:**
- Modify: `jobdb.py` (`SCHEMA` jobs table ~line 116-118; `ADDED_COLUMNS` ~line 165-177; add `mark_digested` method after `set_vote` ~line 319)
- Test: `test_digest_dedup.py` (create)

**Interfaces:**
- Produces: `JobDB.mark_digested(uids: list[str]) -> None` — stamps `digested_at = now_iso()` for each uid. Empty list is a no-op. Does NOT bump `updated_at` (a digest send is not a content change). New `jobs.digested_at TEXT` column, NULL until first sent.

- [ ] **Step 1: Write the failing test**

Create `test_digest_dedup.py`:

```python
import jobdb
import job_cli
import fit


def _seed(db, ext, title, state=None):
    db.upsert_job({"id": ext, "ats": "greenhouse", "company": "acme",
                   "title": title, "location": "Remote", "url": "http://x"})
    uid = jobdb.make_job_uid("greenhouse", "acme", ext)
    if state:
        db.set_state(uid, state, note="test")
    return uid


def test_digested_at_column_exists_and_migrates_idempotently(tmp_path):
    path = tmp_path / "t.db"
    db = jobdb.JobDB(path)
    cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(jobs)")}
    assert "digested_at" in cols
    db.close()
    # Reopening an existing DB must not error (migration is idempotent).
    db2 = jobdb.JobDB(path)
    cols2 = {r["name"] for r in db2.conn.execute("PRAGMA table_info(jobs)")}
    assert "digested_at" in cols2
    db2.close()


def test_mark_digested_stamps_uids(tmp_path):
    db = jobdb.JobDB(tmp_path / "t.db")
    uid = _seed(db, "1", "Staff SRE")
    assert db.get(uid)["digested_at"] is None
    db.mark_digested([uid])
    assert db.get(uid)["digested_at"] is not None
    # Empty list is a safe no-op.
    db.mark_digested([])
    db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest test_digest_dedup.py -v`
Expected: FAIL — `assert "digested_at" in cols` fails (column missing) and `AttributeError: 'JobDB' object has no attribute 'mark_digested'`.

- [ ] **Step 3: Add the column to `SCHEMA`**

In `jobdb.py`, in the `jobs` CREATE TABLE inside `SCHEMA`, add the column next to the other feedback columns (after `voted_at TEXT,` on line 118):

```python
    voted_at      TEXT,                   -- timestamp of the last vote
    digested_at   TEXT,                   -- last time this lead was in a posted digest
    discovered_at TEXT NOT NULL,
```

- [ ] **Step 4: Add the column to `ADDED_COLUMNS`**

In `jobdb.py`, add to the `ADDED_COLUMNS` dict (after `"voted_at": "TEXT",`):

```python
    "voted_at": "TEXT",
    "digested_at": "TEXT",
```

- [ ] **Step 5: Add the `mark_digested` method**

In `jobdb.py`, add after `set_vote` (before `record_file`, ~line 320):

```python
    def mark_digested(self, uids):
        """Stamp digested_at=now for each uid included in a POSTED digest.

        Called only after a successful Discord post (see job_cli.cmd_refine),
        never by refine_pipeline. Intentionally does not touch updated_at: a
        digest send is a delivery event, not a change to the lead's content.
        """
        if not uids:
            return
        ts = now_iso()
        self.conn.executemany(
            "UPDATE jobs SET digested_at = ? WHERE uid = ?",
            [(ts, u) for u in uids])
        self.conn.commit()
```

- [ ] **Step 6: Run test to verify it passes**

Run: `python -m pytest test_digest_dedup.py -v`
Expected: PASS (both tests).

- [ ] **Step 7: Commit**

```bash
git add jobdb.py test_digest_dedup.py
git commit -m "feat: add digested_at column and mark_digested to jobdb"
```

---

### Task 2: Two-section digest formatter in `fit.py`

**Files:**
- Modify: `fit.py` (extract `_digest_line` from `build_digest` ~line 281-316; add `_seen_oneliner` and `build_digest_sections`)
- Test: `test_digest_dedup.py` (append)

**Interfaces:**
- Consumes: existing `fit.rank_key`, `fit.sort_key`, `fit._short_age`, `fit._no_dash`, `fit._LOC_ABBR`.
- Produces:
  - `fit._digest_line(j: dict) -> str` — one formatted digest line (the exact per-lead line `build_digest` already produces).
  - `fit.build_digest_sections(new: list[dict], seen: list[dict], counts: dict, new_limit=12, seen_limit=10) -> tuple[str, list[str]]` — returns `(digest_text, shown_uids)`. Sorts each list by `sort_key` desc, caps New at `new_limit` and Still-open at `seen_limit`, renders the two sections + pipeline counts, and returns the uids actually displayed (New shown + Still-open shown) for the caller to stamp.

- [ ] **Step 1: Write the failing test**

Append to `test_digest_dedup.py`:

```python
def _job(uid, title, score, posted_at="", ds="", loc="remote", url="http://x"):
    return {"uid": uid, "title": title, "company": "acme", "fit_score": score,
            "llm_fit_score": None, "llm_coding_bar": None, "location_type": loc,
            "url": url, "posted_at": posted_at, "date_source": ds}


def test_build_digest_sections_new_and_still_open():
    new = [_job("u1", "Staff SRE", 90)]
    seen = [_job("u2", "Principal SRE", 80)]
    text, shown = fit.build_digest_sections(new, seen, {"discovered": 5})
    assert "New since last digest** (1)" in text
    assert "Staff SRE" in text
    assert "Still open** (1 previously sent)" in text
    assert "acme 80" in text            # collapsed one-liner for the seen lead
    assert "Principal SRE" not in text  # seen leads are NOT full lines
    assert shown == ["u1", "u2"]
    assert "—" not in text         # no em dash


def test_build_digest_sections_empty_new_says_nothing_new():
    seen = [_job("u2", "Principal SRE", 80)]
    text, shown = fit.build_digest_sections([], seen, {})
    assert "No new leads today." in text
    assert "Still open** (1 previously sent)" in text
    assert shown == ["u2"]


def test_build_digest_sections_caps_and_more_tail():
    seen = [_job(f"s{i}", f"Role {i}", i) for i in range(14)]
    text, shown = fit.build_digest_sections([], seen, {}, new_limit=12, seen_limit=10)
    assert "Still open** (14 previously sent)" in text
    assert "(+4 more)" in text          # 14 seen, 10 shown -> 4 more
    assert len(shown) == 10             # only the 10 displayed are stamped
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest test_digest_dedup.py -k sections -v`
Expected: FAIL — `AttributeError: module 'fit' has no attribute 'build_digest_sections'`.

- [ ] **Step 3: Extract `_digest_line` and refactor `build_digest`**

In `fit.py`, add `_digest_line` just above `build_digest` (after `_short_age`):

```python
def _digest_line(j):
    """One digest line: `fit` . age . loc . **Company**: Title . [open](url).

    Display text is sanitized via _no_dash; the URL is NOT (Workday URLs
    contain '--' that _no_dash would corrupt)."""
    score = rank_key(j)
    age = _short_age(j.get("posted_at"), j.get("date_source"))
    loc = _LOC_ABBR.get(j.get("location_type") or "", "")
    meta = f"`{score:>2}` · {age:>4}"
    if loc:
        meta += f" · {loc}"
    line = f"{meta} · **{_no_dash(j.get('company', ''))}**: {_no_dash(j.get('title', ''))}"
    if j.get("url"):
        line += f" · [open]({j['url']})"
    return line
```

Then replace the per-line body inside `build_digest` (the block from `score = rank_key(j)` through `lines.append(line)`) with a single call, leaving the pending-verdict header logic intact:

```python
    for j in ordered:
        # Label the boundary where the un-vetted tier begins.
        if j.get("llm_fit_score") is None and not pending_header_done:
            lines.append("**Pending verdict (not yet scored):**")
            pending_header_done = True
        lines.append(_digest_line(j))
```

- [ ] **Step 4: Add `_seen_oneliner` and `build_digest_sections`**

In `fit.py`, add after `build_digest`:

```python
def _seen_oneliner(j):
    """Collapsed recap token for an already-sent lead: 'Company 88'."""
    return f"{_no_dash(j.get('company', ''))} {rank_key(j)}"


def build_digest_sections(new, seen, counts, new_limit=12, seen_limit=10):
    """Two-section digest: full lines for leads never sent ('New since last
    digest'), then a compact one-liner recap of still-open leads already sent.
    Both inputs are ranked here by sort_key. Returns (text, shown_uids) where
    shown_uids are the leads actually displayed, for the caller to stamp."""
    new_top = sorted(new, key=sort_key, reverse=True)[:new_limit]
    seen_sorted = sorted(seen, key=sort_key, reverse=True)
    seen_shown = seen_sorted[:seen_limit]

    lines = ["**Job-hound digest**  (fit · age · loc · role)", ""]
    if new_top:
        lines.append(f"**New since last digest** ({len(new_top)})")
        for j in new_top:
            lines.append(_digest_line(j))
    else:
        lines.append("No new leads today.")

    if seen_sorted:
        lines.append("")
        lines.append(f"**Still open** ({len(seen_sorted)} previously sent)")
        recap = " · ".join(_seen_oneliner(j) for j in seen_shown)
        extra = len(seen_sorted) - len(seen_shown)
        if extra > 0:
            recap += f" · (+{extra} more)"
        lines.append(recap)

    if counts:
        lines.append("")
        lines.append("Pipeline: " + " · ".join(f"{k} {v}" for k, v in counts.items()))

    shown_uids = [j["uid"] for j in new_top] + [j["uid"] for j in seen_shown]
    return "\n".join(lines), shown_uids
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest test_digest_dedup.py test_fit_digest.py -v`
Expected: PASS — new section tests pass AND the existing `test_fit_digest.py` still passes (the `_digest_line` refactor preserves `build_digest` output exactly).

- [ ] **Step 6: Commit**

```bash
git add fit.py test_digest_dedup.py
git commit -m "feat: two-section digest formatter (build_digest_sections)"
```

---

### Task 3: Partition in `refine_pipeline`

**Files:**
- Modify: `job_cli.py` (`refine_pipeline` ~line 402-458)
- Test: `test_digest_dedup.py` (append)

**Interfaces:**
- Consumes: `fit.build_digest_sections` (Task 2), `JobDB.mark_digested` exists but is NOT called here.
- Produces: `refine_pipeline(...)` return dict gains `"shown_uids": list[str]`. The digest now shows only `discovered` leads, partitioned New vs Still-open. The empty-active early return includes `"shown_uids": []`.

- [ ] **Step 1: Write the failing test**

Append to `test_digest_dedup.py`:

```python
def _refine(db):
    profile = fit.load_profile(None)
    return job_cli.refine_pipeline(db, profile=profile, master={}, top=10,
                                   no_llm=True, max_age=48, show_all=True)


def test_refine_partitions_new_then_still_open(tmp_path):
    db = jobdb.JobDB(tmp_path / "t.db")
    u1 = _seed(db, "1", "Staff SRE")
    u2 = _seed(db, "2", "Principal SRE")

    r1 = _refine(db)
    assert set(r1["shown_uids"]) == {u1, u2}
    assert "New since last digest" in r1["digest"]

    # Simulate a delivered digest, then refine again.
    db.mark_digested(r1["shown_uids"])
    r2 = _refine(db)
    assert "No new leads today." in r2["digest"]
    assert "Still open" in r2["digest"]

    # A brand-new lead shows up only in the New section.
    u3 = _seed(db, "3", "Senior SRE")
    r3 = _refine(db)
    assert "New since last digest** (1)" in r3["digest"]
    assert u3 in r3["shown_uids"]
    db.close()


def test_refine_digest_excludes_non_discovered(tmp_path):
    db = jobdb.JobDB(tmp_path / "t.db")
    _seed(db, "1", "Discovered SRE")
    _seed(db, "2", "Queued SRE", state="queued")
    r = _refine(db)
    assert "Discovered SRE" in r["digest"]
    assert "Queued SRE" not in r["digest"]
    db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest test_digest_dedup.py -k refine_partitions -v`
Expected: FAIL — `KeyError: 'shown_uids'` (refine_pipeline does not return it yet).

- [ ] **Step 3: Rewrite the filter/verdict/build section of `refine_pipeline`**

In `job_cli.py`, in the empty-active early return (~line 415-417), add `shown_uids`:

```python
    if not active:
        return {"digest": None, "shown_uids": [], "active": 0, "hidden": 0,
                "verify_hidden": 0, "verdict_failures": []}
```

Then replace everything from the freshness-filter comment (step 2 comment, ~line 425) through the `return` at the end of the function with:

```python
    # 2. Apply the same freshness/verify policy the list view uses.
    visible, verify_hidden = _verify_filter(active, show_all)
    fresh_rows, hidden = _fresh_filter(visible, max_age, show_all)

    # 3. The digest surfaces only leads not yet acted on. Leads already queued,
    #    drafted, or ready are in the working set and stay out of the digest.
    candidates = [j for j in fresh_rows if j["state"] == "discovered"]

    # 4. LLM verdict on the top-N discovered candidates by deterministic score.
    failures = []
    if not no_llm and api_key:
        history = fit.build_history(db)
        topn = sorted(candidates, key=lambda x: x["fit_score"], reverse=True)
        done = 0
        for j in topn:
            if done >= top:
                break
            if j.get("llm_fit_score") is not None:
                continue
            try:
                v = fit.verdict(j, master, history, api_key)
            except Exception as e:
                failures.append(f"{j['slug']}: {e}")
                done += 1
                continue
            db.set_fields(j["uid"], llm_fit_score=v["llm_fit_score"],
                          llm_rationale=v["llm_rationale"],
                          llm_coding_bar=v["llm_coding_bar"])
            j.update(v)
            done += 1

    # 5. Reload full rows (fresh verdicts + digested_at) and partition into
    #    never-sent (New) and already-sent (Still open).
    full = [dict(db.get(j["uid"])) for j in candidates]
    new = [j for j in full if not j.get("digested_at")]
    seen = [j for j in full if j.get("digested_at")]
    digest, shown_uids = fit.build_digest_sections(new, seen, db.counts())
    return {"digest": digest, "shown_uids": shown_uids, "active": len(active),
            "hidden": hidden, "verify_hidden": verify_hidden,
            "verdict_failures": failures}
```

(The step-1 deterministic scoring loop over `active` above this block is unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest test_digest_dedup.py -v`
Expected: PASS (all Task 1-3 tests).

- [ ] **Step 5: Commit**

```bash
git add job_cli.py test_digest_dedup.py
git commit -m "feat: partition digest into new vs still-open in refine_pipeline"
```

---

### Task 4: Stamp on successful delivery in `cmd_refine`

**Files:**
- Modify: `job_cli.py` (`cmd_refine` Discord-post block ~line 486-493)
- Test: `test_digest_dedup.py` (append)

**Interfaces:**
- Consumes: `refine_pipeline` result `shown_uids` (Task 3), `JobDB.mark_digested` (Task 1), `notify.post_discord`.
- Produces: `cmd_refine` stamps `digested_at` for `r["shown_uids"]` only when `notify.post_discord` returns truthy. A dry run (`--digest` absent) or a failed/absent-webhook post stamps nothing.

- [ ] **Step 1: Write the failing test**

Append to `test_digest_dedup.py`:

```python
import argparse


def _args(digest):
    return argparse.Namespace(top=10, no_llm=True, digest=digest,
                              profile=None, master=None, config=None,
                              max_age=48, all=True)


def test_cmd_refine_stamps_only_on_successful_post(tmp_path, monkeypatch):
    db = jobdb.JobDB(tmp_path / "t.db")
    uid = _seed(db, "1", "Staff SRE")

    import notify
    monkeypatch.setattr(notify, "post_discord", lambda hook, text: True)
    job_cli.cmd_refine(db, _args(digest=True))
    assert db.get(uid)["digested_at"] is not None
    db.close()


def test_cmd_refine_does_not_stamp_on_failed_post(tmp_path, monkeypatch):
    db = jobdb.JobDB(tmp_path / "t.db")
    uid = _seed(db, "1", "Staff SRE")

    import notify
    monkeypatch.setattr(notify, "post_discord", lambda hook, text: False)
    job_cli.cmd_refine(db, _args(digest=True))
    assert db.get(uid)["digested_at"] is None
    db.close()


def test_cmd_refine_dry_run_does_not_stamp(tmp_path):
    db = jobdb.JobDB(tmp_path / "t.db")
    uid = _seed(db, "1", "Staff SRE")
    job_cli.cmd_refine(db, _args(digest=False))
    assert db.get(uid)["digested_at"] is None
    db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest test_digest_dedup.py -k cmd_refine -v`
Expected: FAIL — `test_cmd_refine_stamps_only_on_successful_post` fails (`digested_at` is still None; nothing stamps yet).

- [ ] **Step 3: Stamp on success in `cmd_refine`**

In `job_cli.py`, in the `if args.digest:` block, add the stamp on the success branch:

```python
    if args.digest:
        cfg = load_cfg(args.config) if args.config else {}
        hook = os.environ.get("DISCORD_WEBHOOK_URL") or cfg.get("discord_webhook", "")
        if notify.post_discord(hook, r["digest"]):
            db.mark_digested(r["shown_uids"])
            print("\n(posted digest to Discord)")
        else:
            print("\n(no Discord webhook configured; digest not pushed)")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest test_digest_dedup.py -v`
Expected: PASS (all tests in the file).

- [ ] **Step 5: Commit**

```bash
git add job_cli.py test_digest_dedup.py
git commit -m "feat: stamp digested_at only after a successful digest post"
```

---

### Task 5: Full-suite regression and MCP adapter check

**Files:**
- Verify only: `job_hound_mcp.py` (confirm it consumes `refine_pipeline` without breaking on the new return shape and never stamps)
- Test: whole suite

**Interfaces:**
- Consumes: everything above. No code changes expected unless the suite reveals a break.

- [ ] **Step 1: Confirm the MCP adapter is unaffected**

Run: `grep -n "refine_pipeline\|build_digest\|shown_uids\|mark_digested" job_hound_mcp.py`
Expected: the adapter calls `refine_pipeline` and reads existing keys (e.g. `digest`, counts). It must NOT call `mark_digested`. The added `shown_uids` key is additive and ignored. If the adapter indexes a removed key, fix by reading from the current return dict. (No removals were made, so none expected.)

- [ ] **Step 2: Run the entire test suite**

Run: `python -m pytest -q`
Expected: PASS — all pre-existing tests plus the new `test_digest_dedup.py`. Pay attention to `test_cli_refine.py`, `test_fit_digest.py`, and `test_job_hound_mcp.py`.

- [ ] **Step 3: Smoke-test the CLI against a scratch DB (no network, no post)**

```bash
JOB_DB=/tmp/digest_smoke.db python job_cli.py refine --no-llm
```
Expected: prints a digest with a `**New since last digest**` (or `No new leads today.`) section and a pipeline line; no traceback. (A fresh scratch DB with no jobs prints `No active leads to refine.` — that is also acceptable; seed via `fetch` or point `JOB_DB` at a copy of the real DB to see sections.)

- [ ] **Step 4: Push the branch and open a PR**

```bash
git push -u origin feature/digest-dedup
gh pr create --base main --title "Digest dedup: two-section New vs Still open digest" \
  --body "Stops the daily digest repeating identical leads. Adds digested_at; refine_pipeline partitions discovered leads into New (never sent) and Still open (already sent); cmd_refine stamps only after a successful post. Spec: docs/superpowers/specs/2026-07-10-digest-dedup-design.md"
```
Expected: PR opens with the `tests` check running. Merge after it passes (main is protected).

---

## Self-Review

**Spec coverage:**
- `digested_at` column + additive migration → Task 1. ✓
- Partition New (NULL) vs Still-open (set), discovered-only → Task 3. ✓
- Stamp only on successful delivery; refine_pipeline side-effect-free; MCP never stamps → Task 4 + Task 5 Step 1. ✓
- Two-section formatting, caps 12/10, `(+N more)`, empty-New line, no em dashes → Task 2. ✓
- Out-of-scope (host key 401) correctly omitted (resolved operationally). ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code; every test step shows the assertion and the exact `pytest` command with expected result.

**Type consistency:** `build_digest_sections` returns `(text, shown_uids)` and is unpacked that way in `refine_pipeline` (Task 3 Step 3). `refine_pipeline` returns `shown_uids`, consumed by `cmd_refine` via `r["shown_uids"]` (Task 4 Step 3). `mark_digested(uids)` defined in Task 1, called in Task 4. Names consistent across tasks.

# Staleness Signal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface committed leads that have stopped moving, so a drafted package never again sits 24 days unsent without anything saying so.

**Architecture:** A pure `staleness.py` module computes idle time from `state_log` (never `jobs.updated_at`, which the nightly scorer bumps). One batched `jobdb.last_activity()` query feeds it. Three read-only surfaces render it: the CLI list, a new digest section, and a chip in the lead inbox UI jobs table. No schema change, no migration.

**Tech Stack:** Python 3.13, pytest, SQLite. TypeScript, Next.js 15, vitest, better-sqlite3 (read-only) on the lead inbox UI side.

Design spec: `docs/superpowers/specs/2026-07-26-staleness-signal-design.md`

## Global Constraints

- **No em dashes, ever.** Use commas, parentheses, or separate sentences. Applies to code comments, docstrings, commit messages, and any rendered string.
- **`staleness.py` stays pure and import-safe:** no network, no database, no file writes. All SQL lives in `jobdb.py`.
- **The clock is `state_log`, never `jobs.updated_at`.** The nightly scoring pass bumps `updated_at` without a state change, which would make every lead look permanently fresh.
- **Gate rows and read rows do not reset the clock.** Their `note` values start with `gate:` and `read:` respectively. Transitions, votes, and notes do reset it.
- **Bias to silence on bad data.** Missing rows or unparseable timestamps return "not stale", never an alarm.
- **Threshold:** `STALE_AFTER_DAYS = 7`. One constant, one data-model tier. The 14-day red in the lead inbox UI is display-only.
- **Committed states:** `queued`, `drafted`, `ready`, `interviewing`. Nothing else can be stale.
- Tests must not depend on wall-clock time. Inject `now=`. Do not add freezegun or any new dependency.
- Repo is at `~/code/job-hound` on branch `feat/staleness-signal`. Run Python from the venv: `source .venv/bin/activate`.

---

## File Structure

**job-hound (`~/code/job-hound`, branch `feat/staleness-signal`):**

| File | Responsibility |
|---|---|
| `staleness.py` (create) | Pure idle-time computation and labeling. Mirrors `freshness.py`. |
| `test_staleness.py` (create) | Unit tests for the pure module. |
| `jobdb.py` (modify) | Add `last_activity()`, the single batched query. |
| `test_staleness_db.py` (create) | Integration tests for `last_activity()` plus the Northgate regression. |
| `job_cli.py` (modify) | `fmt_row` marker, `cmd_list` wiring, `refine_pipeline` needs-attention set. |
| `fit.py` (modify) | `build_digest_sections` gains the Needs attention section. |
| `test_fit_digest.py` (modify) | Digest section tests. |

**lead-inbox (`~/code/lead-inbox`, new branch `feat/staleness-chip`):**

| File | Responsibility |
|---|---|
| `lib/job-hound.ts` (modify) | Batched last-activity query, `idleDays` on `JobListItem`. |
| `lib/job-format.ts` (modify) | `idleLabel` and `idleTone` formatters. |
| `components/JobTable.tsx` (modify) | Render the chip. |
| `tests/job-hound-mapping.test.ts` (modify) | Mapping test. |
| `tests/job-format.test.ts` (modify) | Formatter tests. |

Tasks 1 to 5 are job-hound and land as one PR. Task 6 is lead-inbox and lands as its own PR. They are independent: the CLI and digest are useful without the chip.

---

### Task 1: The pure staleness module

**Files:**
- Create: `staleness.py`
- Test: `test_staleness.py`

**Interfaces:**
- Consumes: nothing. This is the base layer.
- Produces: `COMMITTED_STATES: set[str]`, `STALE_AFTER_DAYS: int`, `idle_days(last_activity_at: str | None, now: datetime | None = None) -> float | None`, `is_stale(state: str | None, last_activity_at: str | None, now: datetime | None = None) -> bool`, `staleness_label(state: str | None, last_activity_at: str | None, now: datetime | None = None) -> str | None`

- [ ] **Step 1: Write the failing tests**

Create `test_staleness.py`:

```python
"""Tests for staleness.py, the 'how long since the operator acted' clock."""

from datetime import datetime, timedelta, timezone

import staleness as st

NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)


def ago(days):
    """ISO timestamp `days` before NOW."""
    return (NOW - timedelta(days=days)).isoformat()


def test_idle_days_counts_whole_and_partial_days():
    assert st.idle_days(ago(3), now=NOW) == 3.0
    assert round(st.idle_days(ago(0.5), now=NOW), 2) == 0.5


def test_idle_days_is_none_when_undatable():
    assert st.idle_days(None, now=NOW) is None
    assert st.idle_days("", now=NOW) is None
    assert st.idle_days("not a timestamp", now=NOW) is None


def test_the_seven_day_boundary():
    assert st.is_stale("drafted", ago(6.9), now=NOW) is False
    assert st.is_stale("drafted", ago(7.1), now=NOW) is True


def test_every_committed_state_can_go_stale():
    for state in ("queued", "drafted", "ready", "interviewing"):
        assert st.is_stale(state, ago(30), now=NOW) is True, state


def test_uncommitted_states_never_go_stale():
    # discovered has cost nothing yet; the terminal states are done. Warning
    # about either is noise that trains the operator to ignore the signal.
    for state in ("discovered", "applied", "skipped", "closed"):
        assert st.is_stale(state, ago(365), now=NOW) is False, state


def test_bad_data_is_silent_rather_than_alarming():
    # A missing or unreadable clock must never manufacture a warning.
    assert st.is_stale("drafted", None, now=NOW) is False
    assert st.is_stale("drafted", "garbage", now=NOW) is False
    assert st.is_stale(None, ago(30), now=NOW) is False


def test_label_reads_as_days_or_is_none():
    assert st.staleness_label("drafted", ago(24), now=NOW) == "idle 24d"
    assert st.staleness_label("drafted", ago(2), now=NOW) is None
    assert st.staleness_label("discovered", ago(99), now=NOW) is None
    # None, not "", so callers can tell "fresh" from "stale but unlabelable".
    assert st.staleness_label("drafted", None, now=NOW) is None


def test_future_timestamps_are_not_stale():
    # Clock skew between hosts should not read as negative idle time.
    future = (NOW + timedelta(days=2)).isoformat()
    assert st.is_stale("drafted", future, now=NOW) is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `source .venv/bin/activate && python -m pytest test_staleness.py -v`
Expected: FAIL, collection error `ModuleNotFoundError: No module named 'staleness'`

- [ ] **Step 3: Write the implementation**

Create `staleness.py`:

```python
#!/usr/bin/env python3
"""
staleness.py - how long since the operator acted on a lead.

Sibling to freshness.py, and deliberately a different question. freshness.py
asks how old a POSTING is. This asks how long a lead the operator already committed to
has sat without him doing anything about it.

The motivating bug: Northgate (score 93) reached `drafted` on 2026-07-02 with
generated documents and was never submitted. Nothing surfaced it for 24 days.
A generated package that is never sent is the most wasteful state in the
pipeline, because the tailoring cost was paid and no application resulted.

The clock is state_log, never jobs.updated_at: the nightly scoring pass bumps
updated_at without changing state, so it would make every lead look
permanently fresh. Gate runs and read stamps are excluded by the caller
(jobdb.last_activity) because neither is a decision about the lead; counting
them would let a lead quiet its own alarm.

Pure and import-safe: no network, no database, no file writes.
"""

from datetime import datetime, timezone

import freshness as fr

# Only a lead the operator has committed to can go stale. `discovered` has cost
# nothing yet, and the terminal states are done.
COMMITTED_STATES = {"queued", "drafted", "ready", "interviewing"}

STALE_AFTER_DAYS = 7


def idle_days(last_activity_at, now=None):
    """Days since the last real action, or None if undatable.

    Reuses freshness.parse_iso so both clocks read timestamps the same way.
    """
    dt = fr.parse_iso(last_activity_at)
    if not dt:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - dt).total_seconds() / 86400.0


def is_stale(state, last_activity_at, now=None):
    """True when a committed lead has sat untouched past the threshold.

    Every uncertain path returns False. A warning that cannot be trusted is
    worse than no warning, because it trains the reader to skip the section.
    """
    if state not in COMMITTED_STATES:
        return False
    days = idle_days(last_activity_at, now=now)
    if days is None:
        return False
    return days >= STALE_AFTER_DAYS


def staleness_label(state, last_activity_at, now=None):
    """'idle 24d' for a stale lead, else None.

    None rather than an empty string so a caller can distinguish "fresh" from
    "stale but unlabelable" without a second call.
    """
    if not is_stale(state, last_activity_at, now=now):
        return None
    days = idle_days(last_activity_at, now=now)
    return f"idle {int(days)}d"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `source .venv/bin/activate && python -m pytest test_staleness.py -v`
Expected: PASS, 8 tests

- [ ] **Step 5: Commit**

```bash
git add staleness.py test_staleness.py
git commit -m "$(cat <<'EOF'
[staleness]: the 'how long since I acted' clock

Sibling to freshness.py and a deliberately different question: that
one asks how old a posting is, this asks how long a committed lead has
sat untouched. Pure, import-safe, no database access.

Every uncertain path returns not-stale. A warning that cannot be
trusted trains the reader to skip the section.
EOF
)"
```

---

### Task 2: The batched last-activity query

**Files:**
- Modify: `jobdb.py` (add a method after `history()`, currently at line 576)
- Test: `test_staleness_db.py` (create)

**Interfaces:**
- Consumes: nothing from Task 1 (this is SQL only).
- Produces: `JobDB.last_activity(uids=None) -> dict[str, str]`, mapping job uid to the ISO timestamp of its most recent qualifying `state_log` row. Jobs with no qualifying rows are absent from the dict.

- [ ] **Step 1: Write the failing tests**

Create `test_staleness_db.py`:

```python
"""Integration tests: the last-activity clock read from state_log."""

import jobdb


def make_job(db, slug_id, state="discovered"):
    """Insert a job and return its uid."""
    db.upsert_job({"ats": "greenhouse", "company": "acme", "id": slug_id,
                   "title": "Platform Engineer", "location": "Remote",
                   "url": "https://example.test/1", "posted_at": "",
                   "date_source": ""})
    return jobdb.make_job_uid("greenhouse", "acme", slug_id)


def test_last_activity_returns_the_most_recent_row(tmp_path):
    db = jobdb.JobDB(str(tmp_path / "t.db"))
    uid = make_job(db, "1")
    db.set_state(uid, "queued")
    db.set_state(uid, "drafted")
    out = db.last_activity()
    hist = db.history(uid)
    assert out[uid] == max(r["at"] for r in hist)
    db.close()


def test_gate_rows_do_not_count_as_activity(tmp_path):
    # A gate run is not a decision about the lead. If it reset the clock, a
    # lead could quiet its own alarm without the operator doing anything.
    db = jobdb.JobDB(str(tmp_path / "t.db"))
    uid = make_job(db, "1")
    db.set_state(uid, "queued")
    before = db.last_activity()[uid]
    db.set_gate(uid, "PROCEED", "{}", "/tmp/report.md")
    after = db.last_activity()[uid]
    assert after == before
    db.close()


def test_read_stamps_do_not_count_as_activity(tmp_path):
    db = jobdb.JobDB(str(tmp_path / "t.db"))
    uid = make_job(db, "1")
    db.set_state(uid, "queued")
    before = db.last_activity()[uid]
    db.set_read(uid, True)
    assert db.last_activity()[uid] == before
    db.close()


def test_votes_and_notes_do_count_as_activity(tmp_path):
    # Both are judgments about the lead, so both mean the operator acted.
    db = jobdb.JobDB(str(tmp_path / "t.db"))
    uid_a = make_job(db, "1")
    uid_b = make_job(db, "2")
    db.set_state(uid_a, "queued")
    db.set_state(uid_b, "queued")
    before_a = db.last_activity()[uid_a]
    before_b = db.last_activity()[uid_b]
    db.set_vote(uid_a, "up", "")
    db.set_note(uid_b, "left a follow up")
    assert db.last_activity()[uid_a] > before_a
    assert db.last_activity()[uid_b] > before_b
    db.close()


def test_uids_filter_narrows_the_result(tmp_path):
    db = jobdb.JobDB(str(tmp_path / "t.db"))
    uid_a = make_job(db, "1")
    uid_b = make_job(db, "2")
    out = db.last_activity(uids=[uid_a])
    assert uid_a in out
    assert uid_b not in out
    db.close()


def test_one_row_per_job(tmp_path):
    db = jobdb.JobDB(str(tmp_path / "t.db"))
    uid = make_job(db, "1")
    db.set_state(uid, "queued")
    db.set_state(uid, "drafted")
    db.set_state(uid, "ready")
    out = db.last_activity()
    assert list(out).count(uid) == 1
    db.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `source .venv/bin/activate && python -m pytest test_staleness_db.py -v`
Expected: FAIL with `AttributeError: 'JobDB' object has no attribute 'last_activity'`

- [ ] **Step 3: Check what the audit notes actually look like**

Before writing the filter, confirm the exact note prefixes that gate and read rows use. Run:

```bash
source .venv/bin/activate && python - <<'PY'
import re, pathlib
src = pathlib.Path("jobdb.py").read_text()
for m in re.finditer(r'INSERT INTO state_log.*?\n(.*?\n){0,4}', src):
    print(m.group(0)[:220].replace("\n", " "))
    print("---")
PY
```

Read the output and note the literal note strings written by `set_gate` and `set_read`. The implementation below assumes they begin with `gate:` and `read:` respectively. **If they differ, use the actual prefixes** and update the docstring accordingly.

- [ ] **Step 4: Write the implementation**

In `jobdb.py`, immediately after the `history()` method (which ends around line 580), add:

```python
    def last_activity(self, uids=None):
        """Map uid to the ISO timestamp of the last time the operator ACTED on it.

        The clock behind staleness.py. Reads state_log rather than
        jobs.updated_at, which the nightly scoring pass bumps without any
        state change (that would make every lead look permanently fresh).

        Gate runs and read stamps are excluded: neither is a decision about
        the lead, and counting them would let a lead quiet its own staleness
        alarm without the operator doing anything. Transitions, votes, and notes all
        count.

        Jobs with no qualifying rows are absent from the result, which callers
        must treat as "unknown", not "stale".
        """
        q = ("SELECT job_uid, MAX(at) AS at FROM state_log "
             "WHERE COALESCE(note, '') NOT LIKE 'gate:%' "
             "AND COALESCE(note, '') NOT LIKE 'read:%'")
        params = []
        if uids is not None:
            uids = list(uids)
            if not uids:
                return {}
            q += " AND job_uid IN (%s)" % ",".join("?" * len(uids))
            params.extend(uids)
        q += " GROUP BY job_uid"
        return {r["job_uid"]: r["at"]
                for r in self.conn.execute(q, params).fetchall()}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `source .venv/bin/activate && python -m pytest test_staleness_db.py -v`
Expected: PASS, 6 tests

If `test_gate_rows_do_not_count_as_activity` or `test_read_stamps_do_not_count_as_activity` fails, the note prefix assumption from Step 3 was wrong. Fix the `LIKE` patterns to match the real note text, do not change the test.

- [ ] **Step 6: Run the full suite for regressions**

Run: `source .venv/bin/activate && python -m pytest -q`
Expected: PASS, 515 passed 1 skipped plus the 14 new tests

- [ ] **Step 7: Commit**

```bash
git add jobdb.py test_staleness_db.py
git commit -m "$(cat <<'EOF'
[jobdb]: last_activity, the clock behind the staleness signal

One batched GROUP BY over state_log, so a list render costs one query
rather than one per row.

Excludes gate and read rows. Neither is a decision about the lead, and
counting them would let a lead quiet its own staleness alarm without
the operator having done anything.
EOF
)"
```

---

### Task 3: The CLI list marker

**Files:**
- Modify: `job_cli.py` (`fmt_row` at line 80, `cmd_list` at line 223)
- Test: `test_staleness_cli.py` (create)

**Interfaces:**
- Consumes: `staleness.staleness_label` (Task 1), `JobDB.last_activity` (Task 2).
- Produces: `fmt_row(r, show_age=True, idle_label=None)`. The new third parameter is keyword-optional, so every existing caller keeps working unchanged.

- [ ] **Step 1: Write the failing test**

Create `test_staleness_cli.py`:

```python
"""The staleness marker on CLI list rows."""

import job_cli


def row(**over):
    base = {"state": "drafted", "slug": "acme__platform-engineer__ab12",
            "title": "Platform Engineer", "company": "acme",
            "location": "Remote", "posted_at": "", "date_source": "",
            "fit_score": 90, "llm_fit_score": None}
    base.update(over)
    return base


def test_idle_label_is_appended_to_the_age_line():
    out = job_cli.fmt_row(row(), idle_label="idle 24d")
    assert "idle 24d" in out
    # Same number of lines as without it: the marker rides the age line
    # rather than making every stale row taller.
    assert len(out.splitlines()) == len(job_cli.fmt_row(row()).splitlines())


def test_no_marker_when_not_stale():
    assert "idle" not in job_cli.fmt_row(row(), idle_label=None)


def test_marker_survives_a_row_with_no_age_data():
    out = job_cli.fmt_row(row(), idle_label="idle 9d")
    assert "age unknown" in out
    assert "idle 9d" in out
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `source .venv/bin/activate && python -m pytest test_staleness_cli.py -v`
Expected: FAIL with `TypeError: fmt_row() got an unexpected keyword argument 'idle_label'`

- [ ] **Step 3: Add the import**

In `job_cli.py`, find the existing import of the freshness module (it is imported as `fr`, used at line 85). Add `staleness` alongside it:

```python
import staleness as stl
```

- [ ] **Step 4: Modify fmt_row**

Replace the whole `fmt_row` function (line 80 to 89) with:

```python
def fmt_row(r, show_age=True, idle_label=None):
    score = fit.rank_key(dict(r))
    line = f"[{r['state']:>12}] [{score:>3}] {r['slug']}\n               {r['title']} @ {r['company']} ({r['location'] or 'n/a'})"
    if show_age:
        try:
            label = fr.freshness_label(r["posted_at"], r["date_source"])
        except (KeyError, IndexError, TypeError):
            label = "age unknown"
        # The staleness marker rides the age line rather than adding one, so
        # a stale row is no taller than any other. Uppercase, not ANSI: this
        # output is piped as often as it is read in a terminal.
        if idle_label:
            label += f" · {idle_label.upper()}"
        line += f"\n               {label}"
    return line
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `source .venv/bin/activate && python -m pytest test_staleness_cli.py -v`
Expected: PASS, 3 tests

- [ ] **Step 6: Wire cmd_list to pass the label**

In `job_cli.py`, in `cmd_list`, replace this loop:

```python
    for r in rows:
        print(fmt_row(r))
```

with:

```python
    activity = db.last_activity(uids=[r["uid"] for r in rows])
    for r in rows:
        print(fmt_row(r, idle_label=stl.staleness_label(
            r["state"], activity.get(r["uid"]))))
```

- [ ] **Step 7: Verify against the real host database**

This is the first end-to-end check, and the pipeline has a known stale lead to prove it. The Northgate row was skipped on 2026-07-26, so it is no longer in a committed state; use the queued and drafted leads instead.

Run: `./bin/jh list --state queued --all`

Expected: rows render normally. Any lead whose last real action was 7 or more days ago carries `· IDLE Nd` on its age line. As of 2026-07-26 the queued leads sat at 2 to 5 days idle, so **no marker is the correct result**, and that is the negative case worth confirming. To confirm the positive case, run `./bin/jh list --all` and look for any committed lead with a marker.

Record what you saw. If nothing anywhere is marked, verify the query returns data at all:

```bash
ssh $JOB_HOST 'sqlite3 -cmd ".timeout 5000" ~/job-hound/jobs.db "SELECT j.slug, MAX(s.at) FROM jobs j JOIN state_log s ON s.job_uid=j.uid WHERE j.state IN (\"queued\",\"drafted\",\"ready\",\"interviewing\") GROUP BY j.uid;"'
```

- [ ] **Step 8: Commit**

```bash
git add job_cli.py test_staleness_cli.py
git commit -m "$(cat <<'EOF'
[cli]: mark idle committed leads in the list view

The marker rides the existing age line, so a stale row is no taller
than any other. Plain uppercase rather than ANSI colour: this output
is piped as often as it is read in a terminal, and the codebase has no
colour helper today.

One batched last_activity query per render, not one per row.
EOF
)"
```

---

### Task 4: The digest Needs attention section

**Files:**
- Modify: `fit.py` (`build_digest_sections` at line 391)
- Test: `test_fit_digest.py` (modify, add cases)

**Interfaces:**
- Consumes: `staleness.staleness_label` (Task 1).
- Produces: `build_digest_sections(new, seen, counts, new_limit=12, seen_limit=10, stale=None)`. The `stale` parameter is a list of job dicts, each carrying an extra `idle_label` key (a string like `"idle 24d"`). Default `None` means no section, so every existing caller keeps working.

- [ ] **Step 1: Write the failing tests**

Add to `test_fit_digest.py`:

```python
def test_needs_attention_section_leads_the_digest():
    # This section is the whole point of the feature: it arrives on cron
    # whether or not the operator goes looking, which is what the digest's other
    # sections never did for a committed lead.
    stale = [{"uid": "u1", "company": "Northgate", "title": "SRE Team Lead",
              "url": "https://example.test/1", "fit_score": 93,
              "llm_fit_score": None, "posted_at": "", "date_source": "",
              "location_type": "", "idle_label": "idle 24d"}]
    text, _ = fit.build_digest_sections([], [], {}, stale=stale)
    assert "Needs attention" in text
    assert "idle 24d" in text
    assert "Northgate" in text
    # It must come before the new-leads section, because it is the part
    # about the operator rather than about the market.
    assert text.index("Needs attention") < text.index("No new leads today.")


def test_no_section_when_nothing_is_stale():
    # A clean pipeline must stay quiet, or the section becomes wallpaper.
    text, _ = fit.build_digest_sections([], [], {}, stale=[])
    assert "Needs attention" not in text
    text2, _ = fit.build_digest_sections([], [], {}, stale=None)
    assert "Needs attention" not in text2


def test_stale_leads_are_not_marked_digested():
    # shown_uids drives mark_digested, which controls the New vs Still open
    # split. A stale lead is a recurring nag, not a one-time announcement,
    # so it must keep appearing until the operator acts.
    stale = [{"uid": "stale-uid", "company": "Northgate", "title": "SRE",
              "url": "", "fit_score": 93, "llm_fit_score": None,
              "posted_at": "", "date_source": "", "location_type": "",
              "idle_label": "idle 24d"}]
    _, shown = fit.build_digest_sections([], [], {}, stale=stale)
    assert "stale-uid" not in shown
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `source .venv/bin/activate && python -m pytest test_fit_digest.py -v -k "needs_attention or nothing_is_stale or not_marked_digested"`
Expected: FAIL with `TypeError: build_digest_sections() got an unexpected keyword argument 'stale'`

- [ ] **Step 3: Write the implementation**

In `fit.py`, replace the signature and the opening of `build_digest_sections`. The current signature is:

```python
def build_digest_sections(new, seen, counts, new_limit=12, seen_limit=10):
```

Replace the function's signature, docstring, and the two lines that build `lines`, with:

```python
def build_digest_sections(new, seen, counts, new_limit=12, seen_limit=10,
                          stale=None):
    """Digest with up to three sections: leads that need action, then leads
    never sent, then a compact recap of still-open leads already sent.

    `stale` is a list of committed leads that have sat untouched (see
    staleness.py), each carrying an `idle_label` key. It leads the digest
    because it is the only part about the operator rather than about the market, and
    it is omitted entirely when empty so a clean pipeline stays quiet.

    Stale leads are deliberately NOT in the returned shown_uids: that list
    drives mark_digested, and a nag that stops after one appearance is not a
    nag. They keep appearing until the operator acts on them.

    Both `new` and `seen` are ranked here by sort_key. Returns
    (text, shown_uids).
    """
    new_top = sorted(new, key=sort_key, reverse=True)[:new_limit]
    seen_sorted = sorted(seen, key=sort_key, reverse=True)
    seen_shown = seen_sorted[:seen_limit]

    lines = ["**Job-hound digest**  (fit · age · loc · role)", ""]

    if stale:
        stale_sorted = sorted(stale, key=sort_key, reverse=True)
        lines.append(f"**Needs attention** ({len(stale_sorted)})")
        for j in stale_sorted:
            lines.append(_stale_digest_line(j))
        lines.append("")

    if new_top:
```

The rest of the function body (from `lines.append(f"**New since last digest**...` onward) is unchanged.

- [ ] **Step 4: Add the line renderer**

In `fit.py`, immediately before `build_digest_sections`, add:

```python
def _stale_digest_line(j):
    """One Needs attention line: `fit` . idle . **Company**: Title . [open].

    Mirrors _digest_line but swaps posting age for idle time, which is the
    only number that matters once a lead is already committed to.
    """
    score = rank_key(j)
    idle = j.get("idle_label", "")
    line = f"`{score:>2}` · {idle} · **{_no_dash(j.get('company', ''))}**: {_no_dash(j.get('title', ''))}"
    if j.get("url"):
        line += f" · [open]({j['url']})"
    return line
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `source .venv/bin/activate && python -m pytest test_fit_digest.py -v`
Expected: PASS, all existing tests plus the 3 new ones

- [ ] **Step 6: Run the full suite**

Run: `source .venv/bin/activate && python -m pytest -q`
Expected: PASS, no regressions in `test_digest_dedup.py` or `test_cli_refine.py`

- [ ] **Step 7: Commit**

```bash
git add fit.py test_fit_digest.py
git commit -m "$(cat <<'EOF'
[digest]: a Needs attention section for leads that stopped moving

Leads the digest because it is the only part about the operator rather than
about the market, and is omitted entirely when empty so a clean
pipeline stays quiet.

Stale leads are deliberately not added to shown_uids. That list drives
mark_digested, and a nag that stops after one appearance is not a nag.
EOF
)"
```

---

### Task 5: Wire the digest section into refine_pipeline

**Files:**
- Modify: `job_cli.py` (`refine_pipeline`, the `active` list at line ~600 and the `build_digest_sections` call at line ~605)
- Test: `test_cli_refine.py` (modify)

**Interfaces:**
- Consumes: `staleness.staleness_label` (Task 1), `JobDB.last_activity` (Task 2), `fit.build_digest_sections(..., stale=)` (Task 4).
- Produces: `refine_pipeline` result dict gains a `stale` key (a list of job dicts with `idle_label`), alongside the existing `digest`, `shown_uids`, `active`, `hidden`, `verify_hidden`, `verdict_failures`.

**Why this task is separate:** `refine_pipeline` filters twice before building the digest, and both filters would swallow exactly the leads this feature exists to surface. The `stale` set must be computed from `active`, upstream of both. This is the actual root cause of the Northgate miss:

```python
# The existing line 3, which excludes every committed state:
candidates = [j for j in fresh_rows if j["state"] == "discovered"]
```

- [ ] **Step 1: Write the failing test**

Add to `test_cli_refine.py`:

```python
def test_stale_committed_leads_reach_the_digest(tmp_path, monkeypatch):
    """The Northgate regression.

    refine_pipeline filters to `discovered` only and applies a posting-age
    filter before building the digest. Both would swallow a committed lead
    with an old posting, which is exactly what let a drafted package sit 24
    days unsent. The stale set must be computed upstream of both.
    """
    import job_cli
    import jobdb

    db = jobdb.JobDB(str(tmp_path / "t.db"))
    db.upsert_job({"ats": "greenhouse", "company": "omnicell", "id": "1",
                   "title": "Site Reliability Engineer, Team Lead",
                   "location": "Remote", "url": "https://example.test/1",
                   "posted_at": "", "date_source": ""})
    uid = jobdb.make_job_uid("greenhouse", "omnicell", "1")
    db.set_state(uid, "queued")
    db.set_state(uid, "drafted")
    # Backdate every audit row to 24 days ago, reproducing the real history.
    db.conn.execute(
        "UPDATE state_log SET at = datetime('now', '-24 days') WHERE job_uid = ?",
        (uid,))
    db.conn.commit()

    r = job_cli.refine_pipeline(db, profile=fit.load_profile(None),
                                master={}, no_llm=True)
    stale_uids = [j["uid"] for j in r["stale"]]
    assert uid in stale_uids
    assert "Needs attention" in r["digest"]
    db.close()
```

If `fit.load_profile(None)` does not work with a None argument, read the existing tests in `test_cli_refine.py` and use whatever profile-construction pattern they already use.

- [ ] **Step 2: Run the test to verify it fails**

Run: `source .venv/bin/activate && python -m pytest test_cli_refine.py -v -k stale_committed`
Expected: FAIL with `KeyError: 'stale'`

- [ ] **Step 3: Compute the stale set upstream of both filters**

In `job_cli.py`'s `refine_pipeline`, immediately after the `active` list is built and the early-return guard, add:

```python
    # Computed from `active`, deliberately upstream of BOTH filters below.
    # The freshness filter drops old postings and step 3 keeps only
    # `discovered`, so a committed lead with an old posting would be invisible
    # to the digest. That is exactly how a drafted Northgate package sat 24
    # days unsent, and this section exists to stop it recurring.
    activity = db.last_activity(uids=[j["uid"] for j in active])
    stale = []
    for j in active:
        label = stl.staleness_label(j["state"], activity.get(j["uid"]))
        if label:
            j["idle_label"] = label
            stale.append(j)
```

Note: the early-return guard (`if not active:`) returns a dict that must also gain the new key. Update it to include `"stale": []`.

- [ ] **Step 4: Pass it to the digest builder and the result**

In the same function, change:

```python
    digest, shown_uids = fit.build_digest_sections(new, seen, db.counts())
    return {"digest": digest, "shown_uids": shown_uids, "active": len(active),
            "hidden": hidden, "verify_hidden": verify_hidden,
            "verdict_failures": failures}
```

to:

```python
    digest, shown_uids = fit.build_digest_sections(new, seen, db.counts(),
                                                   stale=stale)
    return {"digest": digest, "shown_uids": shown_uids, "active": len(active),
            "hidden": hidden, "verify_hidden": verify_hidden,
            "verdict_failures": failures, "stale": stale}
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `source .venv/bin/activate && python -m pytest test_cli_refine.py -v`
Expected: PASS, all tests

- [ ] **Step 6: Run the full suite**

Run: `source .venv/bin/activate && python -m pytest -q`
Expected: PASS

- [ ] **Step 7: Verify against the real pipeline without spending API budget**

Run: `./bin/jh refine --no-llm`

Expected: the digest prints. If any committed lead is 7 or more days idle, a **Needs attention** section leads it. `--no-llm` means deterministic scoring only, so this costs nothing.

Do NOT run `./bin/jh refine --digest` during verification: that posts to Discord and stamps `digested_at`.

- [ ] **Step 8: Commit**

```bash
git add job_cli.py test_cli_refine.py
git commit -m "$(cat <<'EOF'
[refine]: compute the stale set upstream of both digest filters

refine_pipeline applies a posting-age filter and then keeps only
`discovered` leads before building the digest. Both would swallow a
committed lead with an old posting, which is precisely how a drafted
Northgate package sat 24 days unsent with nothing surfacing it.

The stale set is therefore computed from `active`, before either
filter runs. Regression test reproduces the real 24-day history.
EOF
)"
```

- [ ] **Step 9: Open the job-hound PR**

```bash
git push -u origin feat/staleness-signal
gh pr create --repo jrivers/job-hound --base main \
  --title "Surface committed leads that stopped moving" \
  --body "Implements docs/superpowers/specs/2026-07-26-staleness-signal-design.md

Northgate reached \`drafted\` on 2026-07-02 with generated documents and was never submitted. Nothing surfaced it for 24 days. This adds the signal that would have caught it on day 7.

New \`staleness.py\` (pure, mirrors \`freshness.py\`) plus one batched \`jobdb.last_activity()\` query. Two surfaces here: an \`IDLE Nd\` marker on CLI list rows, and a **Needs attention** section leading the daily digest, omitted when the pipeline is clean.

The root cause was in \`refine_pipeline\`: it filters to \`discovered\` only and applies a posting-age filter before building the digest, so a committed lead with an old posting was structurally invisible. The stale set is now computed upstream of both filters, with a regression test reproducing the real 24-day history.

The clock is \`state_log\`, never \`jobs.updated_at\` (the nightly scorer bumps it without a state change). Gate runs and read stamps are excluded so a lead cannot quiet its own alarm.

No migration: works on all 411 existing rows immediately.

The lead inbox UI chip is a separate PR."
```

---

### Task 6: The lead inbox UI chip

**Files:**
- Modify: `~/code/lead-inbox/lib/job-hound.ts`
- Modify: `~/code/lead-inbox/lib/job-format.ts`
- Modify: `~/code/lead-inbox/components/JobTable.tsx`
- Test: `~/code/lead-inbox/tests/job-format.test.ts`
- Test: `~/code/lead-inbox/tests/job-hound-mapping.test.ts`

**Interfaces:**
- Consumes: nothing from Tasks 1 to 5. This repo reads the same `state_log` table directly, read-only. It does NOT import Python.
- Produces: `idleDays: number | null` on `JobListItem`; `idleLabel(idleDays: number | null): string | null` and `idleTone(idleDays: number | null): string` in `lib/job-format.ts`.

**Setup:** this task is in a different repository. Start with:

```bash
cd ~/code/lead-inbox
git checkout main && git pull --ff-only origin main
git checkout -b feat/staleness-chip
```

- [ ] **Step 1: Write the failing formatter tests**

Add to `tests/job-format.test.ts`:

```typescript
describe('idleLabel and idleTone', () => {
  it('labels a stale lead and stays silent on a fresh one', () => {
    expect(idleLabel(24)).toBe('idle 24d')
    expect(idleLabel(7)).toBe('idle 7d')
    expect(idleLabel(6)).toBeNull()
    expect(idleLabel(null)).toBeNull()
  })

  it('escalates the tone past two weeks', () => {
    // Display-only second tier. The data model has one threshold; this
    // distinguishes "nudge" from "this is dying" without a second constant
    // in the Python.
    expect(idleTone(8)).toContain('ffb86c')   // amber
    expect(idleTone(15)).toContain('ff5555')  // red
  })
})
```

Add the import to the existing import statement at the top of the file.

- [ ] **Step 2: Run to verify failure**

Run: `cd ~/code/lead-inbox && npx vitest run tests/job-format.test.ts`
Expected: FAIL, `idleLabel is not defined`

- [ ] **Step 3: Write the formatters**

Add to `lib/job-format.ts`:

```typescript
// Mirrors staleness.py's STALE_AFTER_DAYS. Kept in sync by hand: the two
// repos share a database, not a codebase.
const STALE_AFTER_DAYS = 7
const URGENT_AFTER_DAYS = 14

export function idleLabel(idleDays: number | null): string | null {
  if (idleDays === null || idleDays < STALE_AFTER_DAYS) return null
  return `idle ${Math.floor(idleDays)}d`
}

// Static, never animated. A blinking row on a dashboard that stays open all
// day reads as an alarm you train yourself to ignore, and it fights
// prefers-reduced-motion.
export function idleTone(idleDays: number | null): string {
  if (idleDays !== null && idleDays >= URGENT_AFTER_DAYS) return 'text-[#ff5555]'
  return 'text-[#ffb86c]'
}
```

- [ ] **Step 4: Run to verify pass**

Run: `npx vitest run tests/job-format.test.ts`
Expected: PASS

- [ ] **Step 5: Write the failing mapping test**

Add to `tests/job-hound-mapping.test.ts`, following the existing `baseRow` pattern in that file:

```typescript
it('maps idleDays from the activity map, null when absent', () => {
  const row = { ...baseRow, uid: 'u1', state: 'drafted' }
  expect(mapJobRow(row, { u1: 24 }).idleDays).toBe(24)
  expect(mapJobRow(row, {}).idleDays).toBeNull()
})
```

Read the existing tests in that file first and match how `mapJobRow` (or whatever the mapping function is actually named there) is called. Adjust the call signature to match reality rather than assuming.

- [ ] **Step 6: Add idleDays to the type and mapping**

In `lib/job-hound.ts`:

1. Add to the `JobListItem` type, after `updatedAt: string | null` (line 57):

```typescript
  idleDays: number | null
```

2. Add the batched query. The file already reads `state_log` per job at line 428; this is the list-view equivalent:

```typescript
/**
 * Days since the last real action per job uid.
 *
 * Mirrors jobdb.last_activity(): reads state_log, not jobs.updated_at (the
 * nightly scorer bumps that without a state change), and excludes gate and
 * read rows because neither is a decision about the lead.
 */
function lastActivityDays(db: Database, now: number = Date.now()): Record<string, number> {
  const rows = db
    .prepare(
      `SELECT job_uid, MAX(at) AS at FROM state_log
       WHERE COALESCE(note, '') NOT LIKE 'gate:%'
         AND COALESCE(note, '') NOT LIKE 'read:%'
       GROUP BY job_uid`,
    )
    .all() as { job_uid: string; at: string }[]
  const out: Record<string, number> = {}
  for (const r of rows) {
    const ts = Date.parse(r.at)
    if (Number.isNaN(ts)) continue
    out[r.job_uid] = (now - ts) / 86_400_000
  }
  return out
}
```

3. In the mapping function, add `idleDays` beside the existing `updatedAt` mapping (line 288). The mapping function needs access to the activity map; thread it through as a second parameter, defaulting to `{}` so existing callers keep working:

```typescript
    idleDays: activity[row.uid] ?? null,
```

4. In the list-loading function (the one at line 375 that does `SELECT * FROM jobs`), call `lastActivityDays(db)` once and pass it into the mapping.

**Important:** only committed states may show a chip. Either filter in the mapping (`COMMITTED.has(row.state) ? days : null`) or in the render. Choose one and do it in exactly one place. The committed set is `queued`, `drafted`, `ready`, `interviewing`.

- [ ] **Step 7: Render the chip**

In `components/JobTable.tsx`, add the chip to the age cell (the `<td>` currently rendering `ageLabel`). Import `idleLabel` and `idleTone` alongside the existing `gateTone`/`scoreTone` imports on line 8, then render:

```tsx
        {idleLabel(job.idleDays) && (
          <span className={`ml-2 ${idleTone(job.idleDays)}`}>
            {idleLabel(job.idleDays)}
          </span>
        )}
```

- [ ] **Step 8: Run all four gates**

```bash
npm test
npx tsc --noEmit
npm run lint
npm run build
```

Expected: all four clean. Note `react-hooks/set-state-in-effect` is enforced in this repo, and the lint step is where a violation surfaces.

- [ ] **Step 9: Commit and open the PR**

```bash
git add lib/job-hound.ts lib/job-format.ts components/JobTable.tsx tests/
git commit -m "$(cat <<'EOF'
[jobs]: an idle chip on committed leads that stopped moving

Mirrors job-hound's staleness.py: reads state_log rather than
jobs.updated_at, and excludes gate and read rows so a lead cannot
quiet its own alarm.

Static amber, escalating to red past two weeks. Deliberately not
animated: a blinking row on a dashboard that stays open all day reads
as an alarm you train yourself to ignore, and it fights
prefers-reduced-motion.
EOF
)"
git push -u origin feat/staleness-chip
gh pr create --repo jrivers/lead-inbox --base main \
  --title "Idle chip for committed leads that stopped moving" \
  --body "The lead inbox UI half of the staleness signal. The job-hound half (CLI marker plus digest section) is a separate PR.

Adds \`idleDays\` to \`JobListItem\`, computed from a batched \`state_log\` query that mirrors \`jobdb.last_activity()\`: not \`jobs.updated_at\` (the nightly scorer bumps it without a state change), and excluding gate and read rows so a lead cannot quiet its own alarm.

Renders as a static chip on committed leads only: amber at 7 days, red past 14. Not animated, deliberately. A blinking row on a dashboard that stays open all day trains you to ignore it, and it fights \`prefers-reduced-motion\`.

\`STALE_AFTER_DAYS\` is duplicated from the Python by hand. The two repos share a database, not a codebase; if that threshold changes, both move.

Not verified: no browser rendering, per this repo's \`environment: 'node'\` vitest setup."
```

---

## Self-Review

**Spec coverage:**

| Spec requirement | Task |
|---|---|
| `staleness.py` pure module, three functions | 1 |
| `COMMITTED_STATES`, `STALE_AFTER_DAYS = 7` | 1 |
| Clock is `state_log`, not `updated_at` | 2 |
| Gate and read rows excluded | 2 |
| `jobdb.last_activity()` batched | 2 |
| CLI list marker | 3 |
| Digest Needs attention section, omitted when clean | 4, 5 |
| the lead inbox UI chip, amber/red, static | 6 |
| Error handling: silent on bad data | 1 (tests), 2 (absent uids) |
| Northgate regression test | 5 |
| No migration | all (nothing touches schema) |

**Gaps found and closed during review:**

1. **`refine_pipeline`'s double filter** was not in the spec and would have silently defeated the whole feature. Task 5 exists because of it and carries the regression test.
2. **`shown_uids` / `mark_digested`** interaction was unspecified. If stale leads were added to `shown_uids` they would be marked digested and demoted to the "Still open" recap after one appearance, making the nag fire exactly once. Task 4 excludes them, with a test pinning it.
3. **Future timestamps** (clock skew between the Mac and the host) were unhandled. Task 1 tests that they read as not-stale rather than negative idle.
4. **The `if not active:` early return** in `refine_pipeline` returns a dict that would have been missing the new `stale` key. Called out in Task 5 Step 3.

**Type consistency:** `idle_label` is the dict key in Python (Tasks 4, 5); `idleDays` is the number field in TypeScript (Task 6). Different names because they are different things: Python passes a rendered string, TypeScript passes a raw number and formats at the display layer. `staleness_label` (Python) and `idleLabel` (TypeScript) return `str | None` and `string | null` respectively, consistently nullable in both.

**Known duplication:** `STALE_AFTER_DAYS = 7` exists in both `staleness.py` and `lib/job-format.ts`. The repos share a database, not a codebase. Flagged in the code comment and the PR body rather than abstracted, because the alternative (a shared config file read by both a Python CLI and a Next.js build) costs more than it saves for one integer.

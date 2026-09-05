# Lead Voting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Up/down votes with optional notes on leads, given from the lead-inbox jobs tab, spooled to job-hound's DB, and fed into fit-ranking history.

**Architecture:** lead-inbox writes vote JSON files to `$JOB_INBOX_DIR/votes/` (it never writes jobs.db); job_ingest.py's 5-minute timer drains them into new `jobs` columns via `JobDB.set_vote`; `fit.build_history` includes votes as liked/disliked examples; the UI overlays pending spool votes so feedback is instant. Spec: `docs/superpowers/specs/2026-07-06-lead-voting-design.md`.

**Tech Stack:** Python 3 + sqlite3 + unittest/pytest (job-hound); Next.js 15 + TypeScript + better-sqlite3 + vitest (lead-inbox).

## Global Constraints

- Voice: no em dashes anywhere, in code comments or UI copy. Use commas or separate sentences.
- lead-inbox opens jobs.db read-only (`readonly: true` + `query_only = ON`); that must not change.
- Vote values are exactly `'up' | 'down' | null`; notes cap at 280 chars.
- Spool files are written atomically (temp + rename), filenames `<epoch-ms>-<slug>.json`.
- The drain never raises; bad files quarantine to `votes/failed/`, applied files move to `votes/processed/` (archive, never delete).
- job-hound work goes on branch `feature/lead-voting` (already exists, holds the spec). lead-inbox work goes on a new branch `feature/lead-voting` in `~/code/repos/lead-inbox`.
- Commit style: `[component]: description`.

---

### Task 1: jobdb vote columns + set_vote (job-hound)

**Files:**
- Modify: `jobdb.py` (SCHEMA ~line 115, ADDED_COLUMNS ~line 162, new method after `set_fields` ~line 311)
- Test: `tests/test_vote.py` (create)

**Interfaces:**
- Produces: `JobDB.set_vote(uid, vote, note=None) -> sqlite3.Row` where vote is `'up' | 'down' | None`; None clears vote, vote_note, voted_at. Columns `vote TEXT`, `vote_note TEXT`, `voted_at TEXT` on `jobs`. Every call appends a state_log row with unchanged state and note `vote: <up|down|cleared>` plus `. <note>` when a note is given.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_vote.py`:

```python
import tempfile
import unittest
from pathlib import Path

import jobdb


def make_db(tmpdir):
    db = jobdb.JobDB(Path(tmpdir) / "test.db")
    db.upsert_job({"id": "123", "title": "Platform Engineer", "location": "Remote",
                   "url": "https://example.com/j/123", "company": "acme",
                   "ats": "greenhouse"})
    row = db.conn.execute("SELECT * FROM jobs").fetchone()
    return db, row["uid"]


class SetVoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db, self.uid = make_db(self.tmp.name)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_set_vote_up_with_note(self):
        row = self.db.set_vote(self.uid, "up", note="love the domain")
        self.assertEqual(row["vote"], "up")
        self.assertEqual(row["vote_note"], "love the domain")
        self.assertTrue(row["voted_at"])

    def test_vote_overwrites_previous(self):
        self.db.set_vote(self.uid, "up")
        row = self.db.set_vote(self.uid, "down", note="too much K8s")
        self.assertEqual(row["vote"], "down")
        self.assertEqual(row["vote_note"], "too much K8s")

    def test_clear_vote(self):
        self.db.set_vote(self.uid, "up", note="x")
        row = self.db.set_vote(self.uid, None)
        self.assertIsNone(row["vote"])
        self.assertIsNone(row["vote_note"])
        self.assertIsNone(row["voted_at"])

    def test_vote_does_not_change_state(self):
        row = self.db.set_vote(self.uid, "down")
        self.assertEqual(row["state"], "discovered")

    def test_vote_writes_state_log_audit(self):
        self.db.set_vote(self.uid, "down", note="wrong stack")
        log = self.db.conn.execute(
            "SELECT * FROM state_log WHERE job_uid = ? ORDER BY id DESC",
            (self.uid,)).fetchone()
        self.assertEqual(log["from_state"], "discovered")
        self.assertEqual(log["to_state"], "discovered")
        self.assertIn("vote: down", log["note"])
        self.assertIn("wrong stack", log["note"])

    def test_invalid_vote_rejected(self):
        with self.assertRaises(ValueError):
            self.db.set_vote(self.uid, "sideways")

    def test_unknown_uid_rejected(self):
        with self.assertRaises(ValueError):
            self.db.set_vote("nope:nope:nope", "up")


class VoteMigrationTests(unittest.TestCase):
    def test_old_db_gains_vote_columns(self):
        # Simulate a DB created before the vote columns existed, then confirm
        # opening it with JobDB migrates the columns in without data loss.
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = sqlite3.connect(str(path))
            conn.execute(
                "CREATE TABLE jobs ("
                "uid TEXT PRIMARY KEY, slug TEXT UNIQUE NOT NULL,"
                "ext_id TEXT NOT NULL, ats TEXT NOT NULL, company TEXT NOT NULL,"
                "title TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'discovered',"
                "discovered_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
            conn.execute(
                "INSERT INTO jobs (uid, slug, ext_id, ats, company, title,"
                " state, discovered_at, updated_at) VALUES"
                " ('a:b:1', 'b__x__1', '1', 'a', 'b', 'X', 'discovered',"
                " '2026-01-01', '2026-01-01')")
            conn.commit()
            conn.close()
            db = jobdb.JobDB(path)
            cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(jobs)")}
            self.assertIn("vote", cols)
            self.assertIn("vote_note", cols)
            self.assertIn("voted_at", cols)
            self.assertEqual(db.conn.execute(
                "SELECT COUNT(*) AS n FROM jobs").fetchone()["n"], 1)
            db.close()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `source .venv/bin/activate && python -m pytest tests/test_vote.py -v`
Expected: FAIL with `AttributeError: 'JobDB' object has no attribute 'set_vote'` (and migration test fails on missing columns).

- [ ] **Step 3: Implement**

In `jobdb.py` SCHEMA, after the `close_reason` line (`close_reason  TEXT, ... captured on close`), add:

```sql
    vote          TEXT,                   -- up | down | NULL, operator lead feedback
    vote_note     TEXT,                   -- optional one-line reason for the vote
    voted_at      TEXT,                   -- timestamp of the last vote
```

In `ADDED_COLUMNS`, after `"close_reason": "TEXT",` add:

```python
    "vote": "TEXT",
    "vote_note": "TEXT",
    "voted_at": "TEXT",
```

After the `set_fields` method, add:

```python
    def set_vote(self, uid, vote, note=None):
        """Operator lead feedback, distinct from lifecycle state.

        vote is 'up', 'down', or None (None clears vote, note, and timestamp).
        Overwrites any previous vote. Appends a state_log audit row with the
        state unchanged so the timeline stays complete.
        """
        if vote not in ("up", "down", None):
            raise ValueError("vote must be 'up', 'down', or None")
        row = self.get(uid)
        if not row:
            raise ValueError(f"no job with uid {uid}")
        ts = now_iso()
        if vote is None:
            self.conn.execute(
                "UPDATE jobs SET vote = NULL, vote_note = NULL, voted_at = NULL, "
                "updated_at = ? WHERE uid = ?", (ts, uid))
        else:
            self.conn.execute(
                "UPDATE jobs SET vote = ?, vote_note = ?, voted_at = ?, "
                "updated_at = ? WHERE uid = ?", (vote, note, ts, ts, uid))
        label = "cleared" if vote is None else vote
        audit = f"vote: {label}" + (f". {note}" if note else "")
        self.conn.execute(
            "INSERT INTO state_log (job_uid, from_state, to_state, at, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (uid, row["state"], row["state"], ts, audit))
        self.conn.commit()
        return self.get(uid)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_vote.py -v` then the full suite `python -m pytest -q`
Expected: all PASS (185 existing + 9 new).

- [ ] **Step 5: Commit**

```bash
git add jobdb.py tests/test_vote.py
git commit -m "[jobdb]: vote columns and set_vote with state_log audit"
```

---

### Task 2: drain_votes in job_ingest (job-hound)

**Files:**
- Modify: `job_ingest.py` (new helpers after `remove_pending` ~line 187; rework the top of `main()` ~line 360)
- Test: `tests/test_drain_votes.py` (create)

**Interfaces:**
- Consumes: `JobDB.set_vote(uid, vote, note=None)` from Task 1; `JobDB.resolve(ident)` (existing, returns Row or None, raises TransitionError on ambiguity).
- Produces: `drain_votes(db, base) -> int` (count applied) where base is the inbox root; reads `<base>/votes/*.json`, moves to `<base>/votes/processed/` or `<base>/votes/failed/`. `main()` drains votes before the ANTHROPIC_API_KEY gate, so votes apply even without a key.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_drain_votes.py`:

```python
import json
import tempfile
import unittest
from pathlib import Path

import job_ingest
import jobdb


def vote_file(base, name, payload):
    root = Path(base) / "votes"
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(json.dumps(payload) if isinstance(payload, dict)
                             else payload)


class DrainVotesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self.tmp.name) / "job-inbox"
        self.db = jobdb.JobDB(Path(self.tmp.name) / "test.db")
        self.db.upsert_job({"id": "1", "title": "Platform Engineer",
                            "location": "Remote", "url": "https://x",
                            "company": "acme", "ats": "greenhouse"})
        row = self.db.conn.execute("SELECT * FROM jobs").fetchone()
        self.uid, self.slug = row["uid"], row["slug"]

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_empty_or_absent_spool_is_noop(self):
        self.assertEqual(job_ingest.drain_votes(self.db, self.inbox), 0)

    def test_happy_path_applies_and_archives(self):
        vote_file(self.inbox, f"1000-{self.slug}.json",
                  {"slug": self.slug, "vote": "up", "note": "great fit",
                   "voted_at": "2026-07-06T12:00:00Z"})
        applied = job_ingest.drain_votes(self.db, self.inbox)
        self.assertEqual(applied, 1)
        row = self.db.get(self.uid)
        self.assertEqual(row["vote"], "up")
        self.assertEqual(row["vote_note"], "great fit")
        processed = list((self.inbox / "votes" / "processed").glob("*.json"))
        self.assertEqual(len(processed), 1)
        self.assertEqual(list((self.inbox / "votes").glob("*.json")), [])

    def test_last_write_wins_by_filename_order(self):
        vote_file(self.inbox, f"1000-{self.slug}.json",
                  {"slug": self.slug, "vote": "up", "voted_at": "t1"})
        vote_file(self.inbox, f"2000-{self.slug}.json",
                  {"slug": self.slug, "vote": "down", "note": "changed my mind",
                   "voted_at": "t2"})
        self.assertEqual(job_ingest.drain_votes(self.db, self.inbox), 2)
        self.assertEqual(self.db.get(self.uid)["vote"], "down")

    def test_malformed_json_quarantined(self):
        vote_file(self.inbox, "1000-bad.json", "{not json")
        self.assertEqual(job_ingest.drain_votes(self.db, self.inbox), 0)
        self.assertEqual(len(list((self.inbox / "votes" / "failed").glob("*.json"))), 1)

    def test_unknown_slug_quarantined(self):
        vote_file(self.inbox, "1000-ghost.json",
                  {"slug": "ghost__job__zzzz", "vote": "up", "voted_at": "t"})
        self.assertEqual(job_ingest.drain_votes(self.db, self.inbox), 0)
        self.assertEqual(len(list((self.inbox / "votes" / "failed").glob("*.json"))), 1)

    def test_invalid_vote_value_quarantined(self):
        vote_file(self.inbox, f"1000-{self.slug}.json",
                  {"slug": self.slug, "vote": "sideways", "voted_at": "t"})
        self.assertEqual(job_ingest.drain_votes(self.db, self.inbox), 0)
        self.assertIsNone(self.db.get(self.uid)["vote"])
        self.assertEqual(len(list((self.inbox / "votes" / "failed").glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_drain_votes.py -v`
Expected: FAIL with `AttributeError: module 'job_ingest' has no attribute 'drain_votes'`.

- [ ] **Step 3: Implement**

In `job_ingest.py`, after `remove_pending` (~line 187), add:

```python
def _votes_dirs(base):
    root = Path(base).expanduser() / "votes"
    processed = root / "processed"
    failed = root / "failed"
    for d in (root, processed, failed):
        d.mkdir(parents=True, exist_ok=True)
    return root, processed, failed


def drain_votes(db, base):
    """Apply pending vote spool files (written by lead-inbox) to the DB.

    Files are processed in name order (epoch-ms prefixed), so the newest vote
    for a slug lands last and wins. Bad files quarantine to failed/ and never
    crash the drain. Returns the number of votes applied.
    """
    root, processed, failed = _votes_dirs(base)
    applied = 0
    for f in sorted(root.glob("*.json")):
        try:
            entry = json.loads(f.read_text())
            slug = entry["slug"]
            vote = entry["vote"]
            note = entry.get("note") or None
        except (json.JSONDecodeError, OSError, KeyError, TypeError):
            f.replace(failed / f.name)
            print(f"job_ingest: malformed vote file {f.name} -> failed/",
                  file=sys.stderr)
            continue
        try:
            row = db.resolve(slug)
        except Exception:
            row = None
        if not row:
            f.replace(failed / f.name)
            print(f"job_ingest: vote for unknown job '{slug}' -> failed/",
                  file=sys.stderr)
            continue
        try:
            db.set_vote(row["uid"], vote, note=note)
        except ValueError as e:
            f.replace(failed / f.name)
            print(f"job_ingest: vote rejected for {slug}: {e} -> failed/",
                  file=sys.stderr)
            continue
        f.replace(processed / f.name)
        applied += 1
    return applied
```

In `main()`, move DB setup above the API-key gate and drain votes there. Replace:

```python
def main():
    inbox = os.environ.get("JOB_INBOX_DIR") or str(
        Path.home() / ".lead-inbox" / "data" / "job-inbox")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("job_ingest: ANTHROPIC_API_KEY not set; aborting", file=sys.stderr)
        return 1
    db_path = os.environ.get("JOB_DB") or str(Path.cwd() / "jobs.db")
```

with:

```python
def main():
    inbox = os.environ.get("JOB_INBOX_DIR") or str(
        Path.home() / ".lead-inbox" / "data" / "job-inbox")
    db_path = os.environ.get("JOB_DB") or str(Path.cwd() / "jobs.db")
    db = JobDB(db_path)
    # Votes need no API access; drain them even when the key is missing.
    drain_votes(db, inbox)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("job_ingest: ANTHROPIC_API_KEY not set; aborting", file=sys.stderr)
        return 1
```

and delete the now-duplicated `db = JobDB(db_path)` line further down in `main()` (keep everything else in place).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_drain_votes.py test_job_ingest.py -q` then full suite `python -m pytest -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add job_ingest.py tests/test_drain_votes.py
git commit -m "[ingest]: drain vote spool into jobs.db before the API gate"
```

---

### Task 3: votes in fit history (job-hound)

**Files:**
- Modify: `fit.py:96-122` (`build_history`)
- Test: `tests/test_history_votes.py` (create)

**Interfaces:**
- Consumes: `vote`, `vote_note` columns from Task 1.
- Produces: `build_history` entries may now carry `decision` values `"liked"` and `"disliked"` (in addition to `"pursued"`/`"rejected"`), sourced from voted-but-untriaged discovered jobs. `_history_block` needs no change (it upper-cases whatever decision it gets).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_history_votes.py`:

```python
import tempfile
import unittest
from pathlib import Path

import fit
import jobdb


class HistoryVoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = jobdb.JobDB(Path(self.tmp.name) / "t.db")
        for i, company in enumerate(["acme", "globex", "initech"]):
            self.db.upsert_job({"id": str(i), "title": f"Platform Engineer {i}",
                                "location": "Remote", "url": "https://x",
                                "company": company, "ats": "greenhouse"})

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def uid_for(self, company):
        return self.db.conn.execute(
            "SELECT uid FROM jobs WHERE company = ?", (company,)).fetchone()["uid"]

    def test_upvote_appears_as_liked_with_reason(self):
        self.db.set_vote(self.uid_for("acme"), "up", note="love the domain")
        hist = fit.build_history(self.db)
        entry = next(h for h in hist if h["company"] == "acme")
        self.assertEqual(entry["decision"], "liked")
        self.assertEqual(entry["reason"], "love the domain")

    def test_downvote_appears_as_disliked(self):
        self.db.set_vote(self.uid_for("globex"), "down", note="too much K8s")
        hist = fit.build_history(self.db)
        entry = next(h for h in hist if h["company"] == "globex")
        self.assertEqual(entry["decision"], "disliked")
        self.assertEqual(entry["reason"], "too much K8s")

    def test_unvoted_discovered_jobs_stay_excluded(self):
        self.db.set_vote(self.uid_for("acme"), "up")
        hist = fit.build_history(self.db)
        self.assertNotIn("initech", [h["company"] for h in hist])

    def test_lifecycle_decision_beats_vote(self):
        # A skipped job with a stray vote still reads as rejected: lifecycle
        # decisions are the stronger signal.
        uid = self.uid_for("acme")
        self.db.set_vote(uid, "up")
        self.db.set_state(uid, "skipped")
        hist = fit.build_history(self.db)
        entry = next(h for h in hist if h["company"] == "acme")
        self.assertEqual(entry["decision"], "rejected")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_history_votes.py -v`
Expected: the two vote tests FAIL (voted discovered jobs are excluded today).

- [ ] **Step 3: Implement**

In `fit.py` `build_history`, replace the query:

```python
    rows = db.conn.execute(
        "SELECT * FROM jobs "
        "WHERE state IN ('queued','drafted','ready','applied','interviewing',"
        "'skipped','closed') "
        "ORDER BY updated_at DESC"
    ).fetchall()
```

with:

```python
    rows = db.conn.execute(
        "SELECT * FROM jobs "
        "WHERE state IN ('queued','drafted','ready','applied','interviewing',"
        "'skipped','closed') "
        "   OR (state = 'discovered' AND vote IS NOT NULL) "
        "ORDER BY updated_at DESC"
    ).fetchall()
```

In the loop, extend the decision ladder with a discovered-vote branch. After the `elif state == "closed":` block's `reason = ...` line and before the `else: continue`, add:

```python
        elif state == "discovered":
            # Voted but untriaged: a softer signal than a lifecycle decision.
            decision = "liked" if r["vote"] == "up" else "disliked"
            reason = r["vote_note"] or ""
```

(The docstring's "Untriaged 'discovered' jobs carry no decision" sentence should gain: "unless the operator voted on them, which counts as liked/disliked.")

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_history_votes.py test_fit_history.py -v` then `python -m pytest -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add fit.py tests/test_history_votes.py
git commit -m "[fit]: voted leads join the LLM decision history as liked/disliked"
```

---

### Task 4: vote spool library (lead-inbox)

**Files:**
- Create: `lib/job-votes.ts`
- Test: `tests/job-votes.test.ts`
- Repo: `~/code/repos/lead-inbox`, new branch `feature/lead-voting` off `main`

**Interfaces:**
- Consumes: `config.jobInboxDir` from `lib/config.ts` (exists, env `JOB_INBOX_DIR`, default `~/.lead-inbox/data/job-inbox`).
- Produces: `submitVote(slug: string, vote: VoteValue, note?: string, baseDir?: string): JobVote` and `readPendingVotes(baseDir?: string): Map<string, JobVote>`; `type VoteValue = 'up' | 'down' | null`; `type JobVote = { slug: string; vote: VoteValue; note: string; voted_at: string }`.

- [ ] **Step 1: Write the failing test**

Check an existing test's import style first (`head -5 tests/backup-status.test.ts`) and match it. Create `tests/job-votes.test.ts`:

```typescript
import { describe, expect, it } from 'vitest'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { readPendingVotes, submitVote } from '../lib/job-votes'

function tmpBase(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'job-votes-'))
}

describe('job votes spool', () => {
  it('writes an atomic spool file readPendingVotes can see', () => {
    const base = tmpBase()
    const record = submitVote('acme__platform__ab12', 'up', 'love it', base)
    expect(record.vote).toBe('up')
    const pending = readPendingVotes(base)
    expect(pending.get('acme__platform__ab12')?.vote).toBe('up')
    expect(pending.get('acme__platform__ab12')?.note).toBe('love it')
    const files = fs.readdirSync(path.join(base, 'votes'))
    expect(files).toHaveLength(1)
    expect(files[0]).toMatch(/^\d+-acme__platform__ab12\.json$/)
  })

  it('newest vote per slug wins', async () => {
    const base = tmpBase()
    submitVote('a__b__c1', 'up', '', base)
    await new Promise((resolve) => setTimeout(resolve, 5))
    submitVote('a__b__c1', 'down', 'changed my mind', base)
    expect(readPendingVotes(base).get('a__b__c1')?.vote).toBe('down')
  })

  it('missing spool dir reads as empty', () => {
    expect(readPendingVotes(path.join(tmpBase(), 'nope')).size).toBe(0)
  })

  it('skips malformed files without throwing', () => {
    const base = tmpBase()
    const dir = path.join(base, 'votes')
    fs.mkdirSync(dir, { recursive: true })
    fs.writeFileSync(path.join(dir, '1-bad.json'), '{not json')
    submitVote('good__slug__x1', 'down', '', base)
    expect(readPendingVotes(base).size).toBe(1)
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/code/repos/lead-inbox && npm install && npx vitest run tests/job-votes.test.ts`
Expected: FAIL, cannot resolve `../lib/job-votes`.

- [ ] **Step 3: Implement**

Create `lib/job-votes.ts`:

```typescript
import fs from 'node:fs'
import path from 'node:path'
import { config } from './config'

export type VoteValue = 'up' | 'down' | null

export type JobVote = {
  slug: string
  vote: VoteValue
  note: string
  voted_at: string
}

function votesDir(baseDir: string): string {
  return path.join(baseDir, 'votes')
}

/**
 * Spool a lead vote for job_ingest.py to drain into jobs.db (5-minute timer).
 * Written atomically (temp + rename) so the Python reader never observes a
 * partially written file. Filename is <epoch-ms>-<slug>.json so lexical order
 * is chronological and the drain's last-write-wins holds.
 */
export function submitVote(
  slug: string,
  vote: VoteValue,
  note = '',
  baseDir = config.jobInboxDir
): JobVote {
  const dir = votesDir(baseDir)
  fs.mkdirSync(dir, { recursive: true })
  const record: JobVote = { slug, vote, note, voted_at: new Date().toISOString() }
  const name = `${Date.now()}-${slug}.json`
  const tmp = path.join(dir, `.${name}.tmp`)
  fs.writeFileSync(tmp, JSON.stringify(record, null, 2))
  fs.renameSync(tmp, path.join(dir, name))
  return record
}

/**
 * Pending (not yet drained) votes, newest per slug. The jobs dashboard overlays
 * these on the read-only DB so a vote is visible before the drain lands it.
 */
export function readPendingVotes(baseDir = config.jobInboxDir): Map<string, JobVote> {
  const dir = votesDir(baseDir)
  const result = new Map<string, JobVote>()
  let names: string[]
  try {
    names = fs.readdirSync(dir).filter((n) => n.endsWith('.json')).sort()
  } catch {
    return result
  }
  for (const name of names) {
    try {
      const parsed = JSON.parse(fs.readFileSync(path.join(dir, name), 'utf-8')) as JobVote
      if (parsed && typeof parsed.slug === 'string') result.set(parsed.slug, parsed)
    } catch {
      // Partially written or malformed: skip it, the drain quarantines it.
    }
  }
  return result
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `npx vitest run tests/job-votes.test.ts` then the full suite `npm test` and `npx eslint lib/job-votes.ts tests/job-votes.test.ts`
Expected: all PASS, no lint errors.

- [ ] **Step 5: Commit (on new branch)**

```bash
cd ~/code/repos/lead-inbox
git checkout -b feature/lead-voting main
git add lib/job-votes.ts tests/job-votes.test.ts
git commit -m "[jobs]: vote spool library (atomic write + pending read)"
```

---

### Task 5: vote API route (lead-inbox)

**Files:**
- Create: `app/api/jobs/[slug]/vote/route.ts`

**Interfaces:**
- Consumes: `submitVote`, `VoteValue` from Task 4.
- Produces: `POST /api/jobs/<slug>/vote` accepting `{ vote: 'up' | 'down' | null, note?: string }`; 201 `{ vote: JobVote }`, 400 on bad input, 500 on spool failure. The UI (Task 7) calls this.

- [ ] **Step 1: Implement (route handlers here are thin; validation is covered by the Task 4 unit tests plus a curl smoke test below)**

Create `app/api/jobs/[slug]/vote/route.ts`:

```typescript
import { NextRequest, NextResponse } from 'next/server'
import { submitVote, VoteValue } from '@/lib/job-votes'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

type RouteContext = {
  params: Promise<{ slug: string }>
}

export async function POST(req: NextRequest, context: RouteContext) {
  const { slug } = await context.params
  let body: unknown
  try {
    body = await req.json()
  } catch {
    return NextResponse.json({ error: 'Invalid JSON body' }, { status: 400 })
  }
  const rec = body && typeof body === 'object'
    ? (body as { vote?: unknown; note?: unknown })
    : {}
  const vote = rec.vote
  if (vote !== 'up' && vote !== 'down' && vote !== null) {
    return NextResponse.json(
      { error: "vote must be 'up', 'down', or null" },
      { status: 400 }
    )
  }
  const note = typeof rec.note === 'string' ? rec.note.slice(0, 280) : ''
  try {
    const submission = submitVote(decodeURIComponent(slug), vote as VoteValue, note)
    return NextResponse.json({ vote: submission }, { status: 201 })
  } catch {
    return NextResponse.json({ error: 'Failed to spool vote' }, { status: 500 })
  }
}
```

- [ ] **Step 2: Typecheck, lint, and smoke test**

Run: `npx tsc --noEmit && npx eslint app/api/jobs`
Then: `JOB_INBOX_DIR=/tmp/vote-smoke npm run dev` (background) and:

```bash
curl -s -X POST localhost:3000/api/jobs/test__slug__x1/vote \
  -H 'Content-Type: application/json' -d '{"vote":"up","note":"smoke"}'
# expect: {"vote":{"slug":"test__slug__x1","vote":"up","note":"smoke","voted_at":"..."}} and a file in /tmp/vote-smoke/votes/
curl -s -X POST localhost:3000/api/jobs/test__slug__x1/vote \
  -H 'Content-Type: application/json' -d '{"vote":"sideways"}'
# expect: {"error":"vote must be 'up', 'down', or null"} with HTTP 400
```

Stop the dev server after.

- [ ] **Step 3: Commit**

```bash
git add "app/api/jobs/[slug]/vote/route.ts"
git commit -m "[jobs]: POST /api/jobs/[slug]/vote spools operator votes"
```

---

### Task 6: read overlay + vote-aware sort (lead-inbox)

**Files:**
- Modify: `lib/job-hound.ts` (types ~lines 36-132, `mapListItem` ~line 233, `sortJobs` ~line 276, `getJobsDashboard` ~line 311, `getJobDetail` ~line 362)

**Interfaces:**
- Consumes: `readPendingVotes`, `JobVote`, `VoteValue` from Task 4; `vote`, `vote_note`, `voted_at` DB columns from Task 1 (may be absent on an unmigrated DB, so row fields are optional).
- Produces: `JobListItem` gains `vote: VoteValue` and `votedAt: string | null`; `JobDetail` gains `voteNote: string | null`. Dashboard and detail reads overlay pending spool votes (spool always wins, it is strictly newer than the DB). Default sort ranks up-voted first and down-voted last. Task 7's UI relies on these exact field names.

- [ ] **Step 1: Implement**

In `lib/job-hound.ts`:

1. Add to imports: `import { readPendingVotes, VoteValue } from './job-votes'`
2. `JobListItem` type: after `nextAction: string`, add:

```typescript
  vote: VoteValue
  votedAt: string | null
```

3. `JobDetail` type: after `notes: string | null`, add:

```typescript
  voteNote: string | null
```

4. `JobRow` type: after `location_type: string | null`, add (optional, older DBs lack them):

```typescript
  vote?: string | null
  vote_note?: string | null
  voted_at?: string | null
```

5. In `mapListItem`, after `nextAction: nextAction(row),` add:

```typescript
    vote: row.vote === 'up' || row.vote === 'down' ? row.vote : null,
    votedAt: asString(row.voted_at ?? null),
```

6. Add above `sortJobs`:

```typescript
function voteRank(vote: VoteValue): number {
  return vote === 'up' ? 1 : vote === 'down' ? -1 : 0
}
```

and change `sortJobs` to:

```typescript
function sortJobs(a: JobListItem, b: JobListItem): number {
  if (voteRank(b.vote) !== voteRank(a.vote)) return voteRank(b.vote) - voteRank(a.vote)
  if (b.score !== a.score) return b.score - a.score
  return (new Date(b.updatedAt || 0).getTime()) - (new Date(a.updatedAt || 0).getTime())
}
```

7. In `getJobsDashboard`, replace the `allJobs` line with:

```typescript
    const pendingVotes = readPendingVotes()
    const allJobs = rows
      .map((row) => mapListItem(row, fileKinds.get(row.uid) || []))
      .map((job) => {
        const pending = pendingVotes.get(job.slug)
        return pending ? { ...job, vote: pending.vote, votedAt: pending.voted_at } : job
      })
      .sort(sortJobs)
```

8. In `getJobDetail`, in the returned object add `voteNote: asString(row.vote_note ?? null),` after `notes: row.notes,`, and just before `return`, overlay:

```typescript
    const pending = readPendingVotes().get(slug)
```

then in the returned object spread the overlay at the end:

```typescript
      ...(pending ? { vote: pending.vote, votedAt: pending.voted_at, voteNote: pending.note || null } : {}),
```

- [ ] **Step 2: Typecheck, lint, run suite**

Run: `npx tsc --noEmit && npx eslint lib/job-hound.ts && npm test`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add lib/job-hound.ts
git commit -m "[jobs]: overlay pending votes on reads, vote-aware default sort"
```

---

### Task 7: vote UI in jobs tab and detail page (lead-inbox)

**Files:**
- Modify: `app/jobs/page.tsx` (type ~line 11, sort ~line 124, `JobRow` ~line 319)
- Modify: `app/jobs/[slug]/page.tsx` (type ~line 23, add a "Your take" panel in the left column ~line 145)

**Interfaces:**
- Consumes: `vote`/`votedAt`/`voteNote` fields from Task 6; `POST /api/jobs/[slug]/vote` from Task 5.
- Produces: user-facing thumbs on every pipeline row and a vote + note panel on the detail page. Optimistic update, rollback on POST failure.

- [ ] **Step 1: Implement list page**

In `app/jobs/page.tsx`:

1. Add to the local `JobListItem` type: `vote: 'up' | 'down' | null` and `votedAt: string | null`.
2. Add helpers near `dateMs`:

```typescript
function voteRank(vote: 'up' | 'down' | null): number {
  return vote === 'up' ? 1 : vote === 'down' ? -1 : 0
}
```

3. In `JobsPage`, add after the `useEffect`:

```typescript
  const applyVote = (slug: string, vote: 'up' | 'down' | null) => {
    setData((prev) => prev
      ? { ...prev, jobs: prev.jobs.map((job) => (job.slug === slug ? { ...job, vote } : job)) }
      : prev)
  }
```

4. In the `displayedJobs` sort, change the final (score) branch to:

```typescript
        return voteRank(b.vote) - voteRank(a.vote) || b.score - a.score || dateMs(b.updatedAt) - dateMs(a.updatedAt)
```

5. Render rows with the callback: `<JobRow key={job.slug} job={job} onVote={applyVote} />`
6. Change `JobRow` to accept and render vote buttons:

```typescript
function JobRow({ job, onVote }: { job: JobListItem; onVote: (slug: string, vote: 'up' | 'down' | null) => void }) {
```

and in the badges div, before `<Badge tone="state">`:

```tsx
          <VoteButtons job={job} onVote={onVote} />
```

7. Add the component:

```tsx
function VoteButtons({ job, onVote }: { job: JobListItem; onVote: (slug: string, vote: 'up' | 'down' | null) => void }) {
  const cast = (value: 'up' | 'down') => {
    const next = job.vote === value ? null : value
    onVote(job.slug, next)
    fetch(`/api/jobs/${encodeURIComponent(job.slug)}/vote`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ vote: next }),
    }).then((res) => {
      if (!res.ok) onVote(job.slug, job.vote)
    }).catch(() => onVote(job.slug, job.vote))
  }
  const base = 'text-xs px-1.5 py-0.5 rounded border font-mono transition-colors'
  return (
    <span className="flex items-center gap-1">
      <button
        onClick={() => cast('up')}
        title={job.vote === 'up' ? 'Clear up-vote' : 'Vote up'}
        className={`${base} ${job.vote === 'up'
          ? 'bg-[#50fa7b]/20 text-[#50fa7b] border-[#50fa7b]/40'
          : 'text-[#6272a4] border-[#44475a]/30 hover:text-[#50fa7b] hover:border-[#50fa7b]/30'}`}
      >
        ▲
      </button>
      <button
        onClick={() => cast('down')}
        title={job.vote === 'down' ? 'Clear down-vote' : 'Vote down'}
        className={`${base} ${job.vote === 'down'
          ? 'bg-[#ff5555]/20 text-[#ff5555] border-[#ff5555]/40'
          : 'text-[#6272a4] border-[#44475a]/30 hover:text-[#ff5555] hover:border-[#ff5555]/30'}`}
      >
        ▼
      </button>
    </span>
  )
}
```

- [ ] **Step 2: Implement detail page**

In `app/jobs/[slug]/page.tsx`:

1. Add to the local `JobDetail` type: `vote: 'up' | 'down' | null`, `voteNote: string | null`, `votedAt: string | null`.
2. In the left column, directly above `<Panel title="Fit and rationale">`, add `<VotePanel job={job} onChange={(patch) => setJob((prev) => (prev ? { ...prev, ...patch } : prev))} />`.
3. Add the component:

```tsx
function VotePanel({ job, onChange }: { job: JobDetail; onChange: (patch: Partial<JobDetail>) => void }) {
  const [note, setNote] = useState(job.voteNote || '')

  const send = (vote: 'up' | 'down' | null, noteValue: string) => {
    onChange({ vote, voteNote: noteValue || null })
    fetch(`/api/jobs/${encodeURIComponent(job.slug)}/vote`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ vote, note: noteValue }),
    }).catch(() => {})
  }

  const cast = (value: 'up' | 'down') => send(job.vote === value ? null : value, note)
  const active = 'bg-[#50fa7b]/20 text-[#50fa7b] border-[#50fa7b]/40'
  const activeDown = 'bg-[#ff5555]/20 text-[#ff5555] border-[#ff5555]/40'
  const idle = 'text-[#6272a4] border-[#44475a]/30'

  return (
    <Panel title="Your take">
      <div className="flex items-center gap-2">
        <button onClick={() => cast('up')} className={`text-sm px-3 py-1 rounded border font-mono ${job.vote === 'up' ? active : idle}`}>
          ▲ good lead
        </button>
        <button onClick={() => cast('down')} className={`text-sm px-3 py-1 rounded border font-mono ${job.vote === 'down' ? activeDown : idle}`}>
          ▼ not for me
        </button>
      </div>
      {job.vote && (
        <div className="space-y-2">
          <textarea
            value={note}
            onChange={(event) => setNote(event.target.value.slice(0, 280))}
            placeholder="Why? One line teaches the ranker (optional)"
            rows={2}
            className="w-full rounded border border-[#50fa7b]/10 bg-[#0d0e14] px-3 py-2 text-xs text-[#f8f8f2] outline-none font-mono focus:border-[#50fa7b]/40"
          />
          <button
            onClick={() => send(job.vote, note)}
            className="text-[10px] px-2 py-1 rounded border font-mono text-[#8be9fd] border-[#8be9fd]/30 hover:border-[#8be9fd]/60"
          >
            Save note
          </button>
        </div>
      )}
      <p className="text-[10px] text-[#44475a] font-mono">
        Votes feed the fit ranking. They do not change pipeline state.
      </p>
    </Panel>
  )
}
```

- [ ] **Step 3: Typecheck, lint, build, live smoke**

Run: `npx tsc --noEmit && npx eslint app/jobs && npm run build`
Then `JOB_INBOX_DIR=/tmp/vote-smoke JOB_HOUND_DB_PATH=<a copy of a real jobs.db> npm run dev`, open `localhost:3000/jobs`, click a thumb, verify: button highlights, row re-sorts, a JSON file appears under `/tmp/vote-smoke/votes/`, reload keeps the vote (overlay), and the detail page shows the "Your take" panel with a working note save.

- [ ] **Step 4: Commit**

```bash
git add app/jobs/page.tsx "app/jobs/[slug]/page.tsx"
git commit -m "[jobs]: vote thumbs on pipeline rows and Your take panel on detail"
```

---

### Task 8: PRs, merge, deploy verification

**Files:** none (process)

- [ ] **Step 1: job-hound PR**

```bash
cd ~/code/job-hound
git push -u origin feature/lead-voting
gh pr create --base main --title "[voting]: lead votes from lead-inbox, spool drain, fit history" \
  --body "Implements docs/superpowers/specs/2026-07-06-lead-voting-design.md (job-hound half): vote columns + set_vote, drain_votes in job_ingest before the API gate, liked/disliked entries in fit history. lead-inbox half follows in jrivers/lead-inbox."
```

Wait for the `tests` check, then merge (the operator merges if branch protection blocks the CLI again).

- [ ] **Step 2: lead-inbox PR**

```bash
cd ~/code/repos/lead-inbox
git push -u origin feature/lead-voting
gh pr create --base main --title "[jobs]: lead voting (spool, API, overlay, UI)" \
  --body "lead-inbox half of job-hound's 2026-07-06 lead-voting spec: vote spool library, POST /api/jobs/[slug]/vote, pending-vote overlay on reads, vote-aware sort, thumbs UI + detail note panel. jobs.db stays read-only."
```

Check this repo's checks and merge flow (it has its own actions runner).

- [ ] **Step 3: Deploy verification on tools**

After both merges:

```bash
ssh $JOB_HOST 'cd ~/job-hound && git pull --ff-only origin main && git log --oneline -1'
# lead-inbox deploys via its actions runner; verify:
ssh $JOB_HOST 'cd ~/.lead-inbox/app && git log --oneline -1'
```

Then end-to-end: vote a lead in the live jobs tab, confirm the spool file lands in `~/.lead-inbox/data/job-inbox/votes/`, wait for the next 5-minute ingest tick, and confirm `vote` is set in the DB:

```bash
ssh $JOB_HOST 'sqlite3 ~/job-hound/jobs.db "SELECT slug, vote, vote_note FROM jobs WHERE vote IS NOT NULL"'
```

Also pull on the chat agent host so its checkout stays current: `ssh agent-host 'cd ~/job-hound && git pull --ff-only origin main'`.

- [ ] **Step 4: Delete local branches after merge, fetch --prune both repos**

#!/usr/bin/env python3
"""
jobdb.py - SQLite system of record for the job pipeline.

Owns the lifecycle of every discovered job. The scan layer (job_monitor.py)
feeds jobs in; later stages (generate, package, track) read and update them.

State machine:

    discovered -> queued -> drafted -> ready -> applied -> interviewing -> closed
                    |                                          |
                    +------------------ skipped ---------------+

    closed -> applied | interviewing        only when outcome is 'ghosted'

  discovered : found by a scan, not yet triaged
  queued     : you want to pursue it (the work queue for generation)
  drafted    : resume + cover letter generated, not yet reviewed
  ready      : you've reviewed the package; ready to submit by hand
  applied    : you submitted it (date stamped)
  interviewing few: in process
  closed     : terminal for a DECIDED outcome (rejected, withdrawn, offer,
               accepted). A `ghosted` close reopens to applied/interviewing,
               because nobody decided anything: see set_state.
  skipped    : you decided not to pursue (terminal)

Transitions are validated: you can't jump from discovered straight to applied
without passing through the queue, which keeps the data honest and the
reports meaningful.

This module is import-safe and has no network or file-generation concerns.
Pure data layer. The CLI lives in job_cli.py.
"""

import os
import sqlite3
import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path


# --- State model ----------------------------------------------------------

STATES = [
    "discovered", "queued", "drafted", "ready",
    "applied", "interviewing", "closed", "skipped",
]

# Allowed forward (and a few backward) transitions. Key -> set of legal next.
TRANSITIONS = {
    "discovered":  {"queued", "skipped"},
    "queued":      {"drafted", "skipped", "discovered"},
    "drafted":     {"ready", "queued", "skipped"},
    "ready":       {"applied", "drafted", "skipped"},
    "applied":     {"interviewing", "closed"},
    "interviewing": {"closed", "applied"},
    # Terminal for every outcome that was actually DECIDED. `ghosted` is the
    # one exception: it means "they stopped replying", not "they said no", and
    # a recruiter who goes quiet and then books a round has decided nothing.
    # set_state enforces the outcome check; the pair here only says which
    # states a reopen may land in.
    "closed":      {"applied", "interviewing"},
    "skipped":     {"queued"},  # allow un-skipping back into the queue
}

# Outcomes recorded when a job reaches 'closed'.
OUTCOMES = ["rejected", "withdrawn", "offer", "accepted", "ghosted", "other"]

# The column a structured disposition reason belongs in, per state. Only these
# two states have one; every other state records its free text as a state_log
# note instead. set_state writes the column in the same transaction as the
# transition, so the two can never disagree.
REASON_COLUMN = {"closed": "close_reason", "skipped": "skip_reason"}


def next_states(row):
    """The legal next states for one job row, honouring the reopen guard.

    TRANSITIONS alone is not the whole answer any more. A `closed` row lists
    the reopen destinations, but set_state only permits them when the outcome
    is `ghosted`, so reading the table directly over-reports for a job closed
    as rejected and would have the inbox draw a button that 409s. Callers that
    need "what can this row do next" ask here; TRANSITIONS stays the single
    definition of the edges themselves.
    """
    # _col is defined further down the module; this body only runs after
    # import, so the name resolves fine.
    nxt = set(TRANSITIONS.get(row["state"], set()))
    if row["state"] == "closed" and _col(row, "outcome") != "ghosted":
        nxt = set()
    return sorted(nxt)


class TransitionError(ValueError):
    pass


class DBPathError(RuntimeError):
    """No database path could be resolved without inventing one."""


def resolve_db_path(override=None):
    """Locate the jobs database, or refuse.

    There is exactly ONE jobs.db and it lives on the deployment host. Every
    entry point resolves it the same way, and none of them may invent one: a
    path that gets created on first open is how a second, divergent database
    appeared on a workstation in August 2026 and swallowed nine days of decisions.
    So the last step is an error, not a default. See
    docs/single-source-of-truth.md.
    """
    if override:
        return Path(override).expanduser()
    env = os.environ.get("JOB_DB")
    if env:
        return Path(env).expanduser()
    local = Path.cwd() / "jobs.db"
    if local.exists():
        return local
    raise DBPathError(
        "No jobs.db. JOB_DB is unset and there is no jobs.db in "
        f"{Path.cwd()}.\n"
        "The one real database lives on the deployment host; drive it with "
        "bin/jh (for example: bin/jh stats).\n"
        "To work against a throwaway copy instead, set JOB_DB or pass --db."
    )


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Hosts that serve the same posting under two names. Kept to pairs actually
# observed in this pipeline, never guessed: Greenhouse migrated job boards from
# boards.greenhouse.io to job-boards.greenhouse.io and both spellings are still
# in circulation, so the scanner and the open-jobs corpus routinely disagree
# about the host for a posting they both saw.
HOST_ALIASES = {"job-boards.greenhouse.io": "boards.greenhouse.io"}

# Query parameters known to carry no identity. Everything NOT listed here is
# kept, because on Taleo, BrassRing and some SuccessFactors boards the
# requisition id lives in the query string and dropping it would merge every
# posting on the board into one.
# Query parameters known to carry no identity. Deliberately TINY, and grown
# only on evidence.
#
# An earlier version listed eight more from memory (hub, trk, referrer, fbclid,
# gclid, mc_cid, mc_eid, trackid). None appears anywhere in this repo or in the
# corpus: a sweep of 2,165 live postings found exactly ONE query parameter in
# circulation, `gh_jid`, which must be kept. Every guessed entry was a standing
# chance of the single error canonical_url exists to prevent, inside a function
# whose stated rule is that an unrecognised parameter might be the identifier.
# A list that has to model the whole ATS universe cannot coexist with that
# rule, so it does not try.
#
# `gh_jid` is deliberately ABSENT: on a Greenhouse board embedded in a
# company's own site the posting is addressed as `?gh_jid=<id>` and nothing
# else identifies it, so stripping it merges that entire careers page into one
# row. `gh_src` beside it IS a tracker. One letter apart, opposite meanings.
TRACKING_PARAMS = {"gh_src"}


def canonical_url(url):
    """A posting URL reduced to what identifies the posting.

    Two crawlers routinely spell the same posting differently: Greenhouse moved
    boards from boards.greenhouse.io to job-boards.greenhouse.io and both are
    still in circulation, ATS links pick up `?gh_src=` tracking parameters, and
    hosts vary on `www.` and scheme. An exact string comparison misses every
    one of those, which defeats the layer that exists precisely for the cases
    where the uid already differs.

    Deliberately conservative, because the two errors are not symmetrical. A
    missed merge costs one duplicate row a human can see and ignore. A wrong
    merge silently discards a real job that no later stage would ever surface
    again. So every ambiguous case resolves toward keeping the rows apart.

    Two consequences of that rule:

    Path case is PRESERVED. ATS ids are case-sensitive, and folding them would
    turn a dedup miss into a dropped lead.

    Query parameters are dropped by ALLOWLIST, never wholesale. Taleo,
    BrassRing and some SuccessFactors boards put the requisition id in the
    query rather than the path, so discarding the whole query collapsed every
    posting on such a board onto one string: the first ingested and every later
    one was dropped as a duplicate, permanently, since known_urls is derived
    from the whole table. openjobs.py exists partly to reach those very ATS
    families. An unrecognised parameter might be the identifier, so it stays.
    """
    from urllib.parse import urlsplit, parse_qsl, urlencode
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    host = (parts.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    host = HOST_ALIASES.get(host, host)
    path = (parts.path or "").rstrip("/")
    kept = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                  if k.lower() not in TRACKING_PARAMS
                  and not k.lower().startswith("utm_"))
    query = f"?{urlencode(kept)}" if kept else ""
    if not host:  # not an absolute URL; compare what we were given
        return url.strip().rstrip("/")
    return f"{host}{path}{query}"


def make_job_uid(ats, company, ext_id):
    """Stable unique id for a posting across scans.

    Mirrors job_monitor.job_key so the same posting maps to the same row.
    """
    return f"{ats}:{company}:{ext_id}"


def make_slug(company, title, uid):
    """Short, filesystem-safe handle: company__role__shortid."""
    def clean(s):
        s = (s or "").lower()
        s = "".join(c if c.isalnum() or c in " -" else "" for c in s)
        return "-".join(s.split())[:40].strip("-")
    short = hashlib.sha1(uid.encode()).hexdigest()[:4]
    return f"{clean(company)}__{clean(title)}__{short}"


# --- Database --------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    uid           TEXT PRIMARY KEY,      -- ats:company:ext_id
    slug          TEXT UNIQUE NOT NULL,  -- company__role__shortid
    ext_id        TEXT NOT NULL,         -- the ATS's own id
    ats           TEXT NOT NULL,
    company       TEXT NOT NULL,          -- board slug: uid field AND URL segment
    company_display TEXT,                  -- readable name; cosmetic only
    title         TEXT NOT NULL,
    location      TEXT,
    location_type TEXT,                  -- remote | verify | onsite/hybrid (scan tag)
    url           TEXT,
    posted_at     TEXT,                  -- best available posting date (ISO) or ''
    date_source   TEXT,                  -- which field/ATS it came from (~ = approx)
    description   TEXT,                  -- full JD, filled at draft time
    salary_min    INTEGER,
    salary_max    INTEGER,
    state         TEXT NOT NULL DEFAULT 'discovered',
    outcome       TEXT,                  -- set when closed
    folder        TEXT,                  -- path to application package
    notes         TEXT,
    source        TEXT NOT NULL DEFAULT 'unknown',  -- which path ingested it
    fit_score     INTEGER,                -- deterministic 0-100 fit score
    fit_reasons   TEXT,                   -- short string: what drove fit_score
    llm_fit_score INTEGER,                -- 0-100 from the LLM verdict tier
    llm_rationale TEXT,                   -- one-line LLM fit rationale
    llm_coding_bar TEXT,                  -- LLM read of the hands-on coding bar
    skip_reason   TEXT,                   -- structured reason captured on skip
    close_reason  TEXT,                   -- structured reason captured on close
    vote          TEXT,                   -- up | down | NULL, operator lead feedback
    vote_note     TEXT,                   -- optional one-line reason for the vote
    voted_at      TEXT,                   -- timestamp of the last vote
    digested_at   TEXT,                   -- last time this lead was in a posted digest
    read_at       TEXT,                   -- when the operator processed this lead; NULL = unread
    gate_decision TEXT,                  -- PROCEED | CONDITIONAL | NEEDS_REVIEW | DO_NOT_APPLY | ERROR
    gate_json     TEXT,                  -- structured requirements, incl. human rulings
    gate_report_path TEXT,               -- path to fit-report.md
    gate_at       TEXT,
    gate_override_reason TEXT,           -- mandatory written reason to bypass the gate
    gate_overridden_at TEXT,
    interview_rounds TEXT,               -- JSON array of round labels, in order
    interview_at INTEGER,                -- 1-based marker into interview_rounds
    interview_decision INTEGER,          -- 1 when every round is done
    interview_next TEXT,                 -- free text: what is actually next
    interview_updated TEXT,              -- when the marker last moved
    discovered_at TEXT NOT NULL,
    queued_at     TEXT,
    drafted_at    TEXT,
    ready_at      TEXT,
    applied_at    TEXT,
    closed_at     TEXT,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_uid   TEXT NOT NULL REFERENCES jobs(uid),
    from_state TEXT,
    to_state  TEXT NOT NULL,
    at        TEXT NOT NULL,
    note      TEXT
);

CREATE TABLE IF NOT EXISTS files (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_uid   TEXT NOT NULL REFERENCES jobs(uid),
    kind      TEXT NOT NULL,        -- resume | cover_letter | jd | other
    version   INTEGER NOT NULL DEFAULT 1,
    path      TEXT NOT NULL,
    sha256    TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gaps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_uid    TEXT NOT NULL REFERENCES jobs(uid),
    requirement TEXT NOT NULL,
    plan       TEXT,
    hours_estimate INTEGER,
    deadline   TEXT,
    status     TEXT NOT NULL DEFAULT 'open',   -- open | closed
    closed_reason TEXT,                        -- planned (human) | reclassified (system)
    close_note TEXT,                           -- mandatory written reason for a human close
    created_at TEXT NOT NULL,
    closed_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_files_job ON files(job_uid);
CREATE INDEX IF NOT EXISTS idx_gaps_job ON gaps(job_uid);
"""

# Column that gets stamped when entering each state.
STATE_TIMESTAMP = {
    "queued": "queued_at",
    "drafted": "drafted_at",
    "ready": "ready_at",
    "applied": "applied_at",
    "closed": "closed_at",
}


# Columns added to the jobs table after the original schema shipped. CREATE
# TABLE IF NOT EXISTS never adds columns to an existing table, so any DB created
# before these landed must be migrated forward on open (additive only, never
# destructive). Keep this list in sync with the jobs table in SCHEMA.
ADDED_COLUMNS = {
    "location_type": "TEXT",
    "fit_score": "INTEGER",
    "fit_reasons": "TEXT",
    "llm_fit_score": "INTEGER",
    "llm_rationale": "TEXT",
    "llm_coding_bar": "TEXT",
    "skip_reason": "TEXT",
    "close_reason": "TEXT",
    "vote": "TEXT",
    "vote_note": "TEXT",
    "voted_at": "TEXT",
    "digested_at": "TEXT",
    "read_at": "TEXT",
    "gate_decision": "TEXT",
    "gate_json": "TEXT",
    "gate_report_path": "TEXT",
    "gate_at": "TEXT",
    "gate_override_reason": "TEXT",
    "gate_overridden_at": "TEXT",
    "gate_model": "TEXT",
    "interview_rounds": "TEXT",
    "interview_at": "INTEGER",
    "interview_decision": "INTEGER",
    "interview_next": "TEXT",
    "interview_updated": "TEXT",
    # Two discovery sources now (the ATS scanner and the open-jobs wide net).
    # Backfilled below rather than left NULL: every row that predates this
    # column came from the scanner, which is a fact and not a guess.
    "source": "TEXT NOT NULL DEFAULT 'unknown'",
    # `company` is a key and, for four ATSes, a path segment in
    # posting_endpoint, so it can never be prettified. The readable name
    # gets its own column instead. NULL on older rows; readers fall back
    # to `company`.
    "company_display": "TEXT",
}

# Columns added to the gaps table after it first shipped. The gaps table
# itself is new and unmerged (not yet in the production DB), but a dev DB
# could already have created it before closed_reason existed, so migrate it
# forward the same additive way as the jobs table above.
ADDED_GAPS_COLUMNS = {
    "closed_reason": "TEXT",
    "close_note": "TEXT",   # mandatory written reason for a human gap-close
}

# The gate columns have dedicated, audited setters (set_gate, set_override).
# set_fields is a generic UPDATE with no audit trail, so letting it write
# these would be an unaudited way to mark a job PROCEED. Refuse them there.
_GATE_COLUMNS = {
    "gate_decision", "gate_json", "gate_report_path", "gate_at",
    "gate_override_reason", "gate_overridden_at", "gate_model",
}

# read_at has a dedicated audited setter (set_read) for the same reason the
# gate columns do: set_fields writes no state_log row, and an unaudited read
# stamp would silently drain the unread queue.
#
# notes deliberately stays writable by set_fields. It gates nothing, two
# existing tests use it as the example of a legitimate generic write, and
# set_notes is the audited path rather than a prohibition on the generic one.
_AUDITED_COLUMNS = {"read_at"}

# The interview columns have dedicated, audited setters (set_rounds, set_stage)
# for the same reason: set_fields writes no state_log row, and the board is a
# claim about where a real conversation stands. A marker that moved with no
# record of who moved it or when is exactly the kind of quiet drift the rest of
# this module is built to prevent.
_INTERVIEW_COLUMNS = {
    "interview_rounds", "interview_at", "interview_decision",
    "interview_next", "interview_updated",
}

# The lifecycle itself. set_state is the only thing allowed to move these, and
# it is the only writer that validates the edge and leaves a state_log row.
# `outcome` is here because the reopen guard READS it to decide whether a
# closed job may come back: an unaudited set_fields(outcome='ghosted') on a
# rejected row would turn a final decision into a reopenable one with nothing
# in the audit trail to show it happened. `close_reason` is deliberately NOT
# blocked; job_hound_mcp writes it alongside a close it just made.
_LIFECYCLE_COLUMNS = {"state", "outcome", "closed_at"}

# The base frame every application starts on: a recruiter screen, three
# interview rounds, and (appended at render time) a decision. A default, not a
# rule: every entry can be relabelled, reordered, added to, or removed, which is
# how a loop that runs two rounds or five still renders honestly.
#
# The bare "round" labels are placeholders to be replaced with who or what the
# round actually is. They deliberately carry NO number. Every surface here is
# 1-based over the whole list, recruiter screen included, so the old "round 1"
# seed sat in position 2: `jh stage <ident> 2` answered "round 1", `jh rounds`
# printed "2. round 1", and the board captioned that same node "round 1"
# because its caption counts real rounds and skips the screen. Three surfaces,
# two numbers, one slot. A placeholder that names no number cannot disagree
# with the position it sits in, and the number the human needs is the derived
# caption, which is right by construction.
DEFAULT_ROUNDS = ["recruiter", "round", "round", "round"]

ROUND_LABEL_MAX = 60
MAX_ROUNDS = 12

# What is next, in free text ("final round, not yet scheduled"). Renders on one
# line under a lane, so it is capped well below NOTE_MAX.
INTERVIEW_NEXT_MAX = 200


def _col(row, name):
    """Read one column from a sqlite3.Row or a plain dict. Missing -> None."""
    try:
        return row[name]
    except (KeyError, IndexError):
        return None


def _stage_timestamp(occurred):
    """Validate a YYYY-MM-DD round date and return it as an ISO timestamp.

    Refuses a future date: a round cannot have happened tomorrow, and accepting
    one would make the quiet clock negative on a board whose only job is
    measuring how long something has been silent.
    """
    try:
        day = datetime.strptime(str(occurred).strip(), "%Y-%m-%d")
    except (ValueError, TypeError):
        raise ValueError(
            f"a round date must be YYYY-MM-DD, got {occurred!r}") from None
    day = day.replace(tzinfo=timezone.utc)
    if day > datetime.now(timezone.utc):
        raise ValueError(f"a round cannot have happened in the future: {occurred}")
    return day.isoformat()


def rounds_of(row):
    """Decode a job's round list. Never raises, always returns a list.

    A malformed or hand-edited blob degrades to an empty list rather than
    taking down the board, which renders every live conversation at once.
    """
    raw = _col(row, "interview_rounds")
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(val, list):
        return []
    return [str(x).strip() for x in val if str(x).strip()]

# A working note about a lead, not a one-line vote reason. vote_note is the
# one-liner, and the write API caps it at 280 (jobapi.VOTE_NOTE_MAX) where it
# writes it. This is the field you paste a recruiter email into.
NOTE_MAX = 4000


class JobDB:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        # The write API, the nightly scan, the ingest timer and bin/jh all
        # open this file, and the web inbox opens it read-only alongside
        # them. WAL lets readers run while a writer holds the lock; the busy
        # timeout makes a second writer wait its turn instead of failing
        # immediately with "database is locked".
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        """Add any post-v1 columns missing from an older DB. Additive only."""
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        for col, decl in ADDED_COLUMNS.items():
            if col not in existing:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
                if col == "source":
                    # Recover provenance for rows that predate the column from
                    # the audit trail, which already recorded how each arrived.
                    # Asserting they were all scanner rows would be wrong for
                    # 26 of the 571 live ones, and the 14 hand-fetched are the
                    # highest-signal rows in the pipeline: the 2026-08-19
                    # coverage audit found NONE of the interview loops came
                    # from the scanner.
                    #
                    # Inside the add branch so it fires exactly once, for the
                    # reason spelled out under read_at below.
                    for note, name in (("fetched by url", "fetch"),
                                       ("mission-control ingest", "mission-control")):
                        self.conn.execute(
                            "UPDATE jobs SET source = ? WHERE uid IN "
                            "(SELECT job_uid FROM state_log WHERE note = ?)",
                            (name, note))
                    # Whatever is left really did come from the scanner: it is
                    # the only other writer that existed.
                    self.conn.execute(
                        "UPDATE jobs SET source = 'scan' WHERE source = 'unknown'")
                if col == "read_at":
                    # Start clean. Every lead that existed before the inbox
                    # shipped counts as already processed, so the queue opens
                    # empty and fills from the next scan.
                    #
                    # This runs INSIDE the add branch so it fires exactly once,
                    # at the migration. A "WHERE read_at IS NULL" backfill in
                    # the body of _migrate would look equivalent and would be
                    # catastrophic: it re-runs on every open, so every newly
                    # discovered lead would be stamped read before anyone saw it
                    # and the unread queue would be permanently empty. The
                    # gate_model backfill below is safe to re-run only because
                    # its WHERE clause is self-limiting. This one is not.
                    self.conn.execute(
                        "UPDATE jobs SET read_at = ?", (now_iso(),))
        existing_gaps = {r["name"] for r in self.conn.execute("PRAGMA table_info(gaps)")}
        for col, decl in ADDED_GAPS_COLUMNS.items():
            if col not in existing_gaps:
                self.conn.execute(f"ALTER TABLE gaps ADD COLUMN {col} {decl}")
        # Every row gated before provenance existed was evaluated with the only
        # model ever run in production. Stamp it once; this is a fact, not a guess.
        self.conn.execute(
            "UPDATE jobs SET gate_model = 'claude-opus-4-8' "
            "WHERE gate_decision IS NOT NULL AND gate_model IS NULL")
        self.conn.commit()

    def close(self):
        self.conn.close()

    # -- ingest ------------------------------------------------------------

    def upsert_job(self, job):
        """Insert a scanned job if new. Returns True if it was newly added.

        `job` is the dict shape from job_monitor: id, title, location, url,
        company, ats. Existing jobs are left untouched (we don't clobber state).
        """
        uid = make_job_uid(job["ats"], job["company"], job["id"])
        cur = self.conn.execute("SELECT 1 FROM jobs WHERE uid = ?", (uid,))
        if cur.fetchone():
            return False
        slug = make_slug(job["company"], job["title"], uid)
        ts = now_iso()
        self.conn.execute(
            """INSERT INTO jobs
               (uid, slug, ext_id, ats, company, company_display, title,
                location, location_type, url, posted_at, date_source,
                description, source, state, discovered_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'discovered', ?, ?)""",
            (uid, slug, str(job["id"]), job["ats"], job["company"],
             job.get("company_display") or job["company"],
             job["title"], job.get("location", ""), job.get("location_type", ""),
             job.get("url", ""), job.get("posted_at", ""),
             job.get("date_source", ""), job.get("description", ""),
             job.get("source") or "unknown", ts, ts),
        )
        self.conn.execute(
            "INSERT INTO state_log (job_uid, from_state, to_state, at, note) "
            "VALUES (?, NULL, 'discovered', ?, ?)",
            (uid, ts, job.get("source") or "unknown"),
        )
        self.conn.commit()
        return True

    def known_urls(self):
        """Every non-empty posting URL in the pipeline, canonicalised.

        Used by the wide net's second dedup layer. It lives here so job_cli
        stays free of raw SQL and jobdb remains the only module that talks to
        the database.
        """
        return {canonical_url(r["url"]) for r in self.conn.execute(
            "SELECT url FROM jobs WHERE url IS NOT NULL AND url != ''")
            if canonical_url(r["url"])}

    def ingest_scan(self, jobs):
        """Bulk upsert a scan's results. Returns count of newly added."""
        added = 0
        for j in jobs:
            if self.upsert_job(j):
                added += 1
        return added

    # -- state transitions -------------------------------------------------

    def set_state(self, uid, to_state, note=None, outcome=None, reason=None):
        """Move a job, and stamp its structured reason in the same transaction.

        `reason` is the disposition reason for the two states that have a
        column for it (see REASON_COLUMN). It is written in the same UPDATE as
        the state itself rather than by a follow-up set_fields call, so a crash
        between two commits cannot leave a terminal row with no reason. That
        matters most for `closed`, whose only outgoing transition is the
        narrow ghosted reopen below.

        Reopening a `ghosted` close clears the close columns and, unlike every
        other transition, does NOT restamp the destination's timestamp: the
        application really was submitted back on the original applied_at, and
        the Reply Window measures employer response time from it.
        """
        if to_state not in STATES:
            raise TransitionError(f"unknown state: {to_state}")
        if reason and to_state not in REASON_COLUMN:
            raise TransitionError(
                f"reason is only stored for {sorted(REASON_COLUMN)}, "
                f"not '{to_state}'. Use note, which is audited for every state.")
        row = self.get(uid)
        if not row:
            raise TransitionError(f"no job with uid {uid}")
        frm = row["state"]
        if to_state == frm:
            return row  # no-op
        if to_state not in TRANSITIONS.get(frm, set()):
            raise TransitionError(
                f"illegal transition {frm} -> {to_state} "
                f"(allowed: {sorted(TRANSITIONS.get(frm, []))})"
            )
        if outcome and to_state != "closed":
            # An outcome is what a close records. Before the ghosted reopen it
            # was merely meaningless elsewhere; now it is load-bearing, because
            # the reopen guard reads it. Worse, `outcome = ?` is appended to the
            # same UPDATE as the reopen's `outcome = NULL` and SQLite takes the
            # later assignment, so a reopen that carried one came back live
            # still saying 'ghosted'. Refuse it, the way the API already
            # refuses a `reason` sent for a state that has no column.
            raise TransitionError(
                f"an outcome is only recorded on a close, not on "
                f"'{to_state}'. Drop it, or close the job instead.")
        if to_state == "closed" and outcome and outcome not in OUTCOMES:
            raise TransitionError(f"unknown outcome: {outcome}")
        reopening = frm == "closed"
        if reopening and row["outcome"] != "ghosted":
            raise TransitionError(
                f"only a ghosted job can reopen; this one closed as "
                f"{row['outcome']!r}. A decided close is final: record the new "
                f"conversation as its own job instead.")

        ts = now_iso()
        sets = ["state = ?", "updated_at = ?"]
        vals = [to_state, ts]
        # A reopen restores a row to a state it already reached once, so its
        # timestamp is already correct and must not be moved forward.
        if to_state in STATE_TIMESTAMP and not reopening:
            sets.append(f"{STATE_TIMESTAMP[to_state]} = ?")
            vals.append(ts)
        if reopening:
            # The row is live again, so nothing on it may still say otherwise.
            # The close itself survives in state_log, which is where the
            # 13 days of silence are actually remembered.
            sets += ["outcome = NULL", "closed_at = NULL",
                     "close_reason = NULL"]
        if outcome:
            sets.append("outcome = ?")
            vals.append(outcome)
        if reason:
            sets.append(f"{REASON_COLUMN[to_state]} = ?")
            vals.append(reason)
        vals.append(uid)
        self.conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE uid = ?", vals)
        self.conn.execute(
            "INSERT INTO state_log (job_uid, from_state, to_state, at, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (uid, frm, to_state, ts, note),
        )
        self.conn.commit()
        return self.get(uid)

    # -- file + field updates ---------------------------------------------

    def set_fields(self, uid, **fields):
        # The gate columns have dedicated, audited setters (set_gate,
        # set_override). This is a generic UPDATE with no audit trail, so
        # letting it write them would be an unaudited way to mark a job
        # PROCEED. Refuse.
        bad = _GATE_COLUMNS & set(fields)
        if bad:
            raise ValueError(
                f"set_fields cannot write gate columns {sorted(bad)}. "
                "Use set_gate or set_override, which are audited.")
        bad_audited = _AUDITED_COLUMNS & set(fields)
        if bad_audited:
            raise ValueError(
                f"set_fields cannot write {sorted(bad_audited)}. "
                "Use set_read, which is audited.")
        bad_interview = _INTERVIEW_COLUMNS & set(fields)
        if bad_interview:
            raise ValueError(
                f"set_fields cannot write {sorted(bad_interview)}. "
                "Use set_rounds or set_stage, which are audited.")
        bad_lifecycle = _LIFECYCLE_COLUMNS & set(fields)
        if bad_lifecycle:
            raise ValueError(
                f"set_fields cannot write {sorted(bad_lifecycle)}. "
                "Use set_state, which validates the transition and audits it.")
        if not fields:
            return
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(
            f"UPDATE jobs SET {cols} WHERE uid = ?",
            (*fields.values(), uid),
        )
        self.conn.commit()

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

    def set_read(self, uid, read=True):
        """Mark a lead processed, or push it back into the unread queue.

        read_at IS NULL is the only definition of unread. Audited in state_log
        with the state unchanged, the same shape as set_vote, so `jh show`
        history shows when a lead left the queue and when it came back.
        """
        row = self.get(uid)
        if not row:
            raise ValueError(f"no job with uid {uid}")
        ts = now_iso()
        self.conn.execute(
            "UPDATE jobs SET read_at = ?, updated_at = ? WHERE uid = ?",
            (ts if read else None, ts, uid))
        self.conn.execute(
            "INSERT INTO state_log (job_uid, from_state, to_state, at, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (uid, row["state"], row["state"], ts, "read" if read else "unread"))
        self.conn.commit()
        return self.get(uid)

    def set_notes(self, uid, text):
        """Freeform working note on a lead. Audited, and load-bearing.

        fit.build_history reads notes as the stated reason for any job in a
        pursued state, so what lands here becomes part of the corpus that
        teaches the ranker. That is deliberate (see the lead-inbox design
        spec), which is why it goes through an audited setter rather than
        set_fields. Empty or whitespace-only text clears the column.
        """
        row = self.get(uid)
        if not row:
            raise ValueError(f"no job with uid {uid}")
        clean = (text or "").strip()[:NOTE_MAX] or None
        ts = now_iso()
        self.conn.execute(
            "UPDATE jobs SET notes = ?, updated_at = ? WHERE uid = ?",
            (clean, ts, uid))
        # First line only, and capped: a summary that reprints a 4000-character
        # note is not a summary, and `jh show` dumps these straight to a terminal.
        summary = clean.splitlines()[0][:120] if clean else "cleared"
        self.conn.execute(
            "INSERT INTO state_log (job_uid, from_state, to_state, at, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (uid, row["state"], row["state"], ts, f"note: {summary}"))
        self.conn.commit()
        return self.get(uid)

    # -- interview rounds ---------------------------------------------------

    def set_rounds(self, uid, labels):
        """Replace a job's ordered round list. Audited, state unchanged.

        Labels are free text on purpose. An enum here would recreate, one level
        down, the rigidity that made a fixed stage ladder wrong: Bamboo's round
        three was booked as the technical and became a peer plus a TPM because
        of interviewer availability, and no vocabulary chosen in advance
        survives that.

        A marker pointing past the end of a shortened list is clamped to the
        last round rather than left dangling, since a marker outside its own
        list has no meaning the board could render.
        """
        row = self.get(uid)
        if not row:
            raise ValueError(f"no job with uid {uid}")
        clean = [str(l).strip()[:ROUND_LABEL_MAX]
                 for l in (labels or []) if str(l).strip()]
        if not clean:
            raise ValueError("a round list needs at least one round")
        if len(clean) > MAX_ROUNDS:
            raise ValueError(
                f"at most {MAX_ROUNDS} rounds (got {len(clean)})")

        at = _col(row, "interview_at")
        clamped = False
        if at and at > len(clean):
            at, clamped = len(clean), True

        ts = now_iso()
        self.conn.execute(
            "UPDATE jobs SET interview_rounds = ?, interview_at = ?, "
            "interview_updated = ?, updated_at = ? WHERE uid = ?",
            (json.dumps(clean), at, ts, ts, uid))
        audit = "rounds: " + ", ".join(clean)
        if clamped:
            audit += f" (marker clamped to {at})"
        self.conn.execute(
            "INSERT INTO state_log (job_uid, from_state, to_state, at, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (uid, row["state"], row["state"], ts, audit))
        self.conn.commit()
        return self.get(uid)

    def set_stage(self, uid, at=None, decision=False, next_note=None,
                  occurred=None):
        """Move the marker. Audited, state unchanged.

        `occurred` is the date the round actually happened (YYYY-MM-DD),
        defaulting to now. It exists because `interview_updated` is the clock
        The Loop measures silence from, and recording a round days after the
        fact used to reset that clock to the moment of RECORDING. Live case
        2026-08-20: Bamboo's technical was held 2026-08-13 and written down a
        week later, so the board read zero days quiet when the honest answer
        was seven. A page whose whole job is surfacing silence cannot have a
        clock that resets when you take notes.

        `at` is a 1-based position into the round list, meaning rounds 1..at-1
        are done and round `at` is the active or upcoming one. `decision=True`
        means every round is finished and the outcome is pending; it is never
        stored in the round list, because an outcome is already modelled by
        `state` and `outcome` and a second lifecycle would just disagree with
        the first one eventually.

        Seeds DEFAULT_ROUNDS when the job has no list yet, so a job whose loop
        nobody has described still lands somewhere sensible.

        next_note of None clears any previous note. A stale "what is next" is
        worse than none on a board whose only job is saying what is current.
        """
        row = self.get(uid)
        if not row:
            raise ValueError(f"no job with uid {uid}")

        rounds = rounds_of(row)
        seeded = False
        if not rounds:
            rounds, seeded = list(DEFAULT_ROUNDS), True

        if decision:
            at_val, dec = None, 1
            label = "decision"
        else:
            if not isinstance(at, int) or isinstance(at, bool):
                raise ValueError("a stage marker must be a round number")
            if at < 1 or at > len(rounds):
                raise ValueError(
                    f"round {at} is outside this job's {len(rounds)}-round "
                    f"list. Add it first: jh rounds <ident> --add \"...\"")
            at_val, dec = at, 0
            label = rounds[at - 1]

        note = (next_note or "").strip()[:INTERVIEW_NEXT_MAX] or None
        ts = now_iso()
        moved = _stage_timestamp(occurred) if occurred else ts
        self.conn.execute(
            "UPDATE jobs SET interview_rounds = ?, interview_at = ?, "
            "interview_decision = ?, interview_next = ?, "
            "interview_updated = ?, updated_at = ? WHERE uid = ?",
            (json.dumps(rounds), at_val, dec, note, moved, ts, uid))
        # The position, not just the label. A numberless placeholder cannot say
        # which slot moved on its own, so staging positions 2, 3 and 4 of the
        # seeded frame would write three identical notes, and the row keeps only
        # the CURRENT marker. It is the 1-based list position (what `jh rounds`
        # prints and what `jh stage` takes), deliberately NOT a round number:
        # the board's round numbers skip the recruiter screen, and putting one
        # here would reintroduce the very disagreement this frame removed.
        audit = f"stage: {label}" if decision else f"stage: {label} (position {at})"
        if seeded:
            audit += " (seeded default rounds)"
        if occurred:
            # A backdated clock has to be visible in the trail, or it reads as
            # though the marker moved on a day nothing happened.
            audit += f" (held {occurred}, recorded {ts[:10]})"
        if note:
            audit += f". {note}"
        self.conn.execute(
            "INSERT INTO state_log (job_uid, from_state, to_state, at, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (uid, row["state"], row["state"], ts, audit))
        self.conn.commit()
        return self.get(uid)

    def interviewing(self):
        """Every job with a live conversation, oldest marker movement first.

        The board's only query. Jobs with no marker yet sort last (NULL
        interview_updated), since they have nothing to be stale about.
        """
        return self.conn.execute(
            "SELECT * FROM jobs WHERE state = 'interviewing' "
            "ORDER BY interview_updated IS NULL, interview_updated ASC"
        ).fetchall()

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

    def record_file(self, uid, kind, path, version=None):
        if version is None:
            cur = self.conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM files "
                "WHERE job_uid = ? AND kind = ?", (uid, kind))
            version = cur.fetchone()[0]
        p = Path(path)
        sha = None
        if p.exists():
            sha = hashlib.sha256(p.read_bytes()).hexdigest()
        self.conn.execute(
            "INSERT INTO files (job_uid, kind, version, path, sha256, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (uid, kind, version, str(path), sha, now_iso()),
        )
        self.conn.commit()
        return version

    # -- queries -----------------------------------------------------------

    def get(self, uid):
        cur = self.conn.execute("SELECT * FROM jobs WHERE uid = ?", (uid,))
        return cur.fetchone()

    def get_by_slug(self, slug):
        cur = self.conn.execute("SELECT * FROM jobs WHERE slug = ?", (slug,))
        return cur.fetchone()

    def resolve(self, ident):
        """Accept a full uid, a slug, or a unique slug prefix."""
        r = self.get(ident) or self.get_by_slug(ident)
        if r:
            return r
        cur = self.conn.execute(
            "SELECT * FROM jobs WHERE slug LIKE ? ", (ident + "%",))
        rows = cur.fetchall()
        if len(rows) == 1:
            return rows[0]
        if len(rows) > 1:
            raise TransitionError(f"ambiguous identifier '{ident}' matches {len(rows)} jobs")
        return None

    def list(self, state=None, limit=None):
        q = "SELECT * FROM jobs"
        params = []
        if state:
            q += " WHERE state = ?"
            params.append(state)
        q += " ORDER BY discovered_at DESC"
        if limit:
            q += " LIMIT ?"
            params.append(limit)
        return self.conn.execute(q, params).fetchall()

    def next_to_apply(self):
        """The next job ready for a human to submit."""
        cur = self.conn.execute(
            "SELECT * FROM jobs WHERE state = 'ready' "
            "ORDER BY ready_at ASC LIMIT 1")
        return cur.fetchone()

    def next_to_draft(self):
        """The next queued job awaiting generation."""
        cur = self.conn.execute(
            "SELECT * FROM jobs WHERE state = 'queued' "
            "ORDER BY queued_at ASC LIMIT 1")
        return cur.fetchone()

    def counts(self):
        cur = self.conn.execute(
            "SELECT state, COUNT(*) n FROM jobs GROUP BY state")
        return {r["state"]: r["n"] for r in cur.fetchall()}

    def files_for(self, uid, kind=None):
        q = "SELECT * FROM files WHERE job_uid = ?"
        params = [uid]
        if kind:
            q += " AND kind = ?"
            params.append(kind)
        q += " ORDER BY kind, version"
        return self.conn.execute(q, params).fetchall()

    def history(self, uid):
        return self.conn.execute(
            "SELECT * FROM state_log WHERE job_uid = ? ORDER BY at", (uid,)
        ).fetchall()

    def last_activity(self, uids=None):
        """Map uid to the ISO timestamp of the last time the operator ACTED on it.

        The clock behind staleness.py. Reads state_log rather than
        jobs.updated_at, which the nightly scoring pass bumps without any
        state change (that would make every lead look permanently fresh).

        Gate runs and read stamps are excluded: neither is a decision about
        the lead, and counting them would let a lead quiet its own staleness
        alarm without the operator doing anything. set_gate writes a note starting
        with "gate: " (e.g. "gate: PROCEED"), matched by prefix.

        Read stamps (set_read) write the plain note "read" or "unread" (no
        colon, no prefix) with from_state == to_state, since a read stamp
        never changes the lifecycle state. Matching that note text ALONE
        would also catch a genuine set_state transition whose caller-supplied
        --note happens to be the literal word "read" or "unread" (reachable
        from the CLI and from jobapi.py's body.note), wrongly dropping a real
        action. So the exclusion also requires from_state == to_state, which
        a real transition can never have: set_state returns before writing
        any state_log row when to_state == frm (see set_state's "no-op"
        guard above), so a same-state row in state_log is only ever written
        by set_vote/set_read/set_notes/set_gate/set_override/
        audit_gate_rule/close_gap, never by a lifecycle transition. This also
        holds for the 411 legacy read-stamp rows already in production,
        which were written by the same set_read code path and so already
        satisfy from_state == to_state.

        Transitions, votes, and notes all count.

        Jobs with no qualifying rows are absent from the result, which callers
        must treat as "unknown", not "stale".
        """
        q = ("SELECT job_uid, MAX(at) AS at FROM state_log "
             "WHERE COALESCE(note, '') NOT LIKE 'gate:%' "
             "AND NOT (from_state = to_state "
             "AND COALESCE(note, '') IN ('read', 'unread'))")
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

    # -- fit gate ------------------------------------------------------------

    def set_gate(self, uid, decision, gate_json, report_path, model=None):
        """Record a gate run. Audited in state_log without changing state.

        Also clears any prior override. An override waives a SPECIFIC
        decision; a new decision is a new fact, so the old waiver is void and
        the human must look again. This is deliberate for gate.recompute()
        too (called after a human ruling on an UNSURE item): a recompute is
        still a new decision, and clearing on it is the conservative,
        fail-closed choice. This makes override freshness a property of the
        data (does an override exist right now) rather than a timestamp
        comparison, which can tie when now_iso()'s second-level granularity
        lands an override and a re-gate in the same second.
        """
        ts = now_iso()
        self.conn.execute(
            "UPDATE jobs SET gate_decision = ?, gate_json = ?, gate_model = ?, "
            "gate_report_path = ?, gate_at = ?, updated_at = ?, "
            "gate_override_reason = NULL, gate_overridden_at = NULL "
            "WHERE uid = ?",
            (decision, gate_json, model, str(report_path), ts, ts, uid))
        row = self.get(uid)
        self.conn.execute(
            "INSERT INTO state_log (job_uid, from_state, to_state, at, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (uid, row["state"], row["state"], ts, f"gate: {decision}"))
        self.conn.commit()
        return self.get(uid)

    def set_override(self, uid, reason):
        """Bypass the gate. The reason is mandatory and is audited.

        An override is the only thing that lets DO_NOT_APPLY or ERROR draft.
        If overrides become habitual the gate is theater, so the audit trail
        here is the only way to notice that happening.

        An override waives a decision the gate actually rendered, so a job
        with no gate_decision at all (never gated) is refused here too,
        mirroring the mandatory-reason guard. Defense in depth: cmd_gate_
        override already refuses this at the CLI layer.
        """
        if not (reason or "").strip():
            raise ValueError("an override requires a written reason")
        row = self.get(uid)
        if not row or not row["gate_decision"]:
            raise ValueError(
                f"{uid} has never been gated, so there is no decision to override")
        ts = now_iso()
        self.conn.execute(
            "UPDATE jobs SET gate_override_reason = ?, gate_overridden_at = ?, "
            "updated_at = ? WHERE uid = ?", (reason, ts, ts, uid))
        row = self.get(uid)
        self.conn.execute(
            "INSERT INTO state_log (job_uid, from_state, to_state, at, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (uid, row["state"], row["state"], ts, f"gate override: {reason}"))
        self.conn.commit()
        return self.get(uid)

    def audit_gate_rule(self, uid, n, old_hard, new_hard, note):
        """Audit a human ruling on an UNSURE requirement, the same way
        set_override audits an override. The reclassification itself lives in
        gate_json (written by set_gate on the recompute that follows); this row
        is the proof a human did it, and why, so `jh show` history reveals it.
        """
        ts = now_iso()
        row = self.get(uid)
        frm = "HARD" if old_hard else "SOFT"
        to = "HARD" if new_hard else "SOFT"
        self.conn.execute(
            "INSERT INTO state_log (job_uid, from_state, to_state, at, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (uid, row["state"], row["state"], ts,
             f"gate-rule #{n} {frm}->{to}: {note}"))
        self.conn.commit()

    def add_gap(self, uid, requirement):
        cur = self.conn.execute(
            "INSERT INTO gaps (job_uid, requirement, status, created_at) "
            "VALUES (?, ?, 'open', ?)", (uid, requirement, now_iso()))
        self.conn.commit()
        return cur.lastrowid

    def plan_gap(self, gap_id, plan, hours_estimate, deadline):
        """Returns the number of gap rows updated (0 if gap_id does not exist)."""
        cur = self.conn.execute(
            "UPDATE gaps SET plan = ?, hours_estimate = ?, deadline = ? "
            "WHERE id = ?", (plan, hours_estimate, deadline, gap_id))
        self.conn.commit()
        return cur.rowcount

    def close_gap(self, gap_id, reason):
        """The HUMAN close, via `jh gap-close`: he did the work. Recorded as
        closed_reason='planned' so a later reconcile never reopens it.

        Closing a gap unblocks drafting, so it is a decision, and the reason
        is mandatory and audited, the same rule set_override already follows.

        Returns the number of gap rows updated (0 if gap_id does not exist).
        """
        if not (reason or "").strip():
            raise ValueError("closing a gap requires a written reason")
        ts = now_iso()
        cur = self.conn.execute(
            "UPDATE gaps SET status = 'closed', closed_at = ?, "
            "closed_reason = 'planned', close_note = ? WHERE id = ?",
            (ts, reason, gap_id))
        if cur.rowcount:
            gap = self.conn.execute(
                "SELECT job_uid FROM gaps WHERE id = ?", (gap_id,)).fetchone()
            row = self.get(gap["job_uid"])
            self.conn.execute(
                "INSERT INTO state_log (job_uid, from_state, to_state, at, note) "
                "VALUES (?, ?, ?, ?, ?)",
                (gap["job_uid"], row["state"], row["state"], ts,
                 f"gap closed: {reason}"))
        self.conn.commit()
        return cur.rowcount

    def close_gaps_not_in(self, uid, keep_requirements):
        """The SYSTEM close. Closes every OPEN gap for this job whose
        requirement is not in keep_requirements. Used to reconcile the gaps
        table when a ruling moves a requirement off the hard-NONE set (e.g.
        HARD -> SOFT). Recorded as closed_reason='reclassified', since the
        human never did any work here and this gap should reopen if the
        requirement becomes a hard NONE again.

        Only ever closes, never reopens: a gap already closed (by hand or by
        an earlier reconcile) is left alone. Touches only this job's gaps.
        Returns the number of gaps closed.
        """
        keep = list(keep_requirements)
        ts = now_iso()
        if keep:
            placeholders = ", ".join("?" for _ in keep)
            cur = self.conn.execute(
                f"UPDATE gaps SET status = 'closed', closed_at = ?, "
                f"closed_reason = 'reclassified' "
                f"WHERE job_uid = ? AND status = 'open' "
                f"AND requirement NOT IN ({placeholders})",
                (ts, uid, *keep))
        else:
            cur = self.conn.execute(
                "UPDATE gaps SET status = 'closed', closed_at = ?, "
                "closed_reason = 'reclassified' "
                "WHERE job_uid = ? AND status = 'open'",
                (ts, uid))
        self.conn.commit()
        return cur.rowcount

    def reopen_gap(self, gap_id):
        """Reopen a gap the SYSTEM previously auto-closed. Never call this on
        a gap closed_reason='planned': that means the human did the work, and
        a re-run must never undo that.

        plan, hours_estimate, and deadline are left as they are; require_pass
        still blocks until all three are present. Returns the number of gap
        rows updated (0 if gap_id does not exist).
        """
        cur = self.conn.execute(
            "UPDATE gaps SET status = 'open', closed_at = NULL, "
            "closed_reason = NULL WHERE id = ?", (gap_id,))
        self.conn.commit()
        return cur.rowcount

    def gaps_for(self, uid):
        return self.conn.execute(
            "SELECT * FROM gaps WHERE job_uid = ? ORDER BY id", (uid,)).fetchall()

    def gap_for_requirement(self, uid, requirement):
        """The most recent gap row for this job and requirement, or None.

        Used by gate._reconcile_gaps to tell an OPEN gap, a gap closed by the
        human (closed_reason='planned', never reopened), and a gap closed by
        the system (closed_reason='reclassified', reopens if the requirement
        becomes a hard NONE again) apart.
        """
        return self.conn.execute(
            "SELECT * FROM gaps WHERE job_uid = ? AND requirement = ? "
            "ORDER BY id DESC LIMIT 1", (uid, requirement)).fetchone()

    def open_gaps(self):
        return self.conn.execute(
            "SELECT * FROM gaps WHERE status = 'open' ORDER BY deadline, id"
        ).fetchall()

    def unplanned_gaps(self, uid):
        """Open gaps missing a plan, an hours estimate, or a deadline.

        All three are required. A plan with no hours and no deadline is a note,
        and a gap logged and admired is how one lost interview happened. A
        zero (or negative) hours estimate is not a plan either, so it counts
        as unplanned the same as a missing one.
        """
        return self.conn.execute(
            "SELECT * FROM gaps WHERE job_uid = ? AND status = 'open' AND ("
            "  plan IS NULL OR TRIM(plan) = '' "
            "  OR hours_estimate IS NULL OR hours_estimate <= 0 "
            "  OR deadline IS NULL OR TRIM(deadline) = '')",
            (uid,)).fetchall()

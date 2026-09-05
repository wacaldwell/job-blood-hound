import sqlite3

import pytest

import jobdb

# The jobs table exactly as it stood right before Task 6 added the six gate
# columns (gate_decision, gate_json, gate_report_path, gate_at,
# gate_override_reason, gate_overridden_at). Mirrors the pattern in
# test_jobdb_schema.py's OLD_SCHEMA: hand-build the old table, insert a row
# with raw sqlite3, then open it with jobdb.JobDB so _migrate() runs for real.
OLD_SCHEMA_PRE_GATE = """
CREATE TABLE jobs (
    uid           TEXT PRIMARY KEY,
    slug          TEXT UNIQUE NOT NULL,
    ext_id        TEXT NOT NULL,
    ats           TEXT NOT NULL,
    company       TEXT NOT NULL,
    title         TEXT NOT NULL,
    location      TEXT,
    location_type TEXT,
    url           TEXT,
    posted_at     TEXT,
    date_source   TEXT,
    description   TEXT,
    salary_min    INTEGER,
    salary_max    INTEGER,
    state         TEXT NOT NULL DEFAULT 'discovered',
    outcome       TEXT,
    folder        TEXT,
    notes         TEXT,
    fit_score     INTEGER,
    fit_reasons   TEXT,
    llm_fit_score INTEGER,
    llm_rationale TEXT,
    llm_coding_bar TEXT,
    skip_reason   TEXT,
    close_reason  TEXT,
    vote          TEXT,
    vote_note     TEXT,
    voted_at      TEXT,
    digested_at   TEXT,
    discovered_at TEXT NOT NULL,
    queued_at     TEXT,
    drafted_at    TEXT,
    ready_at      TEXT,
    applied_at    TEXT,
    closed_at     TEXT,
    updated_at    TEXT NOT NULL
);
"""


def _db(tmp_path):
    db = jobdb.JobDB(tmp_path / "t.db")
    db.upsert_job({"ats": "greenhouse", "company": "globex", "id": "1",
                   "title": "Senior Staff Operations Engineer", "location": "Remote"})
    return db, jobdb.make_job_uid("greenhouse", "globex", "1")


def test_gate_columns_exist_and_round_trip(tmp_path):
    db, uid = _db(tmp_path)
    db.set_gate(uid, "DO_NOT_APPLY", '{"requirements": []}', "/tmp/fit-report.md")
    r = db.get(uid)
    assert r["gate_decision"] == "DO_NOT_APPLY"
    assert r["gate_report_path"] == "/tmp/fit-report.md"
    assert r["gate_at"]


def test_override_is_recorded_with_a_reason_and_audited(tmp_path):
    db, uid = _db(tmp_path)
    db.set_gate(uid, "DO_NOT_APPLY", "{}", "/tmp/r.md")
    db.set_override(uid, "Recruiter reached out directly, worth the shot.")
    r = db.get(uid)
    assert "Recruiter reached out" in r["gate_override_reason"]
    assert r["gate_overridden_at"]
    notes = [h["note"] or "" for h in db.history(uid)]
    assert any("override" in n.lower() for n in notes), "override must be audited"


def test_gap_lifecycle(tmp_path):
    db, uid = _db(tmp_path)
    gid = db.add_gap(uid, "Proficient in Python or Go")
    assert db.unplanned_gaps(uid), "a fresh gap has no plan yet"
    db.plan_gap(gid, "Work through Go tour, build one CLI.", 20, "2026-08-01")
    assert not db.unplanned_gaps(uid), "a planned gap is no longer unplanned"
    g = db.gaps_for(uid)[0]
    assert g["hours_estimate"] == 20
    assert g["status"] == "open"
    db.close_gap(gid, "Worked through the Go tour, built a CLI.")
    assert db.gaps_for(uid)[0]["status"] == "closed"
    assert not db.open_gaps()


def test_a_gap_missing_any_of_plan_hours_deadline_stays_unplanned(tmp_path):
    db, uid = _db(tmp_path)
    gid = db.add_gap(uid, "correlation")
    db.plan_gap(gid, "read some blog posts", None, None)
    assert db.unplanned_gaps(uid), "a plan with no hours and no deadline is a note"


def test_migration_is_additive_on_an_existing_db(tmp_path):
    """The live DB has 363 rows, none of which have the six gate columns.
    Opening that DB with the current JobDB must ALTER them in without losing
    a single pre-existing row. Stands in for the live DB with one hand-built
    row on the pre-gate schema, inserted with raw sqlite3 the way
    test_jobdb_schema.py's test_old_db_is_migrated_forward does, so _migrate()
    is forced to run the ADDED_COLUMNS ALTER path for real."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(OLD_SCHEMA_PRE_GATE)
    conn.execute(
        "INSERT INTO jobs (uid, slug, ext_id, ats, company, title, "
        "state, discovered_at, updated_at) VALUES "
        "('greenhouse:acme:9', 'acme__role__9', '9', 'greenhouse', 'acme', "
        "'Old Role', 'discovered', '2026-01-01', '2026-01-01')"
    )
    conn.commit()
    conn.close()

    db = jobdb.JobDB(path)  # forces _migrate() to ALTER TABLE for real

    # The pre-existing row survived, untouched.
    row = db.get("greenhouse:acme:9")
    assert row is not None
    assert row["title"] == "Old Role"
    assert row["state"] == "discovered"

    # All six gate columns now exist and read back as None on that old row.
    for col in ("gate_decision", "gate_json", "gate_report_path", "gate_at",
                "gate_override_reason", "gate_overridden_at"):
        assert row[col] is None, f"missing migrated column {col}"

    # The gaps table now exists too.
    tables = {r["name"] for r in
              db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "gaps" in tables
    db.close()


def test_override_with_no_written_reason_is_refused(tmp_path):
    """The mandatory reason is the only thing that makes an override a decision
    rather than a silent bypass. If you cannot write the reason, that is the answer."""
    db, uid = _db(tmp_path)
    db.set_gate(uid, "DO_NOT_APPLY", "{}", "/tmp/r.md")
    for bad in ("", "   ", None):
        with pytest.raises(ValueError):
            db.set_override(uid, bad)
    # And the job is still not overridden.
    assert db.get(uid)["gate_override_reason"] is None

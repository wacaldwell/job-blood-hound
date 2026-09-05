import sqlite3

import jobdb

# The original pre-scoring schema (24 columns, no fit_score/llm_*/reason cols).
# A DB created at this version must be migrated forward when JobDB opens it.
OLD_SCHEMA = """
CREATE TABLE jobs (
    uid           TEXT PRIMARY KEY,
    slug          TEXT UNIQUE NOT NULL,
    ext_id        TEXT NOT NULL,
    ats           TEXT NOT NULL,
    company       TEXT NOT NULL,
    title         TEXT NOT NULL,
    location      TEXT,
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
    discovered_at TEXT NOT NULL,
    queued_at     TEXT,
    drafted_at    TEXT,
    ready_at      TEXT,
    applied_at    TEXT,
    closed_at     TEXT,
    updated_at    TEXT NOT NULL
);
"""


def test_old_db_is_migrated_forward(tmp_path):
    """An existing DB on the pre-scoring schema gains the new columns on open,
    with its existing rows preserved. Reproduces the no-such-column crash."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO jobs (uid, slug, ext_id, ats, company, title, "
        "state, discovered_at, updated_at) VALUES "
        "('greenhouse:acme:9', 'acme__role__9', '9', 'greenhouse', 'acme', "
        "'Old Role', 'discovered', '2026-01-01', '2026-01-01')"
    )
    conn.commit()
    conn.close()

    db = jobdb.JobDB(path)
    cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(jobs)")}
    for c in ("location_type", "fit_score", "fit_reasons", "llm_fit_score",
              "llm_rationale", "llm_coding_bar", "skip_reason", "close_reason"):
        assert c in cols, f"missing migrated column {c}"
    # Pre-existing row survived and the new columns are now settable on it.
    db.set_fields("greenhouse:acme:9", fit_score=88, fit_reasons="kept")
    row = db.get("greenhouse:acme:9")
    assert row["title"] == "Old Role"
    assert row["fit_score"] == 88
    db.close()


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


def test_old_gaps_table_is_migrated_forward_with_closed_reason(tmp_path):
    """The gaps table is new and unmerged, but a dev DB could already have
    created it before closed_reason existed. That DB must gain the column
    on open, with its existing gap rows preserved."""
    path = tmp_path / "old_gaps.db"
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE jobs (
            uid TEXT PRIMARY KEY, slug TEXT UNIQUE NOT NULL, ext_id TEXT NOT NULL,
            ats TEXT NOT NULL, company TEXT NOT NULL, title TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'discovered',
            discovered_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE gaps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_uid TEXT NOT NULL REFERENCES jobs(uid),
            requirement TEXT NOT NULL,
            plan TEXT,
            hours_estimate INTEGER,
            deadline TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            closed_at TEXT
        );
    """)
    conn.execute(
        "INSERT INTO jobs (uid, slug, ext_id, ats, company, title, state, "
        "discovered_at, updated_at) VALUES "
        "('greenhouse:acme:9', 'acme__role__9', '9', 'greenhouse', 'acme', "
        "'Old Role', 'discovered', '2026-01-01', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO gaps (job_uid, requirement, status, created_at) VALUES "
        "('greenhouse:acme:9', 'Kubernetes at scale', 'open', '2026-01-01')"
    )
    conn.commit()
    conn.close()

    db = jobdb.JobDB(path)
    cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(gaps)")}
    assert "closed_reason" in cols
    g = db.gaps_for("greenhouse:acme:9")[0]
    assert g["requirement"] == "Kubernetes at scale"
    assert g["closed_reason"] is None
    db.close_gap(g["id"], "Studied K8s, comfortable with it now.")
    assert db.gaps_for("greenhouse:acme:9")[0]["closed_reason"] == "planned"
    db.close()


def test_location_type_persisted_on_ingest(tmp_path):
    db = jobdb.JobDB(tmp_path / "t.db")
    db.upsert_job({
        "id": "1", "ats": "greenhouse", "company": "acme",
        "title": "Solutions Architect", "location": "SF, CA, USA",
        "location_type": "verify", "url": "http://x",
    })
    row = db.get(jobdb.make_job_uid("greenhouse", "acme", "1"))
    assert row["location_type"] == "verify"
    db.close()


def test_description_persisted_on_ingest(tmp_path):
    """Scans that carry a description must store it. Without this the ranker's
    content markers are inert: fit._haystack falls back to title and location."""
    db = jobdb.JobDB(tmp_path / "t.db")
    db.upsert_job({
        "id": "1", "ats": "greenhouse", "company": "acme",
        "title": "Solutions Architect", "location": "Remote", "url": "http://x",
        "description": "Own the PoV motion and competitive positioning.",
    })
    row = db.get(jobdb.make_job_uid("greenhouse", "acme", "1"))
    assert row["description"] == "Own the PoV motion and competitive positioning."
    db.close()


def test_ingest_without_a_description_stays_empty_not_null(tmp_path):
    db = jobdb.JobDB(tmp_path / "t.db")
    db.upsert_job({"id": "2", "ats": "lever", "company": "beta",
                   "title": "Platform Lead", "location": "Remote", "url": "http://y"})
    row = db.get(jobdb.make_job_uid("lever", "beta", "2"))
    assert row["description"] == ""
    db.close()

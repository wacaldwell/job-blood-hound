"""Provenance: which discovery source produced a lead.

There are two sources now (the ATS scanner and the open-jobs wide net) and
the lead inbox renders a badge from this column. It also makes the eventual
question answerable: which source actually produces interviews.
"""
import sqlite3

import jobdb


def _db(tmp_path):
    return jobdb.JobDB(tmp_path / "jobs.db")


def scanned(**over):
    j = {"id": "1", "title": "Platform Engineer", "location": "Remote",
         "url": "https://example.com/1", "company": "acme", "ats": "greenhouse"}
    j.update(over)
    return j


def test_an_openjobs_lead_records_its_own_source(tmp_path):
    db = _db(tmp_path)
    db.upsert_job(scanned(source="openjobs"))
    assert db.get("greenhouse:acme:1")["source"] == "openjobs"


def test_the_state_log_records_the_source_that_discovered_it(tmp_path):
    """The audit trail said 'scan' for every row when there was only one
    source. With two, a hardcoded note is a lie in half the rows."""
    db = _db(tmp_path)
    db.upsert_job(scanned(source="openjobs"))
    note = db.conn.execute(
        "SELECT note FROM state_log WHERE job_uid = ?",
        ("greenhouse:acme:1",)).fetchone()["note"]
    assert note == "openjobs"


def test_migration_backfills_existing_rows_as_scan(tmp_path):
    """Every row in the live DB predates open-jobs, so every one of them came
    from the scanner. That is a fact, not a guess, and NULL would lose it."""
    path = tmp_path / "jobs.db"
    db = _db(tmp_path)
    db.upsert_job(scanned())
    db.close()
    # Simulate a pre-migration database by dropping the column back out.
    raw = sqlite3.connect(path)
    raw.execute("ALTER TABLE jobs DROP COLUMN source")
    raw.commit()
    raw.close()

    reopened = jobdb.JobDB(path)
    assert reopened.get("greenhouse:acme:1")["source"] == "scan"


def test_description_survives_ingest(tmp_path):
    """open-jobs leads arrive with their JD already fetched, which is what
    keeps them off the gate's unfetchable-JD ERROR path."""
    db = _db(tmp_path)
    db.upsert_job(scanned(source="openjobs", description="Terraform and AWS."))
    assert db.get("greenhouse:acme:1")["description"] == "Terraform and AWS."


def test_company_display_is_stored_for_a_human_to_read(tmp_path):
    """`company` is a key and a URL segment, so it stays exactly as the board
    publishes it. The readable name lives in its own column, where a bad guess
    can only ever cost an ugly card."""
    db = _db(tmp_path)
    db.upsert_job(scanned(company="magnitudesoftware.wd1.myworkdayjobs.com",
                          company_display="insightsoftware", source="openjobs"))
    row = db.get("greenhouse:magnitudesoftware.wd1.myworkdayjobs.com:1")
    assert row["company"] == "magnitudesoftware.wd1.myworkdayjobs.com"
    assert row["company_display"] == "insightsoftware"


def test_company_display_defaults_to_the_slug_when_absent(tmp_path):
    """Nothing downstream should have to handle a NULL here."""
    db = _db(tmp_path)
    db.upsert_job(scanned())
    assert db.get("greenhouse:acme:1")["company_display"] == "acme"


# -- every ingestion path names itself ---------------------------------------

def test_an_unspecified_source_is_recorded_as_unknown_not_as_scan(tmp_path):
    """Defaulting to 'scan' asserts something the caller never said.

    Codex caught this on PR #105: `cmd_fetch` and the lead inbox's
    `job_ingest` both omit `source`, so the badge and any
    source-to-interview analysis would have attributed every hand-fetched and
    inbox-submitted lead to the scanner. On the live DB that is 26 of 571 rows,
    and the 14 hand-fetched ones are the highest-signal rows in the pipeline:
    the 2026-08-19 audit found NONE of the interview loops came from the
    scanner.

    A future caller that forgets is now visibly unknown rather than quietly
    wrong.
    """
    db = _db(tmp_path)
    db.upsert_job(scanned())
    assert db.get("greenhouse:acme:1")["source"] == "unknown"


def test_the_scanner_names_itself(tmp_path):
    db = _db(tmp_path)
    db.upsert_job(scanned(source="scan"))
    assert db.get("greenhouse:acme:1")["source"] == "scan"


def test_cmd_fetch_records_a_manual_fetch(tmp_path, monkeypatch):
    import job_cli, job_fetch
    monkeypatch.setattr(job_fetch, "resolve_url", lambda url: {
        "ats": "greenhouse", "company": "acme", "ext_id": "77",
        "title": "Platform Engineer", "location": "Remote",
        "url": "https://example.com/77", "posted_at": "2026-08-01",
        "date_source": "greenhouse:updated_at~", "description": "AWS."})
    db = _db(tmp_path)
    job_cli.cmd_fetch(db, type("A", (), {"url": "https://example.com/77"})())
    assert db.get("greenhouse:acme:77")["source"] == "fetch"


def test_the_inbox_ingest_names_itself(tmp_path):
    """The lead-inbox ingest path is unattended, so a lead it produced must never
    be mistaken for one the scanner chose."""
    import job_ingest
    assert job_ingest.SOURCE == "mission-control"


# -- recovering provenance for rows that predate the column ------------------

def test_migration_recovers_a_manual_fetch_from_the_audit_trail(tmp_path):
    """state_log already recorded how each of these arrived, so the backfill
    reads the evidence instead of assuming."""
    import sqlite3
    path = tmp_path / "jobs.db"
    db = _db(tmp_path)
    db.upsert_job(scanned(id="1", source="scan"))
    db.upsert_job(scanned(id="2", source="scan"))
    db.upsert_job(scanned(id="3", source="scan"))
    db.set_state("greenhouse:acme:2", "queued", note="fetched by url")
    db.set_state("greenhouse:acme:3", "queued", note="mission-control ingest")
    db.close()

    raw = sqlite3.connect(path)
    raw.execute("ALTER TABLE jobs DROP COLUMN source")
    raw.commit()
    raw.close()

    reopened = jobdb.JobDB(path)
    assert reopened.get("greenhouse:acme:1")["source"] == "scan"
    assert reopened.get("greenhouse:acme:2")["source"] == "fetch"
    assert reopened.get("greenhouse:acme:3")["source"] == "mission-control"


def test_the_backfill_does_not_re_run_and_relabel_later_rows(tmp_path):
    """The read_at migration comment warns exactly about this: a backfill in
    the body of _migrate fires on every open. This one must fire once."""
    path = tmp_path / "jobs.db"
    db = _db(tmp_path)
    db.upsert_job(scanned(source="openjobs"))
    db.set_state("greenhouse:acme:1", "queued", note="fetched by url")
    db.close()
    for _ in range(3):
        again = jobdb.JobDB(path)
        assert again.get("greenhouse:acme:1")["source"] == "openjobs"
        again.close()

"""Integration tests: the last-activity clock read from state_log."""

import jobdb


def make_job(db, slug_id, state="discovered"):
    """Insert a job and return its uid."""
    db.upsert_job({"ats": "greenhouse", "company": "acme", "id": slug_id,
                   "title": "Platform Engineer", "location": "Remote",
                   "url": "https://example.test/1", "posted_at": "",
                   "date_source": "", "source": "scan"})
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
    # lead could quiet its own alarm without the human doing anything.
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


def test_votes_and_notes_do_count_as_activity(tmp_path, monkeypatch):
    # Both are judgments about the lead, so both mean the human acted.
    #
    # now_iso() has second-level granularity (see jobdb.now_iso), and this
    # test runs in well under a second, so without pinning the clock the
    # vote/note row would land in the same second as "before" and a strict
    # > comparison below would fail even though the row correctly counts.
    # Follow test_gate_override_freshness.py's pattern: pin now_iso to two
    # distinct fixed values rather than sleeping past a second boundary.
    db = jobdb.JobDB(str(tmp_path / "t.db"))
    # Patch BEFORE any row is written, including the discovery scan row from
    # make_job()'s upsert_job call: that row would otherwise get the real
    # wall-clock timestamp, which sorts after any past fake date used below
    # and would make MAX(at) pick it regardless of the patch.
    monkeypatch.setattr(jobdb, "now_iso", lambda: "2026-07-14T10:00:00+00:00")
    uid_a = make_job(db, "1")
    uid_b = make_job(db, "2")
    db.set_state(uid_a, "queued")
    db.set_state(uid_b, "queued")
    before_a = db.last_activity()[uid_a]
    before_b = db.last_activity()[uid_b]
    monkeypatch.setattr(jobdb, "now_iso", lambda: "2026-07-14T10:00:01+00:00")
    db.set_vote(uid_a, "up", "")
    db.set_notes(uid_b, "left a follow up")
    assert db.last_activity()[uid_a] > before_a
    assert db.last_activity()[uid_b] > before_b
    db.close()


def test_transition_note_matching_read_stamp_text_still_counts(tmp_path, monkeypatch):
    # A real lifecycle transition can carry any caller-supplied --note text,
    # including literally "read" or "unread" (reachable via the CLI's --note
    # flag or jobapi.py's body.note). last_activity must not mistake that
    # for a read stamp: a read stamp always has from_state == to_state
    # (set_read never changes state), while a genuine transition never does
    # (set_state no-ops without writing a row when to_state == frm). So the
    # exclusion keys on both the note text AND from_state == to_state, and
    # a transition with a colliding note text must still count as activity.
    #
    # The scan row (note="scan", written by make_job's upsert_job) and the
    # queued transition below must land on DISTINCT timestamps. Otherwise,
    # under a buggy filter that wrongly excludes the "read"-noted transition,
    # out[uid] would fall back to the surviving scan row and could still
    # equal transition_row["at"] by coincidence (same second under
    # now_iso()'s second-level granularity), passing vacuously without the
    # transition itself ever having counted.
    db = jobdb.JobDB(str(tmp_path / "t.db"))
    monkeypatch.setattr(jobdb, "now_iso", lambda: "2026-07-14T10:00:00+00:00")
    uid = make_job(db, "1")
    monkeypatch.setattr(jobdb, "now_iso", lambda: "2026-07-14T10:00:01+00:00")
    db.set_state(uid, "queued", note="read")
    out = db.last_activity()
    assert uid in out
    hist = db.history(uid)
    scan_row = next(r for r in hist if r["note"] == "scan")
    transition_row = next(r for r in hist if r["to_state"] == "queued")
    assert transition_row["at"] != scan_row["at"]
    assert out[uid] == transition_row["at"]
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


def test_gate_override_counts_as_activity(tmp_path, monkeypatch):
    # A gate override is a decision the human made and wrote a reason for, so it
    # resets the idle clock. It survives the 'gate:%' exclusion only because
    # set_override's note reads "gate override: ..." with a SPACE, not a
    # colon, after "gate". That single character is load bearing across two
    # repos: rename it to "gate:override" and every override silently stops
    # counting as activity.
    db = jobdb.JobDB(str(tmp_path / "t.db"))
    monkeypatch.setattr(jobdb, "now_iso", lambda: "2026-07-14T10:00:00+00:00")
    uid = make_job(db, "1")
    db.set_state(uid, "queued")
    db.set_gate(uid, "DO_NOT_APPLY", "{}", "/tmp/report.md")
    before = db.last_activity()[uid]
    monkeypatch.setattr(jobdb, "now_iso", lambda: "2026-07-14T10:00:01+00:00")
    db.set_override(uid, "recruiter asked me to apply anyway")
    assert db.last_activity()[uid] > before
    db.close()


def test_gate_rule_ruling_counts_as_activity(tmp_path, monkeypatch):
    # Same load-bearing prefix. audit_gate_rule writes "gate-rule #N ...",
    # and the hyphen is the only thing keeping it clear of the 'gate:%'
    # exclusion. A human ruling on an UNSURE requirement is a decision about
    # the lead, so it must reset the clock.
    db = jobdb.JobDB(str(tmp_path / "t.db"))
    monkeypatch.setattr(jobdb, "now_iso", lambda: "2026-07-14T10:00:00+00:00")
    uid = make_job(db, "1")
    db.set_state(uid, "queued")
    before = db.last_activity()[uid]
    monkeypatch.setattr(jobdb, "now_iso", lambda: "2026-07-14T10:00:01+00:00")
    db.audit_gate_rule(uid, 3, True, False, "the JD means the adjacent tool")
    assert db.last_activity()[uid] > before
    db.close()

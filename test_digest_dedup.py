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

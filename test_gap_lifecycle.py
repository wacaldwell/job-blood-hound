"""jobdb-level tests for gap mutation methods.

plan_gap() and close_gap() run an UPDATE on a gap id copied by hand out of
`jh gaps` output, so a typo is entirely plausible. Both must report whether a
row actually matched instead of silently succeeding on a miss.

close_gaps_not_in() is the reconciliation primitive gate.py uses to keep the
gaps table a pure function of the current hard-NONE set: it must close only,
never reopen, and never touch another job's gaps.
"""
import jobdb


def _db(tmp_path):
    db = jobdb.JobDB(tmp_path / "t.db")
    db.upsert_job({"ats": "greenhouse", "company": "acme", "id": "1",
                   "title": "Engineer", "location": "Remote"})
    return db, db.get(jobdb.make_job_uid("greenhouse", "acme", "1"))


def test_plan_gap_returns_rowcount_on_hit(tmp_path):
    db, row = _db(tmp_path)
    gid = db.add_gap(row["uid"], "Kubernetes at scale")
    n = db.plan_gap(gid, "Study K8s", 10, "2026-08-01")
    assert n == 1
    g = db.gaps_for(row["uid"])[0]
    assert g["plan"] == "Study K8s"
    assert g["hours_estimate"] == 10
    assert g["deadline"] == "2026-08-01"


def test_plan_gap_returns_zero_on_a_bad_id(tmp_path):
    db, row = _db(tmp_path)
    n = db.plan_gap(9999, "Study K8s", 10, "2026-08-01")
    assert n == 0


def test_close_gap_returns_rowcount_on_hit(tmp_path):
    db, row = _db(tmp_path)
    gid = db.add_gap(row["uid"], "Kubernetes at scale")
    n = db.close_gap(gid, "Studied K8s, comfortable with it now.")
    assert n == 1
    assert db.gaps_for(row["uid"])[0]["status"] == "closed"


def test_close_gap_returns_zero_on_a_bad_id(tmp_path):
    db, row = _db(tmp_path)
    n = db.close_gap(9999, "Studied K8s, comfortable with it now.")
    assert n == 0


def test_close_gaps_not_in_closes_stale_open_gaps_only(tmp_path):
    db, row = _db(tmp_path)
    keep_id = db.add_gap(row["uid"], "keep this one")
    stale_id = db.add_gap(row["uid"], "reclassified to soft")
    n = db.close_gaps_not_in(row["uid"], {"keep this one"})
    assert n == 1
    gaps = {g["id"]: g["status"] for g in db.gaps_for(row["uid"])}
    assert gaps[keep_id] == "open"
    assert gaps[stale_id] == "closed"


def test_close_gaps_not_in_never_reopens_a_closed_gap(tmp_path):
    db, row = _db(tmp_path)
    gid = db.add_gap(row["uid"], "still hard")
    db.close_gap(gid, "Studied this, comfortable with it now.")
    n = db.close_gaps_not_in(row["uid"], set())
    assert n == 0, "an already-closed gap must not be re-touched or counted"
    assert db.gaps_for(row["uid"])[0]["status"] == "closed"


def test_close_gap_records_planned_reason(tmp_path):
    db, row = _db(tmp_path)
    gid = db.add_gap(row["uid"], "Kubernetes at scale")
    db.close_gap(gid, "Studied K8s, comfortable with it now.")
    g = db.gaps_for(row["uid"])[0]
    assert g["status"] == "closed"
    assert g["closed_reason"] == "planned"


def test_close_gaps_not_in_records_reclassified_reason(tmp_path):
    db, row = _db(tmp_path)
    db.add_gap(row["uid"], "reclassified to soft")
    db.close_gaps_not_in(row["uid"], set())
    g = db.gaps_for(row["uid"])[0]
    assert g["status"] == "closed"
    assert g["closed_reason"] == "reclassified"


def test_reopen_gap_reopens_a_system_closed_gap(tmp_path):
    db, row = _db(tmp_path)
    gid = db.add_gap(row["uid"], "still hard")
    db.close_gaps_not_in(row["uid"], set())  # system close: closed_reason='reclassified'
    n = db.reopen_gap(gid)
    assert n == 1
    g = db.gaps_for(row["uid"])[0]
    assert g["status"] == "open"
    assert g["closed_at"] is None
    assert g["closed_reason"] is None


def test_reopen_gap_returns_zero_on_a_bad_id(tmp_path):
    db, row = _db(tmp_path)
    n = db.reopen_gap(9999)
    assert n == 0


def test_gap_for_requirement_finds_the_most_recent_row(tmp_path):
    db, row = _db(tmp_path)
    gid = db.add_gap(row["uid"], "Kubernetes at scale")
    g = db.gap_for_requirement(row["uid"], "Kubernetes at scale")
    assert g["id"] == gid
    assert db.gap_for_requirement(row["uid"], "no such requirement") is None


def test_close_gaps_not_in_touches_only_this_job(tmp_path):
    db = jobdb.JobDB(tmp_path / "t.db")
    db.upsert_job({"ats": "greenhouse", "company": "acme", "id": "1",
                   "title": "Engineer", "location": "Remote"})
    db.upsert_job({"ats": "greenhouse", "company": "acme", "id": "2",
                   "title": "Engineer II", "location": "Remote"})
    uid1 = jobdb.make_job_uid("greenhouse", "acme", "1")
    uid2 = jobdb.make_job_uid("greenhouse", "acme", "2")
    db.add_gap(uid1, "some gap")
    db.add_gap(uid2, "some gap")
    db.close_gaps_not_in(uid1, set())
    assert db.gaps_for(uid1)[0]["status"] == "closed"
    assert db.gaps_for(uid2)[0]["status"] == "open"

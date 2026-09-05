"""backfill.py must repair rows without disturbing anything else."""
from pathlib import Path

import backfill
import fit
import jobdb


PROFILE = fit.load_profile(Path(__file__).resolve().parent / "profile.example.yaml")


def _db(tmp_path):
    return jobdb.JobDB(tmp_path / "t.db")


def _add(db, ext, title, company="acme", ats="greenhouse",
         location="Remote", description=""):
    db.upsert_job({"id": ext, "ats": ats, "company": company, "title": title,
                   "location": location, "url": "http://x",
                   "description": description})
    return jobdb.make_job_uid(ats, company, ext)


def test_plan_reports_a_stale_score_without_writing(tmp_path):
    db = _db(tmp_path)
    uid = _add(db, "1", "Manager, DevOps Engineering")
    db.conn.execute("UPDATE jobs SET fit_score = 50, fit_reasons = 'remote' "
                    "WHERE uid = ?", (uid,))
    db.conn.commit()

    changes = backfill.plan(db, PROFILE, {})
    assert len(changes) == 1
    assert changes[0]["old_score"] == 50
    assert changes[0]["new_score"] == 90
    # plan() is read-only.
    assert db.get(uid)["fit_score"] == 50
    db.close()


def test_apply_writes_the_score(tmp_path):
    db = _db(tmp_path)
    uid = _add(db, "1", "Manager, DevOps Engineering")
    db.conn.execute("UPDATE jobs SET fit_score = 50 WHERE uid = ?", (uid,))
    db.conn.commit()

    backfill.apply(db, backfill.plan(db, PROFILE, {}))
    assert db.get(uid)["fit_score"] == 90
    db.close()


def test_apply_preserves_updated_at(tmp_path):
    """build_history orders by updated_at DESC and takes 20. Bumping every row
    would flood the decision corpus with whatever the backfill touched last."""
    db = _db(tmp_path)
    uid = _add(db, "1", "Manager, DevOps Engineering")
    db.conn.execute("UPDATE jobs SET fit_score = 50, updated_at = ? "
                    "WHERE uid = ?", ("2020-01-01T00:00:00+00:00", uid))
    db.conn.commit()

    backfill.apply(db, backfill.plan(db, PROFILE, {}))
    assert db.get(uid)["updated_at"] == "2020-01-01T00:00:00+00:00"
    db.close()


def test_apply_never_touches_state_or_gate_columns(tmp_path):
    db = _db(tmp_path)
    uid = _add(db, "1", "Manager, DevOps Engineering")
    db.set_state(uid, "skipped")
    db.set_gate(uid, "DO_NOT_APPLY", "{}", "/tmp/r.md", model="m")
    before = dict(db.get(uid))

    backfill.apply(db, backfill.plan(db, PROFILE, {}))
    after = dict(db.get(uid))
    for col in ("state", "gate_decision", "gate_json", "gate_report_path",
                "gate_at", "applied_at", "discovered_at"):
        assert after[col] == before[col], col
    db.close()


def test_skipped_rows_are_rescored(tmp_path):
    """The whole point: refine_pipeline excludes skipped rows, so a lead wrongly
    skipped by a scoring bug can never climb back out on its own."""
    db = _db(tmp_path)
    uid = _add(db, "1", "Manager, Engineering (DevOps/SRE)", location="Boston")
    db.set_state(uid, "skipped")
    db.conn.execute("UPDATE jobs SET fit_score = 10 WHERE uid = ?", (uid,))
    db.conn.commit()

    backfill.apply(db, backfill.plan(db, PROFILE, {}))
    assert db.get(uid)["fit_score"] == 50
    db.close()


def test_a_fetched_description_changes_the_score(tmp_path):
    db = _db(tmp_path)
    uid = _add(db, "1", "Senior Solutions Architect", location="Remote - USA")
    backfill.apply(db, backfill.plan(db, PROFILE, {}))
    assert db.get(uid)["fit_score"] == 90  # title + remote, nothing to contradict

    jd = ("Own the PoV motion, author competitive positioning, and run "
          "presales deep dives with account executives.")
    backfill.apply(db, backfill.plan(db, PROFILE, {uid: jd}))
    row = db.get(uid)
    assert row["description"] == jd
    assert row["fit_score"] < 50
    assert "sales-role" in row["fit_reasons"]
    db.close()


def test_fetch_missing_skips_rows_that_already_have_one(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _add(db, "1", "Platform Lead", description="already here")
    called = []
    monkeypatch.setattr(backfill, "_greenhouse_bulk",
                        lambda c: called.append(c) or {})
    found, failed = backfill.fetch_missing(db, [dict(r) for r in db.list()],
                                           verbose=False)
    assert called == [] and found == {} and failed == []
    db.close()


def test_fetch_missing_records_an_unreachable_row_without_blanking_it(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _add(db, "1", "Platform Lead")

    def boom(company):
        raise RuntimeError("board 404")

    monkeypatch.setattr(backfill, "_greenhouse_bulk", boom)
    monkeypatch.setattr(backfill.time, "sleep", lambda s: None)
    found, failed = backfill.fetch_missing(db, [dict(r) for r in db.list()],
                                           verbose=False)
    assert found == {}
    assert len(failed) == 1 and "board 404" in failed[0][1]
    db.close()


def test_backfill_is_idempotent(tmp_path):
    db = _db(tmp_path)
    _add(db, "1", "Manager, DevOps Engineering")
    backfill.apply(db, backfill.plan(db, PROFILE, {}))
    assert backfill.plan(db, PROFILE, {}) == []
    db.close()


# --- politeness on public endpoints (CLAUDE.md hard rule) -------------------

def test_uses_the_projects_shared_politeness_delay():
    """Not a local constant: job_monitor.SLEEP_BETWEEN_CALLS is the deliberate
    interval the whole project uses against public ATS endpoints."""
    assert not hasattr(backfill, "FETCH_DELAY")


def test_sleeps_even_when_a_board_fetch_fails(tmp_path, monkeypatch):
    """A failing board is exactly when a tight loop would hammer someone."""
    import job_monitor
    db = _db(tmp_path)
    _add(db, "1", "Platform Lead", company="acme")
    _add(db, "2", "Cloud Lead", company="beta")
    slept = []
    monkeypatch.setattr(backfill.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(backfill, "_greenhouse_bulk",
                        lambda c: (_ for _ in ()).throw(RuntimeError("404")))

    found, failed = backfill.fetch_missing(db, [dict(r) for r in db.list()],
                                           verbose=False)
    assert len(failed) == 2
    assert slept == [job_monitor.SLEEP_BETWEEN_CALLS] * 2, slept
    db.close()


def test_sleeps_even_when_a_per_row_fetch_fails(tmp_path, monkeypatch):
    import job_monitor
    db = _db(tmp_path)
    _add(db, "1", "Platform Lead", company="acme", ats="lever")
    slept = []
    monkeypatch.setattr(backfill.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(backfill.job_generate, "fetch_description",
                        lambda r: (_ for _ in ()).throw(RuntimeError("boom")))

    found, failed = backfill.fetch_missing(db, [dict(r) for r in db.list()],
                                           verbose=False)
    assert len(failed) == 1
    assert slept == [job_monitor.SLEEP_BETWEEN_CALLS], slept
    db.close()


def test_a_dry_run_leaves_every_job_row_untouched(tmp_path):
    """The precise promise: plan() changes no job data. Opening the DB still
    applies the standard additive migration, as every job-hound command does."""
    db = _db(tmp_path)
    uid = _add(db, "1", "Manager, DevOps Engineering")
    db.conn.execute("UPDATE jobs SET fit_score = 50 WHERE uid = ?", (uid,))
    db.conn.commit()
    before = dict(db.get(uid))

    backfill.plan(db, PROFILE, {uid: "a freshly fetched description"})
    assert dict(db.get(uid)) == before
    db.close()


# --- the ledger is a stored signal now, so every writer of it must apply it --
#
# backfill rewrites fit_reasons for EVERY row, closed and skipped included, and
# it called fit.score() with no ledger. That stripped the `ledger:` token that
# fit.rank_key and fit.sort_key now read, so a repair pass silently promoted
# every forbidden lead back above the clean ones until the next refine.

LEDGER = [{"claim": "data catalog", "match": ["data catalog"]}]


def _forbidden(db):
    db.upsert_job({"id": "1", "ats": "greenhouse", "company": "acme",
                   "title": "Senior Platform Engineer", "location": "Remote",
                   "url": "http://x"})
    uid = jobdb.make_job_uid("greenhouse", "acme", "1")
    db.conn.execute("UPDATE jobs SET description = ? WHERE uid = ?",
                    ("You will own our data catalog.", uid))
    db.conn.commit()
    return uid


def test_backfill_records_the_ledger_hit(tmp_path):
    db = jobdb.JobDB(tmp_path / "t.db")
    uid = _forbidden(db)
    backfill.apply(db, backfill.plan(db, PROFILE, {}, do_not_claim=LEDGER))
    row = db.get(uid)
    assert "ledger:data catalog" in row["fit_reasons"]
    assert row["fit_score"] <= fit.LEDGER_CAP
    assert fit.ledger_demoted(dict(row))
    db.close()


def test_backfill_without_a_ledger_is_unchanged(tmp_path):
    """The argument is optional, so every existing caller keeps its behaviour."""
    db = jobdb.JobDB(tmp_path / "t.db")
    uid = _forbidden(db)
    backfill.apply(db, backfill.plan(db, PROFILE, {}))
    assert "ledger:" not in (db.get(uid)["fit_reasons"] or "")
    db.close()

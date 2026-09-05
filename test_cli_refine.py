import argparse
from datetime import datetime, timedelta, timezone

import jobdb
import job_cli
import fit


def _seed(db, ext, title):
    db.upsert_job({"id": ext, "ats": "greenhouse", "company": "acme",
                   "title": title, "location": "Remote", "url": "http://x"})


def test_refine_scores_all_and_verdicts_top_n(tmp_path, monkeypatch):
    db = jobdb.JobDB(tmp_path / "t.db")
    _seed(db, "1", "Staff Solutions Architect")
    _seed(db, "2", "Senior Software Development Engineer")
    _seed(db, "3", "Principal Architect")

    calls = []

    def fake_verdict(job, master, history, api_key, **kw):
        calls.append(job["title"])
        return {"llm_fit_score": 85, "llm_rationale": "ok", "llm_coding_bar": "light"}

    monkeypatch.setattr(fit, "verdict", fake_verdict)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    args = argparse.Namespace(top=2, no_llm=False, digest=False,
                              profile=None, master=None, config=None,
                              max_age=48, all=True)
    job_cli.cmd_refine(db, args)

    # Every active lead got a deterministic score.
    assert all(db.get(jobdb.make_job_uid("greenhouse", "acme", e))["fit_score"]
               is not None for e in ("1", "2", "3"))
    # Only the top 2 by deterministic score got an LLM verdict.
    assert len(calls) == 2
    db.close()


def test_refine_no_llm_skips_verdict(tmp_path, monkeypatch):
    db = jobdb.JobDB(tmp_path / "t.db")
    _seed(db, "1", "Solutions Architect")

    def boom(*a, **k):
        raise AssertionError("verdict must not be called with --no-llm")

    monkeypatch.setattr(fit, "verdict", boom)
    args = argparse.Namespace(top=10, no_llm=True, digest=False,
                              profile=None, master=None, config=None,
                              max_age=48, all=True)
    job_cli.cmd_refine(db, args)
    assert db.get(jobdb.make_job_uid("greenhouse", "acme", "1"))["fit_score"] is not None
    db.close()


def test_refine_defaults_to_three_optional_llm_verdicts():
    args = job_cli.build_parser().parse_args(["refine"])
    assert args.top == job_cli.DEFAULT_LLM_TOP == 3
    assert args.no_llm is False


def test_shared_refine_pipeline_defaults_to_three():
    import inspect
    assert (inspect.signature(job_cli.refine_pipeline)
            .parameters["top"].default) == job_cli.DEFAULT_LLM_TOP


def test_daily_digest_is_deterministic_and_free():
    daily = (job_cli.HERE / "bin" / "daily.sh").read_text()
    assert 'job_cli.py refine --no-llm --top 0 --digest' in daily
    assert 'job_cli.py refine --digest' not in daily


def test_refine_freshness_filter_hides_stale_lead(tmp_path, capsys):
    db = jobdb.JobDB(tmp_path / "t.db")
    _seed(db, "1", "Fresh Solutions Architect")
    _seed(db, "2", "Stale Solutions Architect")
    fresh_uid = jobdb.make_job_uid("greenhouse", "acme", "1")
    stale_uid = jobdb.make_job_uid("greenhouse", "acme", "2")
    db.set_fields(fresh_uid, posted_at="2099-01-01T00:00:00+00:00",
                  date_source="greenhouse:first_published")
    db.set_fields(stale_uid, posted_at="2000-01-01T00:00:00+00:00",
                  date_source="greenhouse:first_published")

    args = argparse.Namespace(top=10, no_llm=True, digest=False,
                              profile=None, master=None, config=None,
                              max_age=48, all=False)
    job_cli.cmd_refine(db, args)
    out = capsys.readouterr().out
    assert "Fresh Solutions Architect" in out
    assert "Stale Solutions Architect" not in out
    db.close()


def test_refine_trailer_reports_what_posting_age_hid(tmp_path, capsys):
    """The trailer cmd_refine prints on every cron run, and it was unasserted.

    It is the only thing that tells the human a lead existed and was withheld, so
    silence and "hid nothing" have to stay distinguishable. Driving the whole
    of cmd_refine rather than refine_pipeline on purpose: the count comes back
    from the pipeline but the sentence is built here, and the sentence is what
    ships to the terminal.
    """
    now = datetime.now(timezone.utc)
    db = jobdb.JobDB(tmp_path / "t.db")
    _seed(db, "1", "Fresh Solutions Architect")
    _seed(db, "2", "Old Solutions Architect")
    db.set_fields(jobdb.make_job_uid("greenhouse", "acme", "1"),
                  posted_at=(now - timedelta(hours=6)).isoformat(),
                  date_source="greenhouse:first_published")
    db.set_fields(jobdb.make_job_uid("greenhouse", "acme", "2"),
                  posted_at=(now - timedelta(days=9)).isoformat(),
                  date_source="greenhouse:first_published")

    job_cli.cmd_refine(db, argparse.Namespace(
        top=10, no_llm=True, digest=False, profile=None, master=None,
        config=None, max_age=48, all=False))
    out = capsys.readouterr().out
    assert "Fresh Solutions Architect" in out
    assert "Old Solutions Architect" not in out
    assert ("(1 lead(s) hidden by posting age, older than 2d; "
            "--all to include)") in out
    db.close()


def test_refine_trailer_is_silent_when_nothing_was_hidden(tmp_path, capsys):
    """A zero count prints nothing at all, rather than "0 lead(s) hidden"."""
    db = jobdb.JobDB(tmp_path / "t.db")
    _seed(db, "1", "Fresh Solutions Architect")
    db.set_fields(jobdb.make_job_uid("greenhouse", "acme", "1"),
                  posted_at=(datetime.now(timezone.utc)
                             - timedelta(hours=6)).isoformat(),
                  date_source="greenhouse:first_published")

    job_cli.cmd_refine(db, argparse.Namespace(
        top=10, no_llm=True, digest=False, profile=None, master=None,
        config=None, max_age=48, all=False))
    out = capsys.readouterr().out
    assert "Fresh Solutions Architect" in out
    assert "hidden by posting age" not in out
    db.close()


def _drafted_24_days_ago(db, ext="1", title="Site Reliability Engineer, Team Lead"):
    """Seed one lead that reached `drafted` and has sat there 24 days.

    Reproduces the real Omnicell history: an old posting, a committed state,
    and every audit row backdated so the idle clock reads 24 days.
    """
    db.upsert_job({"ats": "greenhouse", "company": "omnicell", "id": ext,
                   "title": title,
                   "location": "Remote", "url": "https://example.test/1",
                   "posted_at": "2000-01-01T00:00:00+00:00",
                   "date_source": "greenhouse:first_published"})
    uid = jobdb.make_job_uid("greenhouse", "omnicell", ext)
    db.set_state(uid, "queued")
    db.set_state(uid, "drafted")
    db.conn.execute(
        "UPDATE state_log SET at = datetime('now', '-24 days') WHERE job_uid = ?",
        (uid,))
    db.conn.commit()
    return uid


def test_stale_committed_leads_reach_the_digest(tmp_path):
    """The Omnicell regression.

    refine_pipeline filters to `discovered` only and applies a posting-age
    filter before building the digest. Both would swallow a committed lead
    with an old posting, which is exactly what let a drafted package sit 24
    days unsent. The stale set must be computed upstream of both.
    """
    db = jobdb.JobDB(tmp_path / "t.db")
    uid = _drafted_24_days_ago(db)

    r = job_cli.refine_pipeline(db, profile=fit.load_profile(None),
                                master={}, no_llm=True)
    stale_uids = [j["uid"] for j in r["stale"]]
    assert uid in stale_uids
    assert "Needs attention" in r["digest"]
    db.close()


def test_stale_entries_always_carry_a_nonempty_idle_label(tmp_path):
    """fit._stale_digest_line renders idle_label with no fallback, so a
    missing or empty key would print a blank token between two separators.
    refine_pipeline is the sole producer of that list."""
    db = jobdb.JobDB(tmp_path / "t.db")
    _drafted_24_days_ago(db)

    r = job_cli.refine_pipeline(db, profile=fit.load_profile(None),
                                master={}, no_llm=True)
    assert r["stale"]
    for j in r["stale"]:
        assert j.get("idle_label")
    assert "idle 24d" in r["digest"]
    db.close()


def test_refine_result_has_stale_key_on_an_empty_pipeline(tmp_path):
    """Callers read r["stale"] unconditionally, so the early return must
    carry the key too."""
    db = jobdb.JobDB(tmp_path / "t.db")
    r = job_cli.refine_pipeline(db, profile=fit.load_profile(None),
                                master={}, no_llm=True)
    assert r["active"] == 0
    assert r["stale"] == []
    db.close()


def _stale_committed(db, ext, company):
    """A committed lead idle long enough to reach the Needs attention section."""
    db.upsert_job({"id": ext, "ats": "greenhouse", "company": company,
                   "title": "Site Reliability Engineering Manager",
                   "location": "Remote",
                   "url": f"https://example.test/a-fairly-long-posting-url/{ext}"})
    uid = jobdb.make_job_uid("greenhouse", company, ext)
    db.set_state(uid, "queued")
    db.conn.execute(
        "UPDATE state_log SET at = datetime('now', '-30 days') WHERE job_uid = ?",
        (uid,))
    db.conn.commit()
    return uid


def test_cmd_refine_never_stamps_a_lead_the_digest_truncated_away(tmp_path,
                                                                  monkeypatch):
    """The compounding harm the cap and the deliver limit exist to stop.

    Discord truncates at DISCORD_LIMIT silently, so a New lead pushed past
    the cut is never read. mark_digested would still stamp it, and it would
    be demoted to the collapsed Still open recap forever, having never had
    its one announcement.
    """
    import notify
    db = jobdb.JobDB(tmp_path / "t.db")
    for i in range(40):
        _stale_committed(db, f"s{i}", f"stale-company-number-{i}")
    new_uid = jobdb.make_job_uid("greenhouse", "acme", "new1")
    _seed(db, "new1", "Principal Cloud Architect")

    posted = {}
    monkeypatch.setattr(notify, "post_discord",
                        lambda hook, text: posted.setdefault("text", text) or True)
    job_cli.cmd_refine(db, argparse.Namespace(
        top=10, no_llm=True, digest=True, profile=None, master=None,
        config=None, max_age=48, all=True))

    body = posted["text"][:notify.DISCORD_LIMIT]
    # Guard: the cap must hold the Needs attention section down enough that
    # the New lead actually survives, which is the point of the cap.
    assert "Principal Cloud Architect" in body
    assert db.get(new_uid)["digested_at"] is not None
    # And the section is bounded rather than printing all 40.
    assert "more)" in posted["text"]
    db.close()


def test_cmd_refine_does_not_stamp_beyond_the_delivered_body(tmp_path,
                                                             monkeypatch):
    """Whatever survives the cut is stamped; nothing past it is."""
    import notify
    db = jobdb.JobDB(tmp_path / "t.db")
    for i in range(30):
        _seed(db, f"n{i}", f"Staff Site Reliability Engineer {i}")

    posted = {}
    monkeypatch.setattr(notify, "post_discord",
                        lambda hook, text: posted.setdefault("text", text) or True)
    # Squeeze the transport so the New section cannot fit whole.
    monkeypatch.setattr(notify, "DISCORD_LIMIT", 260)
    job_cli.cmd_refine(db, argparse.Namespace(
        top=10, no_llm=True, digest=True, profile=None, master=None,
        config=None, max_age=48, all=True))

    body = posted["text"][:260]
    stamped = {r["uid"] for r in db.list() if r["digested_at"]}
    assert stamped                       # guard: something was delivered
    assert len(stamped) < 12             # guard: not the whole new_limit batch
    for r in db.list():
        if r["uid"] in stamped:
            assert r["title"] in body
    db.close()


# --- the ledger survives a cached verdict, and a malformed file ------------

_LEDGER_MASTER = {"capabilities": [],
                  "do_not_claim": [{"claim": "data catalog",
                                    "match": ["data catalog"]}]}


def test_a_cached_verdict_does_not_outrank_the_ledger(tmp_path):
    """A forbidden lead with a stale LLM verdict must not head the digest.

    refine_pipeline deliberately does not recompute a verdict a lead already
    has, so capping `fit_score` alone left the cached 95 both displayed and
    sorted into the vetted tier above every clean lead.
    """
    db = jobdb.JobDB(tmp_path / "t.db")
    _seed(db, "1", "Staff Platform Engineer")
    _seed(db, "2", "Senior Cloud Engineer")
    forbidden = jobdb.make_job_uid("greenhouse", "acme", "1")
    clean = jobdb.make_job_uid("greenhouse", "acme", "2")
    db.set_fields(forbidden, description="You will own our data catalog.",
                  llm_fit_score=95, llm_rationale="looked great")

    r = job_cli.refine_pipeline(db, profile=fit.load_profile(None),
                                master=_LEDGER_MASTER, no_llm=True,
                                show_all=True)

    rows = {j["uid"]: dict(j) for j in [db.get(forbidden), db.get(clean)]}
    assert rows[forbidden]["fit_score"] <= fit.LEDGER_CAP
    assert fit.rank_key(rows[forbidden]) <= fit.LEDGER_CAP
    assert fit.sort_key(rows[clean]) > fit.sort_key(rows[forbidden])
    # the verdict itself is preserved, not overwritten: it is a record of what
    # the model said, and clearing it would re-spend the API call every run
    assert rows[forbidden]["llm_fit_score"] == 95
    db.close()


def test_a_malformed_master_does_not_take_the_digest_down(tmp_path):
    """The unattended `refine --no-llm` run must survive a wrong-shaped
    master_resume.yaml. Only the ledger demotion is lost, and that is the
    gate's error to report loudly, not the ranker's to raise."""
    db = jobdb.JobDB(tmp_path / "t.db")
    _seed(db, "1", "Senior Cloud Engineer")
    for broken in (None, [{"claim": "x"}], "capabilities",
                   {"do_not_claim": ["data catalog"]},
                   {"capabilities": ["quantum"]}):
        r = job_cli.refine_pipeline(db, profile=fit.load_profile(None),
                                    master=broken, no_llm=True, show_all=True)
        assert r["active"] == 1
    assert db.get(jobdb.make_job_uid("greenhouse", "acme", "1"))["fit_score"]
    db.close()

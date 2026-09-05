"""The staleness marker on CLI list rows."""

import argparse
from datetime import datetime, timedelta, timezone

import job_cli
import jobdb
import staleness as stl


def row(**over):
    base = {"state": "drafted", "slug": "acme__platform-engineer__ab12",
            "title": "Platform Engineer", "company": "acme",
            "location": "Remote", "posted_at": "", "date_source": "",
            "fit_score": 90, "llm_fit_score": None}
    base.update(over)
    return base


def test_idle_label_is_appended_to_the_age_line():
    out = job_cli.fmt_row(row(), idle_label="idle 24d")
    assert "IDLE 24d" in out
    # Only the word shouts; a silently dropped transform (all-lowercase)
    # must fail this test, not pass it.
    assert "idle 24d" not in out
    # Nor should the whole marker get swept into upper case: the numeral
    # and unit must match the lowercase "53d"-style unit on the freshness
    # label sharing this line, so a blanket .upper() also fails here.
    assert "24D" not in out
    # Same number of lines as without it: the marker rides the age line
    # rather than making every stale row taller.
    assert len(out.splitlines()) == len(job_cli.fmt_row(row()).splitlines())


def test_no_marker_when_not_stale():
    assert "idle" not in job_cli.fmt_row(row(), idle_label=None)


def test_marker_survives_a_row_with_no_age_data():
    out = job_cli.fmt_row(row(), idle_label="idle 9d")
    assert "age unknown" in out
    assert "IDLE 9d" in out
    assert "9D" not in out


def _committed_40_days_ago(db, ext="1"):
    """Seed one `drafted` lead with a 40-day-old posting and a 40-day clock.

    Both numbers are past the 30d default freshness window, which is the
    whole point: a lead cannot be acted on before it was posted, so idle
    days can never exceed posting age. Any dated committed lead that reaches
    30 days idle would be hidden by a default view that filtered it on age.
    """
    db.upsert_job({"ats": "greenhouse", "company": "humana", "id": ext,
                   "title": "Principal Site Reliability Engineer",
                   "location": "Remote", "url": "https://example.test/1",
                   "posted_at": "", "date_source": "greenhouse:first_published"})
    uid = jobdb.make_job_uid("greenhouse", "humana", ext)
    db.conn.execute(
        "UPDATE jobs SET posted_at = datetime('now', '-40 days') WHERE uid = ?",
        (uid,))
    db.conn.commit()
    db.set_state(uid, "queued")
    db.set_state(uid, "drafted")
    db.conn.execute(
        "UPDATE state_log SET at = datetime('now', '-40 days') WHERE job_uid = ?",
        (uid,))
    db.conn.commit()
    return uid


def _list_args(**over):
    base = {"state": None, "all": False, "limit": None,
            "max_age": job_cli.DEFAULT_MAX_AGE_HOURS}
    base.update(over)
    return argparse.Namespace(**base)


def _discovered_40_days_ago(db, ext="9"):
    """Seed one untouched `discovered` lead with a 40-day-old posting."""
    db.upsert_job({"ats": "greenhouse", "company": "acme", "id": ext,
                   "title": "Platform Engineer", "location": "Remote",
                   "url": "https://example.test/9", "posted_at": "",
                   "date_source": "greenhouse:first_published"})
    uid = jobdb.make_job_uid("greenhouse", "acme", ext)
    db.conn.execute(
        "UPDATE jobs SET posted_at = datetime('now', '-40 days') WHERE uid = ?",
        (uid,))
    db.conn.commit()
    return uid


def test_committed_lead_with_an_ancient_posting_is_listed(tmp_path, capsys):
    # The central rule: posting age triages the discovery firehose, it does
    # not hide a lead the human already decided to pursue. Matches the lead
    # inbox's passesFreshFilter.
    db = jobdb.JobDB(tmp_path / "t.db")
    _committed_40_days_ago(db)
    job_cli.cmd_list(db, _list_args())
    out = capsys.readouterr().out
    assert "Principal Site Reliability Engineer" in out
    assert "IDLE 40d" in out
    # Nothing was hidden, so no trailer may claim otherwise.
    assert "hidden" not in out
    db.close()


def test_committed_exemption_covers_leads_short_of_the_idle_threshold(tmp_path, capsys):
    # The whole committed set is exempt, not only the leads already past 7
    # days idle, so a row does not appear and disappear as the clock crosses
    # the threshold.
    db = jobdb.JobDB(tmp_path / "t.db")
    uid = _committed_40_days_ago(db)
    db.conn.execute(
        "UPDATE state_log SET at = datetime('now', '-1 days') WHERE job_uid = ?",
        (uid,))
    db.conn.commit()
    job_cli.cmd_list(db, _list_args())
    out = capsys.readouterr().out
    assert "Principal Site Reliability Engineer" in out
    assert "idle" not in out.lower()  # not stale yet, so no marker
    assert "hidden" not in out
    db.close()


def test_discovered_lead_with_an_ancient_posting_is_still_hidden(tmp_path, capsys):
    # The exemption is for committed leads only. An untouched discovery is
    # still triaged by posting age, and its trailer carries no idle clause:
    # a discovery cannot be stale, so the clause would be wallpaper.
    db = jobdb.JobDB(tmp_path / "t.db")
    _discovered_40_days_ago(db)
    job_cli.cmd_list(db, _list_args())
    out = capsys.readouterr().out
    assert "Platform Engineer" not in out
    assert "1 hidden as older than 30d" in out
    assert "idle" not in out
    db.close()


def test_discovered_lead_is_hidden_while_a_committed_one_lists(tmp_path, capsys):
    # Both rules at once, on the rows-present trailer rather than the
    # empty-list one, which has its own wording.
    db = jobdb.JobDB(tmp_path / "t.db")
    _committed_40_days_ago(db)
    _discovered_40_days_ago(db)
    job_cli.cmd_list(db, _list_args())
    out = capsys.readouterr().out
    assert "Principal Site Reliability Engineer" in out
    assert "Platform Engineer" not in out
    assert "1 older than 30d hidden" in out
    # The freshness trailer can no longer hide a stale lead, so it must not
    # claim to have hidden one.
    assert "of them idle" not in out
    db.close()


def test_limit_is_a_budget_on_discoveries_only(tmp_path, capsys):
    # --limit is the freshness rule one layer up: it bounds the discovery
    # firehose, so a committed lead passes free and never spends budget.
    # Otherwise a tight limit re-hides exactly what the exemption surfaced.
    db = jobdb.JobDB(tmp_path / "t.db")
    _committed_40_days_ago(db)
    _discovered_40_days_ago(db, ext="8")
    _discovered_40_days_ago(db, ext="9")
    job_cli.cmd_list(db, _list_args(all=True, limit=1))
    out = capsys.readouterr().out
    assert "Principal Site Reliability Engineer" in out
    assert out.count("Platform Engineer") == 1  # the whole budget, once
    db.close()


def test_all_shows_the_ancient_discovery_too(tmp_path, capsys):
    # --all still overrides both filters and hides nothing.
    db = jobdb.JobDB(tmp_path / "t.db")
    _committed_40_days_ago(db)
    _discovered_40_days_ago(db)
    job_cli.cmd_list(db, _list_args(all=True))
    out = capsys.readouterr().out
    assert "Principal Site Reliability Engineer" in out
    assert "Platform Engineer" in out
    assert "hidden" not in out
    db.close()


def test_max_age_still_bounds_discoveries(tmp_path, capsys):
    # A tighter --max-age still drops a discovery inside the 30d default.
    db = jobdb.JobDB(tmp_path / "t.db")
    uid = _discovered_40_days_ago(db)
    db.conn.execute(
        "UPDATE jobs SET posted_at = datetime('now', '-5 days') WHERE uid = ?",
        (uid,))
    db.conn.commit()
    job_cli.cmd_list(db, _list_args(max_age=48))
    out = capsys.readouterr().out
    assert "Platform Engineer" not in out
    assert "1 hidden as older than 2d" in out
    db.close()


def test_stale_lead_held_for_location_check_is_announced(tmp_path, capsys):
    # Same ordering bug, smaller case: a `verify` lead is hidden from list at
    # any idle age while the digest nags about it daily.
    db = jobdb.JobDB(tmp_path / "t.db")
    uid = _committed_40_days_ago(db)
    db.conn.execute(
        "UPDATE jobs SET posted_at = '', location_type = 'verify' WHERE uid = ?",
        (uid,))
    db.conn.commit()
    job_cli.cmd_list(db, _list_args())
    out = capsys.readouterr().out
    assert "held for location check" in out
    assert f"1 of them idle {stl.STALE_AFTER_DAYS}d or more" in out
    db.close()


def test_stale_lead_held_for_location_check_is_not_listed(tmp_path, capsys):
    # The other half of the quarantine contract, and the half nothing pinned:
    # the lead is HELD, not merely annotated. _verify_filter runs ahead of
    # _fresh_filter, so a `verify` row never reaches the committed exemption
    # that would otherwise have listed it, and the trailer is the only place
    # it appears. That is deliberate: `verify` says a field on the row may be
    # wrong, which is a different claim from "this row is old", and an
    # unverified location does not belong in the working set. The trailer is
    # what keeps the hold from being silent.
    db = jobdb.JobDB(tmp_path / "t.db")
    uid = _committed_40_days_ago(db)
    db.conn.execute(
        "UPDATE jobs SET location_type = 'verify' WHERE uid = ?", (uid,))
    db.conn.commit()
    job_cli.cmd_list(db, _list_args())
    out = capsys.readouterr().out
    assert "Principal Site Reliability Engineer" not in out
    assert "held for location check" in out
    assert f"1 of them idle {stl.STALE_AFTER_DAYS}d or more" in out
    db.close()


def test_the_quarantine_claims_a_verify_lead_before_posting_age_can(tmp_path,
                                                                    capsys):
    # Pins the filter ORDER, which the committed case above cannot: a
    # committed lead is exempt from posting age either way, so only an old
    # `discovered` lead tagged `verify` can tell the two orderings apart.
    # Quarantine first attributes it to the location check; freshness first
    # would attribute the same row to posting age and send the human looking at
    # the wrong reason.
    db = jobdb.JobDB(tmp_path / "t.db")
    uid = _discovered_40_days_ago(db)
    db.conn.execute(
        "UPDATE jobs SET location_type = 'verify' WHERE uid = ?", (uid,))
    db.conn.commit()
    job_cli.cmd_list(db, _list_args())
    out = capsys.readouterr().out
    assert "Platform Engineer" not in out
    assert "1 held for location check" in out
    assert "older than" not in out
    db.close()


def test_every_state_that_can_go_stale_survives_the_freshness_filter():
    # The cross-module invariant the trailer wording rests on. _stale_note
    # documents that _fresh_filter's hidden set can never contain a stale
    # lead, and cmd_refine's trailer says the same in prose. Both are true
    # only while _fresh_filter's exemption set is a SUPERSET of the states
    # staleness_label fires on. They read one constant today, so it holds by
    # construction, but "by construction" is what the earlier rounds of this
    # bug also looked like. So drive both sides through a real row rather
    # than comparing the two constants to each other, which would pass even
    # if one of them stopped being the thing its caller consults.
    now = datetime.now(timezone.utc)
    idle = (now - timedelta(days=365)).isoformat()
    ancient = (now - timedelta(days=400)).isoformat()
    checked = 0
    for state in jobdb.TRANSITIONS:
        if not stl.staleness_label(state, idle):
            continue
        checked += 1
        row = {"state": state, "posted_at": ancient,
               "date_source": "greenhouse:first_published"}
        kept, hidden = job_cli._fresh_filter(
            [row], job_cli.DEFAULT_MAX_AGE_HOURS, False)
        assert kept == [row], state
        assert hidden == 0, state
    # Guard: a staleness_label that stopped firing entirely would satisfy the
    # loop above vacuously.
    assert checked == len(stl.COMMITTED_STATES)


def test_visible_stale_lead_still_gets_its_row_marker(tmp_path, capsys):
    # The rows LISTED are unchanged by the fix: a stale lead inside the
    # freshness window still renders its marker inline.
    db = jobdb.JobDB(tmp_path / "t.db")
    uid = _committed_40_days_ago(db)
    db.conn.execute(
        "UPDATE jobs SET posted_at = datetime('now', '-2 days') WHERE uid = ?",
        (uid,))
    db.conn.execute(
        "UPDATE state_log SET at = datetime('now', '-9 days') WHERE job_uid = ?",
        (uid,))
    db.conn.commit()
    job_cli.cmd_list(db, _list_args())
    out = capsys.readouterr().out
    assert "Principal Site Reliability Engineer" in out
    assert "IDLE 9d" in out
    db.close()

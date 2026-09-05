"""The posting-liveness sweep.

Every test here mocks the network. Nothing in this file may issue a real HTTP
request: the endpoints under test are other people's production job boards.

The property that matters most is at the bottom: `--apply` acts on `closed`
and on nothing else. Marking a live posting skipped removes a real
opportunity from the pipeline, and no later stage would ever surface it again.
"""

import argparse
from unittest import mock

import pytest

import job_cli
import job_generate
import job_monitor
import jobdb
import liveness as lv


class Resp:
    """Minimal requests-style response."""

    def __init__(self, status_code, payload=None, bad_json=False):
        self.status_code = status_code
        self._payload = payload
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


def row(**over):
    base = {"ats": "greenhouse", "company": "zetaglobal", "ext_id": "4522",
            "description": "", "slug": "zetaglobal__principal-devops__0bc7",
            "title": "Principal DevOps Engineer"}
    base.update(over)
    return base


def responder(resp):
    return lambda url: resp


# --- classification -------------------------------------------------------

@pytest.mark.parametrize("status", [404, 410])
def test_gone_statuses_are_closed(status):
    # The live case: zetaglobal's Greenhouse posting 404s while the row still
    # holds 11,755 characters of stored JD, so it looks alive in the database.
    assert lv.check(row(), fetch=responder(Resp(status))) == lv.CLOSED


def test_two_hundred_with_real_content_is_open():
    resp = Resp(200, {"content": "<p>We are hiring a Principal DevOps Engineer</p>"})
    assert lv.check(row(), fetch=responder(resp)) == lv.OPEN


def test_greenhouse_two_hundred_with_empty_content_is_unknown():
    """A greenhouse 200 never earns a `closed`, and this is why.

    Every dead greenhouse posting observed so far answers 404. A 200 with no
    content has never been seen, so a rule that read it as `closed` would have
    no confirmed benefit and an unbounded false-`closed` risk. `closed` is
    reserved for the 404 and 410 that actually happen.
    """
    assert lv.check(row(), fetch=responder(Resp(200, {"content": ""}))) == lv.UNKNOWN


@pytest.mark.parametrize("content", [None, [], 0, {}, {"a": 1}, 17, False])
def test_greenhouse_non_string_content_is_unknown_not_closed(content):
    """`or ""` used to coalesce every one of these to "" and answer `closed`.

    `{"content": null}` is valid JSON of a shape we do not understand, and a
    shape we do not understand is exactly what must not reach `closed`.
    """
    resp = Resp(200, {"content": content})
    assert lv.check(row(), fetch=responder(resp)) == lv.UNKNOWN


def test_greenhouse_two_hundred_without_a_content_key_is_unknown():
    # Not a posting payload at all. We cannot tell, so we do not guess.
    assert lv.check(row(), fetch=responder(Resp(200, {"oops": 1}))) == lv.UNKNOWN


def test_ashby_board_missing_the_id_is_closed():
    # Ashby's endpoint is the whole board list, so a pulled posting is simply
    # absent from a perfectly healthy 200.
    r = row(ats="ashby", company="vantage", ext_id="5a29-uuid")
    resp = Resp(200, {"jobs": [{"id": "some-other-posting"}]})
    assert lv.check(r, fetch=responder(resp)) == lv.CLOSED


def test_ashby_board_containing_the_id_is_open():
    r = row(ats="ashby", company="vantage", ext_id="5a29-uuid")
    resp = Resp(200, {"jobs": [{"id": "other"}, {"id": "5a29-uuid"}]})
    assert lv.check(r, fetch=responder(resp)) == lv.OPEN


def test_ashby_response_that_is_not_a_board_is_unknown():
    r = row(ats="ashby", company="vantage", ext_id="5a29-uuid")
    assert lv.check(r, fetch=responder(Resp(200, {}))) == lv.UNKNOWN


def test_ashby_empty_board_is_unknown_not_closed():
    """The blast radius case. One empty board would close a whole company.

    "the id is not on the board" is only sound when the board is healthy AND
    populated. A paused or depublished board answers 200 with no jobs, and
    reading that as `closed` marks every ashby row for that company skipped in
    a single sweep. SmartRecruiters is already known to answer 200 with
    totalFound 0 for a slug that does not exist at all.
    """
    r = row(ats="ashby", company="vantage", ext_id="5a29-uuid")
    assert lv.check(r, fetch=responder(Resp(200, {"jobs": []}))) == lv.UNKNOWN


def test_ashby_error_envelope_is_unknown_not_closed():
    r = row(ats="ashby", company="vantage", ext_id="5a29-uuid")
    resp = Resp(200, {"errors": [{"message": "board unavailable"}], "jobs": []})
    assert lv.check(r, fetch=responder(resp)) == lv.UNKNOWN


@pytest.mark.parametrize("jobs", [[1, 2], ["a"], [None], [[{"id": "x"}]]])
def test_a_payload_that_raises_is_unknown_not_a_dead_sweep(jobs):
    """A malformed entry used to raise AttributeError out of check().

    cmd_prune catches only TransitionError, so one bad row would abort a
    ten-minute, 334-request sweep partway through. The docstring on check()
    already promised that anything unexpected is an `unknown`; now it is.
    """
    r = row(ats="ashby", company="vantage", ext_id="5a29-uuid")
    assert lv.check(r, fetch=responder(Resp(200, {"jobs": jobs}))) == lv.UNKNOWN


# --- the classifier-error signal -------------------------------------------
#
# check()'s docstring promises `unknown` for anything _payload_verdict cannot
# name, and that promise must not also make a genuine bug in the classifier
# invisible: `on_classifier_error` fires when _payload_verdict raises for a
# reason it does not already recognize, without changing the verdict.
#
# The ashby non-dict-entry shape above is a known, anticipated malformed
# payload (real ATS feeds do this), so it is handled explicitly inside
# _payload_verdict and must NOT trip the signal: a routine occurrence would
# otherwise look like a code regression every single time it happened, and
# the operator would learn to ignore the one signal meant to catch a typo.

def test_classifier_error_signal_fires_on_a_genuinely_unexpected_failure(monkeypatch):
    def boom(ats, ext, data):
        raise RuntimeError("not a payload shape anyone anticipated")
    monkeypatch.setattr(lv, "_payload_verdict", boom)
    calls = []
    verdict = lv.check(row(), fetch=responder(Resp(200, {"content": "x"})),
                        on_classifier_error=lambda: calls.append(1))
    assert verdict == lv.UNKNOWN
    assert calls == [1]


@pytest.mark.parametrize("jobs", [[1, 2], ["a"], [None], [[{"id": "x"}]]])
def test_classifier_error_signal_does_not_fire_on_the_anticipated_ashby_shape(jobs):
    r = row(ats="ashby", company="vantage", ext_id="5a29-uuid")
    calls = []
    verdict = lv.check(r, fetch=responder(Resp(200, {"jobs": jobs})),
                        on_classifier_error=lambda: calls.append(1))
    assert verdict == lv.UNKNOWN
    assert calls == []


def test_classifier_error_signal_is_silent_without_a_callback():
    # on_classifier_error is optional; every existing caller that omits it
    # must keep working exactly as before.
    def boom(ats, ext, data):
        raise RuntimeError("boom")
    with mock.patch.object(lv, "_payload_verdict", boom):
        assert lv.check(row(), fetch=responder(Resp(200, {"content": "x"}))) == lv.UNKNOWN


@pytest.mark.parametrize("status", [429, 500, 502, 503, 403, 401])
def test_server_and_throttle_statuses_are_unknown_not_closed(status):
    # A 5xx is the ATS having a bad day and a 429 is us being throttled.
    # Both describe our request, not the posting.
    assert lv.check(row(), fetch=responder(Resp(status))) == lv.UNKNOWN


@pytest.mark.parametrize("exc", [
    TimeoutError("timed out"),
    ConnectionError("connection refused"),
    ValueError("something else entirely"),
])
def test_network_failures_are_unknown_not_closed(exc):
    def boom(url):
        raise exc
    assert lv.check(row(), fetch=boom) == lv.UNKNOWN


def test_unparseable_body_is_unknown():
    assert lv.check(row(), fetch=responder(Resp(200, bad_json=True))) == lv.UNKNOWN


def test_ats_with_no_public_endpoint_is_unknown():
    # ninjaone sits on jobvite, for which no fetcher exists at all.
    r = row(ats="jobvite", company="ninjaone", ext_id="9119")
    called = []
    assert lv.check(r, fetch=lambda url: called.append(url)) == lv.UNKNOWN
    assert called == []  # no endpoint means no request, not a guessed URL


def test_vanity_workday_row_is_unknown():
    # company is a plain name, not a "{host}/{site}" cxs slug, so
    # posting_endpoint returns None and there is nothing to ask.
    r = row(ats="workday", company="Capital One", ext_id="req123")
    assert lv.check(r, fetch=responder(Resp(404))) == lv.UNKNOWN


# --- the shared endpoint helper -------------------------------------------

ENDPOINTS = [
    (row(), "https://boards-api.greenhouse.io/v1/boards/zetaglobal/jobs/4522"),
    (row(ats="lever", company="ellevation", ext_id="abc-1"),
     "https://api.lever.co/v0/postings/ellevation/abc-1"),
    (row(ats="ashby", company="kong", ext_id="abc-123"),
     "https://api.ashbyhq.com/posting-api/job-board/kong"),
    (row(ats="smartrecruiters", company="Bosch", ext_id="7431"),
     "https://api.smartrecruiters.com/v1/companies/Bosch/postings/7431"),
    (row(ats="workday", company="acme.wd1.myworkdayjobs.com/Acme",
         ext_id="/job/Atlanta-GA/Role_R1"),
     "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/Acme/job/Atlanta-GA/Role_R1"),
]


@pytest.mark.parametrize("r,expected", ENDPOINTS)
def test_posting_endpoint_matches_what_fetch_description_requests(r, expected):
    """The helper and the JD fetcher must ask the same URL, per ATS.

    This is the anti-drift test. A second copy of these URLs living in
    liveness.py would diverge from the fetchers and the two would silently
    disagree about which posting they were looking at.
    """
    assert job_generate.posting_endpoint(r) == expected
    resp = mock.Mock()
    resp.json.return_value = {"content": "x", "descriptionPlain": "x",
                              "jobs": [{"id": r["ext_id"],
                                        "descriptionPlain": "x"}],
                              "jobAd": {}, "jobPostingInfo": {}}
    with mock.patch.object(job_monitor.SESSION, "get", return_value=resp) as m:
        job_generate.fetch_description(r)
    assert m.call_args[0][0] == expected


def test_posting_endpoint_is_none_without_a_public_endpoint():
    assert job_generate.posting_endpoint(row(ats="jobvite")) is None
    assert job_generate.posting_endpoint(
        row(ats="workday", company="Capital One", ext_id="r1")) is None


# --- the CLI sweep --------------------------------------------------------

def seed(db, ext, ats="greenhouse", company="zetaglobal"):
    db.upsert_job({"ats": ats, "company": company, "id": ext,
                   "title": f"Role {ext}", "location": "Remote",
                   "url": f"https://example.test/{ext}",
                   "posted_at": "", "date_source": ""})
    return jobdb.make_job_uid(ats, company, ext)


def prune_args(**over):
    base = {"state": "discovered", "limit": None, "apply": False}
    base.update(over)
    return argparse.Namespace(**base)


@pytest.fixture
def db(tmp_path):
    d = jobdb.JobDB(tmp_path / "jobs.db")
    yield d
    d.close()


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch):
    """The sweep sleeps 1.5s between rows on purpose. Not in tests."""
    monkeypatch.setattr(job_cli.time, "sleep", lambda *_: None)


def test_apply_transitions_only_closed_rows(db, monkeypatch, capsys):
    """The safety property. An `unknown` must survive the sweep untouched.

    Marking a live posting skipped silently removes a real opportunity, so
    `unknown` (a 500 here) has to be left exactly where it was.
    """
    dead = seed(db, "dead")
    alive = seed(db, "alive")
    murky = seed(db, "murky")
    verdicts = {"dead": lv.CLOSED, "alive": lv.OPEN, "murky": lv.UNKNOWN}
    monkeypatch.setattr(lv, "check", lambda r, fetch=None, on_classifier_error=None: verdicts[r["ext_id"]])

    job_cli.cmd_prune(db, prune_args(apply=True))

    assert db.get(dead)["state"] == "skipped"
    assert db.get(alive)["state"] == "discovered"
    assert db.get(murky)["state"] == "discovered"
    out = capsys.readouterr().out
    assert "1 open, 1 closed, 1 unknown" in out
    assert "1 marked skipped" in out


def test_apply_records_a_dated_note(db, monkeypatch):
    uid = seed(db, "dead")
    monkeypatch.setattr(lv, "check", lambda r, fetch=None, on_classifier_error=None: lv.CLOSED)
    job_cli.cmd_prune(db, prune_args(apply=True))
    note = db.history(uid)[-1]["note"]
    assert "closed" in note
    from datetime import date
    assert date.today().isoformat() in note


def test_dry_run_is_the_default_and_changes_nothing(db, monkeypatch, capsys):
    uid = seed(db, "dead")
    monkeypatch.setattr(lv, "check", lambda r, fetch=None, on_classifier_error=None: lv.CLOSED)
    job_cli.cmd_prune(db, prune_args())
    assert db.get(uid)["state"] == "discovered"
    out = capsys.readouterr().out
    assert "would skip" in out
    assert "Dry run, nothing changed" in out


def test_sweep_sleeps_between_rows_but_not_before_the_first(db, monkeypatch):
    for ext in ("a", "b", "c"):
        seed(db, ext)
    monkeypatch.setattr(lv, "check", lambda r, fetch=None, on_classifier_error=None: lv.OPEN)
    naps = []
    monkeypatch.setattr(job_cli.time, "sleep", lambda s: naps.append(s))
    job_cli.cmd_prune(db, prune_args())
    assert naps == [job_monitor.SLEEP_BETWEEN_CALLS] * 2


def test_limit_bounds_the_sweep(db, monkeypatch, capsys):
    for ext in ("a", "b", "c"):
        seed(db, ext)
    monkeypatch.setattr(lv, "check", lambda r, fetch=None, on_classifier_error=None: lv.OPEN)
    job_cli.cmd_prune(db, prune_args(limit=2))
    assert "2 checked" in capsys.readouterr().out


def test_the_sweep_takes_the_oldest_rows_first(db, monkeypatch):
    """--limit must spend its budget on the leads most likely to be dead.

    db.list orders discovered_at DESC, so a bare --limit 50 checked the 50
    NEWEST leads, the ones most likely to still be open. Old postings are the
    dead ones, so the sweep re-orders before it slices.
    """
    for ext, when in (("new", "2026-07-27"), ("old", "2026-06-01"),
                      ("mid", "2026-07-01")):
        uid = seed(db, ext)
        db.conn.execute("UPDATE jobs SET discovered_at = ? WHERE uid = ?",
                        (when, uid))
    db.conn.commit()
    seen = []

    def note(r, fetch=None, on_classifier_error=None):
        seen.append(r["ext_id"])
        return lv.OPEN

    monkeypatch.setattr(lv, "check", note)
    job_cli.cmd_prune(db, prune_args(limit=2))
    assert seen == ["old", "mid"]


def test_an_illegal_transition_is_reported_not_raised(db, monkeypatch, capsys):
    # applied -> skipped is not a legal transition. The sweep reports it and
    # carries on rather than dying partway through hundreds of rows.
    uid = seed(db, "gone")
    db.set_state(uid, "queued")
    db.set_state(uid, "drafted")
    db.set_state(uid, "ready")
    db.set_state(uid, "applied")
    monkeypatch.setattr(lv, "check", lambda r, fetch=None, on_classifier_error=None: lv.CLOSED)
    job_cli.cmd_prune(db, prune_args(state="applied", apply=True))
    assert db.get(uid)["state"] == "applied"
    assert "could not skip" in capsys.readouterr().out


def test_prune_reports_a_classifier_error_in_the_summary(db, monkeypatch, capsys):
    """The prune summary must surface a classifier failure, not just counts.

    Otherwise a typo inside _payload_verdict produces a clean-looking
    "N unknown" line every day and the operator never learns anything broke.
    """
    seed(db, "weird")

    def check(r, fetch=None, on_classifier_error=None):
        on_classifier_error()
        return lv.UNKNOWN

    monkeypatch.setattr(lv, "check", check)
    job_cli.cmd_prune(db, prune_args())
    out = capsys.readouterr()
    assert "1 classifier error" in out.out
    assert "classifier error on" in out.err


def test_prune_reports_no_classifier_errors_on_a_normal_sweep(db, monkeypatch, capsys):
    seed(db, "fine")
    monkeypatch.setattr(
        lv, "check", lambda r, fetch=None, on_classifier_error=None: lv.OPEN)
    job_cli.cmd_prune(db, prune_args())
    out = capsys.readouterr()
    assert "classifier error" not in out.out
    assert out.err == ""


def test_prune_parses_from_the_command_line():
    args = job_cli.build_parser().parse_args(["prune", "--apply", "--limit", "5"])
    assert args.cmd == "prune"
    assert args.state == "discovered"  # default target: the discovered backlog
    assert args.apply is True
    assert args.limit == 5
    assert job_cli.build_parser().parse_args(["prune"]).apply is False

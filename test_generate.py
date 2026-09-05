from unittest import mock

import pytest

import job_generate
import job_monitor


def test_fetch_description_uses_stored_jd():
    # A manual entry on an unsupported ATS carries its JD in description.
    row = {"description": "STORED JD TEXT", "ats": "avature",
           "ext_id": "40062", "company": "maximus"}
    assert job_generate.fetch_description(row) == "STORED JD TEXT"


def test_fetch_description_unsupported_ats_without_stored_raises():
    row = {"description": "", "ats": "avature", "ext_id": "1", "company": "x"}
    with pytest.raises(ValueError):
        job_generate.fetch_description(row)


def test_supported_ats_refetches_and_ignores_stored_cache():
    # Re-draft must refresh from the ATS, not reuse the cached JD that generate()
    # wrote back into description. A greenhouse row with a stale stored JD should
    # return the freshly-fetched content.
    row = {"description": "STALE CACHED JD", "ats": "greenhouse",
           "ext_id": "1", "company": "acme"}
    resp = mock.Mock()
    resp.json.return_value = {"content": "<p>FRESH JD FROM ATS</p>"}
    with mock.patch.object(job_monitor.SESSION, "get", return_value=resp):
        out = job_generate.fetch_description(row)
    assert "FRESH JD FROM ATS" in out
    assert "STALE" not in out


def test_workday_fetch_description_hits_cxs_detail():
    # A scanned Workday role (slug "host/site", ext_id = externalPath) must be
    # draftable: fetch the JD from the cxs job-detail endpoint.
    row = {"description": "", "ats": "workday",
           "company": "acme.wd1.myworkdayjobs.com/Acme",
           "ext_id": "/job/Atlanta-GA/Role_R1"}
    resp = mock.Mock()
    resp.json.return_value = {"jobPostingInfo": {"jobDescription": "<p>WORKDAY JD</p>"}}
    with mock.patch.object(job_monitor.SESSION, "get", return_value=resp) as m:
        out = job_generate.fetch_description(row)
    assert "WORKDAY JD" in out
    assert "wday/cxs/acme/Acme/job/Atlanta-GA/Role_R1" in m.call_args[0][0]


def test_ashby_fetch_description_uses_public_board_list():
    # Ashby's per-posting endpoint 401s on public boards, so the JD must come
    # from the public job-board list, matched by id (not a per-posting URL).
    row = {"description": "", "ats": "ashby",
           "company": "kong", "ext_id": "abc-123"}
    resp = mock.Mock()
    resp.json.return_value = {"jobs": [
        {"id": "other", "descriptionPlain": "WRONG JD"},
        {"id": "abc-123", "descriptionPlain": "ASHBY JD TEXT"},
    ]}
    with mock.patch.object(job_monitor.SESSION, "get", return_value=resp) as m:
        out = job_generate.fetch_description(row)
    assert out == "ASHBY JD TEXT"
    # Hit the board list, not the auth-gated per-posting endpoint.
    assert m.call_args[0][0].endswith("/job-board/kong")


def test_manual_workday_row_uses_stored_jd_not_cxs():
    # A vanity/manual Workday row (plain company name, no cxs slug) must draft
    # from its stored JD, not try to build a cxs URL and fail.
    row = {"description": "STORED VANITY JD", "ats": "workday",
           "company": "Capital One", "ext_id": "req123"}
    with mock.patch.object(job_monitor.SESSION, "get") as m:
        out = job_generate.fetch_description(row)
    assert out == "STORED VANITY JD"
    m.assert_not_called()  # no cxs fetch attempted


def test_generator_prompt_matches_corrected_master():
    # The prompt must not prime the model with AI claims removed from the master
    # (no-invention guard); it should reference the accurate framing instead.
    p = job_generate.GEN_SYSTEM.lower()
    assert "autonomous agents" not in p
    assert "mcp server ecosystem" not in p
    assert "ai-accelerated devops" in p

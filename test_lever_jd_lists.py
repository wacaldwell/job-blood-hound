"""A Lever posting keeps its requirements, not just its section headings.

Lever returns the bullets in lists[].content and the heading in lists[].text.
Both JD fetchers used to append only `text`, so a 7k-char posting was stored as
the company intro plus five labels (932 chars for the Ellevation CloudOps role),
and the gate graded a company blurb with no requirements in it.
"""
import job_generate
import job_ingest

LEVER_PAYLOAD = {
    "text": "Senior Engineering Manager, CloudOps",
    "categories": {"location": "Remote"},
    "descriptionPlain": "Ellevation Education builds software for schools.",
    "lists": [
        {"text": "What You'll Bring",
         "content": "<ul><li>8+ years operating AWS in production</li>"
                    "<li>Experience leading SRE teams</li></ul>"},
        {"text": "About the Role",
         "content": "<ul><li>Own the CloudOps roadmap</li></ul>"},
    ],
}


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _patch_session(monkeypatch, module):
    monkeypatch.setattr(module.job_monitor.SESSION, "get",
                        lambda *a, **k: _FakeResp(LEVER_PAYLOAD))


def _assert_requirements_present(jd):
    # The headings alone are not a job description; the bullets are the thing
    # the gate grades against.
    assert "8+ years operating AWS in production" in jd
    assert "Experience leading SRE teams" in jd
    assert "Own the CloudOps roadmap" in jd
    assert "Ellevation Education builds software" in jd
    assert "What You'll Bring" in jd          # heading kept as a label
    assert "<li>" not in jd                   # html stripped


def test_generate_fetch_description_keeps_lever_list_content(monkeypatch):
    _patch_session(monkeypatch, job_generate)
    jd = job_generate.fetch_description(
        {"ats": "lever", "company": "ellevationeducation", "ext_id": "abc"})
    _assert_requirements_present(jd)


def test_ingest_fetch_posting_meta_keeps_lever_list_content(monkeypatch):
    _patch_session(monkeypatch, job_ingest)
    meta = job_ingest.fetch_posting_meta(
        {"ats": "lever", "company": "ellevationeducation", "ext_id": "abc"})
    _assert_requirements_present(meta["description"])
    assert meta["title"] == "Senior Engineering Manager, CloudOps"


def test_both_fetchers_agree_on_lever_text(monkeypatch):
    """job_ingest's docstring promises its endpoints mirror job_generate's, so
    the two must produce identical JD text or ingest and gate disagree."""
    _patch_session(monkeypatch, job_generate)
    a = job_generate.fetch_description(
        {"ats": "lever", "company": "ellevationeducation", "ext_id": "abc"})
    _patch_session(monkeypatch, job_ingest)
    b = job_ingest.fetch_posting_meta(
        {"ats": "lever", "company": "ellevationeducation", "ext_id": "abc"})["description"]
    assert a == b

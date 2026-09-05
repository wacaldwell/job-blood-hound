"""Greenhouse scans must carry the description, or the ranker cannot see it."""
import job_monitor


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_fetch_greenhouse_requests_content_and_strips_html(monkeypatch):
    seen = {}

    def fake_get(url, timeout=None):
        seen["url"] = url
        return _Resp({"jobs": [{
            "id": 7813713003,
            "title": "Senior Solutions Architect, Identity Threat Protection",
            "location": {"name": "Remote - USA"},
            "absolute_url": "https://abnormal.ai/careers/jobs/7813713003",
            "updated_at": "2026-07-23T16:23:31-04:00",
            "content": "<p>Own the <strong>PoV motion</strong>.</p>"
                       "<li>Author competitive positioning</li>",
        }]})

    monkeypatch.setattr(job_monitor.SESSION, "get", fake_get)
    out = job_monitor.fetch_greenhouse("abnormalsecurity")

    assert "content=true" in seen["url"], "without content=true the JD is absent"
    assert "<p>" not in out[0]["description"]
    assert "PoV motion" in out[0]["description"]
    assert "Author competitive positioning" in out[0]["description"]


def test_fetch_greenhouse_tolerates_a_missing_content_field(monkeypatch):
    def fake_get(url, timeout=None):
        return _Resp({"jobs": [{"id": 1, "title": "Platform Engineer",
                                "location": {"name": "Remote"},
                                "absolute_url": "http://x", "updated_at": ""}]})

    monkeypatch.setattr(job_monitor.SESSION, "get", fake_get)
    assert job_monitor.fetch_greenhouse("acme")[0]["description"] == ""

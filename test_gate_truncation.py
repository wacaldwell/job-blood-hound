"""A truncated model response must fail closed AND say so honestly.

The gate's output is one JSON object per requirement, each carrying a verbatim
quote from the JD, so it is much longer than the generator's output. At the old
4000 token cap, a real 11.7k-character Humana posting cut off mid-string, the
JSON would not parse, and the gate ERRORed with "the model's response could not
be parsed". That failed closed, which is correct, but the message sent the reader
hunting for a JSON bug that did not exist. The response was simply truncated.
"""
import json

import pytest

import gate
import jobdb
import llm


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_a_truncated_response_is_named_as_truncation(monkeypatch):
    """Not 'could not be parsed'. Say the cap was hit, and name the knob."""
    def fake_post(url, headers=None, json=None, timeout=None):
        return _Resp({"stop_reason": "max_tokens",
                      "content": [{"type": "text", "text": '{"requirements": [{"quo'}]})

    monkeypatch.setattr(llm.requests, "post", fake_post)

    with pytest.raises(gate.TruncatedResponse) as e:
        gate._call_anthropic("sys", "user", "k")

    msg = str(e.value).lower()
    assert "cut off" in msg or "truncat" in msg
    assert "gate_max_tokens" in msg, "the message must name the knob to turn"


def test_a_truncated_response_still_fails_closed(tmp_path, monkeypatch):
    """The headline. TruncatedResponse is a ValueError, so run_gate's existing
    handler catches it and the job is BLOCKED, never allowed through."""
    def boom(system, user, api_key):
        raise gate.TruncatedResponse("cut off at the cap")

    db = jobdb.JobDB(tmp_path / "t.db")
    db.upsert_job({"ats": "greenhouse", "company": "humana", "id": "1",
                   "title": "Infrastructure Operations Lead", "location": "Remote"})
    row = db.get(jobdb.make_job_uid("greenhouse", "humana", "1"))

    master = {
        "works_as": ["manager"],
        "capabilities": [{"claim": "AWS governance", "evidence": "Northwind, 10 accounts"}],
        "do_not_claim": [{"claim": "event correlation", "match": ["correlation"]}],
    }

    out = gate.run_gate(db, row, master, api_key="k", jd_text="a very long jd",
                        call=boom)

    assert out["decision"] == gate.ERROR
    assert db.get(row["uid"])["gate_decision"] == gate.ERROR
    # And the report tells the truth about why.
    assert "cut off" in out["report_path"].read_text().lower()


def test_the_output_cap_is_big_enough_for_a_long_jd():
    """A senior JD routinely yields 20+ requirements, each with a verbatim quote.
    4000 was not enough and produced a truncated response on a real posting.
    16000 was not enough either once a second provider was supported: kimi-k2.6
    measured 22000 output tokens for 51 requirements on a 10k-char JD, and at
    16000 the gate failed closed on a truncated response. The floor tracks the
    most verbose supported provider, so do not lower it below that measurement."""
    assert gate.GATE_MAX_TOKENS >= 32000


def test_a_complete_response_is_unaffected(monkeypatch):
    """The happy path must not regress: a normal end_turn response parses."""
    payload = {"requirements": [
        {"quote": "AWS", "topic": "aws", "hard": True, "confidence": "high",
         "verdict": "HAVE", "evidence": "Northwind", "bridge": ""}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        import json as _j
        return _Resp({"stop_reason": "end_turn",
                      "content": [{"type": "text", "text": _j.dumps(payload)}]})

    monkeypatch.setattr(llm.requests, "post", fake_post)

    raw = gate._call_anthropic("sys", "user", "k")
    assert json.loads(raw)["requirements"][0]["topic"] == "aws"

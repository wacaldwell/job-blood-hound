import gate

DNC = [
    {"claim": "data catalog architecture", "match": ["data catalog"]},
    {"claim": "hand-writing production code",
     "match": ["proficient in python", "python or go"]},
]


def _req(**kw):
    base = {"quote": "q", "topic": "t", "hard": True, "confidence": "high",
            "verdict": "HAVE", "evidence": "solid evidence", "bridge": "",
            "forced": "", "ruled_by_human": False}
    base.update(kw)
    return base


def test_have_without_evidence_is_demoted_to_none():
    out = gate.enforce([_req(verdict="HAVE", evidence="  ")], DNC)
    assert out[0]["verdict"] == "NONE"
    assert out[0]["forced"] == "no-evidence"


def test_partial_without_a_written_bridge_is_demoted_to_none():
    out = gate.enforce([_req(verdict="PARTIAL", bridge="")], DNC)
    assert out[0]["verdict"] == "NONE"
    assert out[0]["forced"] == "no-bridge"


def test_partial_with_a_bridge_survives():
    out = gate.enforce([_req(verdict="PARTIAL", bridge="Ran Nagios at CDN scale.")], DNC)
    assert out[0]["verdict"] == "PARTIAL"
    assert out[0]["forced"] == ""


def test_do_not_claim_overrules_a_confident_have():
    """The whole point: the model saying HAVE does not matter here."""
    r = _req(quote="Deep expertise in data catalog architecture",
             verdict="HAVE", evidence="I once used Glue")
    out = gate.enforce([r], DNC)
    assert out[0]["verdict"] == "NONE"
    assert out[0]["forced"] == "do-not-claim: data catalog architecture"


def test_do_not_claim_matches_python_or_go_bullet():
    """'proficient in python' is the token that fires: it is a literal, lowercased
    substring of 'Proficient in Python or Go', and match lists are checked in
    order so it is the first one found. 'python or go' also happens to be a
    substring here, but it is not the token that fires first. This is why the
    reason match is a list, so either phrasing catches the requirement."""
    r = _req(quote="Proficient in Python or Go", verdict="HAVE", evidence="x")
    out = gate.enforce([r], DNC)
    assert out[0]["verdict"] == "NONE"
    assert out[0]["forced"].startswith("do-not-claim:")


def test_enforce_never_upgrades_a_none():
    out = gate.enforce([_req(verdict="NONE", evidence="lots of evidence")], DNC)
    assert out[0]["verdict"] == "NONE"


def test_enforce_does_not_mutate_its_input():
    reqs = [_req(verdict="HAVE", evidence="")]
    gate.enforce(reqs, DNC)
    assert reqs[0]["verdict"] == "HAVE"


# --- the shipped ledger, against player-coach phrasing ---------------------
# These load the tracked example resume on purpose. The coding-bar miss below was
# a hole in the ledger DATA, not in enforce(), so a stub DNC could not have
# caught it.

import pathlib

import pytest
import yaml


def _real_ledger():
    master = yaml.safe_load(
        (pathlib.Path(__file__).parent / "master_resume.example.yaml").read_text())
    return gate.load_profile(master)[1]


PLAYER_COACH_QUOTES = [
    # One live run, 2026-07-24: these slipped the ledger and came back RECOMMEND.
    "Hands-on engineering background in Python and at least one other backend "
    "language, with the ability to read, review, and contribute to production code",
    "Balance hands-on technical contributions to backend services, data "
    "pipelines, and AWS infrastructure with management responsibilities",
    "This is a player-coach role",
    "You will contribute code alongside your team",
]


@pytest.mark.parametrize("quote", PLAYER_COACH_QUOTES)
def test_ledger_forces_none_on_player_coach_requirements(quote):
    """do_not_claim is absolute: a role that wants a manager who writes
    production code is a NONE however generously the model graded it."""
    out = gate.enforce([_req(verdict="PARTIAL", quote=quote,
                             bridge="scripting is adjacent")], _real_ledger())
    assert out[0]["verdict"] == "NONE"
    assert "hand-writing production code" in out[0]["forced"]


PURE_MANAGEMENT_QUOTES = [
    # Another live run, 2026-07-24: a management role with no coding bar. It must
    # stay untouched, or the widened tokens are producing false disqualifications.
    "Lead multiple engineering teams in delivering scalable, secure, and "
    "high-quality software products.",
    "Drive adoption of modern development practices including Agile, DevOps, "
    "CI/CD and test automation.",
    "Champion engineering excellence through code quality, technical reviews, "
    "observability, and continuous improvement.",
    "Track record of delivering enterprise-grade software in regulated "
    "environments (SOC2, HIPAA).",
    "Ability to drive technical strategy, architecture decisions, and platform "
    "evolution.",
]


@pytest.mark.parametrize("quote", PURE_MANAGEMENT_QUOTES)
def test_ledger_leaves_pure_management_requirements_alone(quote):
    out = gate.enforce([_req(verdict="HAVE", quote=quote)], _real_ledger())
    assert out[0]["verdict"] == "HAVE"
    assert not out[0]["forced"]

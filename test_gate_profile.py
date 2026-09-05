import json

import pytest
import yaml
from pathlib import Path

import gate


def test_capability_without_evidence_is_rejected():
    master = {"capabilities": [{"claim": "quantum computing", "evidence": ""}],
              "do_not_claim": []}
    with pytest.raises(gate.ProfileError):
        gate.load_profile(master)


def test_do_not_claim_with_only_match_word_tokens_is_valid():
    """match_word alone can fire, so requiring `match` too would reject a
    perfectly good entry (the ECS/EKS one is nearly this shape)."""
    master = {"capabilities": [],
              "do_not_claim": [{"claim": "container orchestration",
                                "match_word": ["ecs", "eks"]}]}
    _, dnc = gate.load_profile(master)
    assert dnc[0]["match_word"] == ["ecs", "eks"]


def test_do_not_claim_with_no_tokens_of_either_kind_is_rejected():
    master = {"capabilities": [], "do_not_claim": [{"claim": "orphan"}]}
    with pytest.raises(gate.ProfileError):
        gate.load_profile(master)


def test_load_profile_returns_capabilities_and_do_not_claim():
    master = {
        "capabilities": [
            {"claim": "multi-account AWS governance",
             "evidence": "Northwind Commerce, 10-account AWS Organization, Dec 2022 to Jul 2024"}],
        "do_not_claim": [
            {"claim": "event correlation", "match": ["correlation"]}],
    }
    caps, dnc = gate.load_profile(master)
    assert caps[0]["claim"] == "multi-account AWS governance"
    assert dnc[0]["match"] == ["correlation"]


def test_the_shipped_master_resume_has_a_valid_ledger():
    """The tracked master_resume.example.yaml must itself load cleanly. It is the
    template every user copies to master_resume.yaml, so a broken ledger here is
    a broken ledger for everyone."""
    p = Path(__file__).resolve().parent / "master_resume.example.yaml"
    master = yaml.safe_load(p.read_text())
    caps, dnc = gate.load_profile(master)
    assert caps, "master_resume.example.yaml must define capabilities"
    dnc_claims = " ".join(d["claim"] for d in dnc).lower()
    for required in ("data catalog", "correlation", "production code",
                     "container orchestration"):
        assert required in dnc_claims, f"do_not_claim must cover {required}"


def _real_dnc():
    p = Path(__file__).resolve().parent / "master_resume.example.yaml"
    _, dnc = gate.load_profile(yaml.safe_load(p.read_text()))
    return dnc


def _req(quote):
    return {"quote": quote, "topic": "", "hard": True, "confidence": "high",
            "verdict": "HAVE", "evidence": "x", "bridge": ""}


@pytest.mark.parametrize("quote", [
    "Strong production experience with containers and a container orchestration"
    " platform (ECS or EKS/Kubernetes)",
    "Deep hands-on Kubernetes experience",
    "Own our container orchestration platform, currently ECS",
    "Experience running workloads on Amazon EKS",
    # Standalone platform names, with no "Kubernetes" and no "orchestration" in
    # the sentence to carry the match. The first four cases above all contained
    # one or the other, which hid the fact that a bare "ECS" requirement matched
    # nothing at all. The ledger has to stand on its own here: the semantic
    # screen is a second pass that can be disabled or fail its model call, and
    # when it does, an optimistic HAVE would survive enforce() untouched.
    "5+ years running production workloads on ECS",
    "Deep experience with EKS",
    "ECS/EKS",
    "Migrate our services to EKS",
    "Hands-on with ECS, including task definitions and service autoscaling",
])
def test_orchestration_requirements_are_forced_to_none(quote):
    """The profile has Docker, not a control plane. Without this entry the gate is
    free to bridge a plain containerization bullet into a PARTIAL, which is exactly
    the stretch bridge a coding-bar requirement got away with on one live run
    before it was ledgered."""
    out = gate.enforce([_req(quote)], _real_dnc())
    assert out[0]["verdict"] == "NONE"
    assert out[0]["forced"].startswith(gate.LEDGER_FORCED)


@pytest.mark.parametrize("quote", [
    # "eks" is inside "weeks" and "ecs" is inside "specs", both ordinary JD
    # vocabulary. These are why the two tokens live in match_word: whole-word
    # matching catches a bare "ECS" without dragging these along with it.
    "Ship your first impactful changes to production within the first two weeks",
    "Write clear technical specs before implementation",
    "Review the architecture specs and the rollout plan for the next six weeks",
    # Docker and containerization ARE his. The ledger entry names the
    # orchestration platform precisely so these keep grading on their merits.
    "Maintain our devcontainer setup so engineers get reproducible environments",
    "Own our containerization strategy (Docker), image standards and base image security",
    "Strong experience building and securing container images",
])
def test_orchestration_tokens_do_not_misfire(quote):
    out = gate.enforce([_req(quote)], _real_dnc())
    assert out[0]["forced"] == "", f"ledger misfired on {quote!r}"


def test_evidence_includes_location_from_contact():
    """A residency requirement is unanswerable if the gate never sees where the
    candidate lives. On one live run two different models each graded 'must reside
    near Portland' a hard NONE while the resume said Portland, OR, because
    build_evidence dropped the contact block."""
    master = {
        "contact": {"name": "A", "email": "a@b.c", "phone": "1",
                    "location": "Portland, OR 97205"},
        "summary": "s", "experience": [], "skills": [],
        "certifications": [], "education": [], "capabilities": [],
    }
    ev = gate.build_evidence(master)
    assert ev["location"] == "Portland, OR 97205"
    # Contact details that are not evidence must not be shipped to the model.
    blob = json.dumps(ev)
    assert "a@b.c" not in blob
    assert "\"phone\"" not in blob


def test_evidence_location_is_blank_when_absent():
    """A resume with no contact block must not crash the gate."""
    assert gate.build_evidence({"summary": "s"})["location"] == ""
    assert gate.build_evidence({"contact": None})["location"] == ""


# --- malformed SHAPES, not just malformed content -------------------------
#
# `load_profile` reads a hand-edited YAML file, so "valid YAML, wrong shape"
# is a real state on disk. Every one of these used to raise AttributeError on
# a `.get` call, which is not what callers catch: gate.run_gate maps
# ProfileError to ERROR (fail closed) and job_cli.refine_pipeline maps it to
# un-demoted scoring (fail safe), and an AttributeError sailed past both and
# took the unattended nightly digest down with it.

def test_an_empty_document_is_a_profile_error():
    """A master resume emptied out parses as None, not as {}."""
    with pytest.raises(gate.ProfileError):
        gate.load_profile(None)


def test_a_top_level_list_is_a_profile_error():
    with pytest.raises(gate.ProfileError):
        gate.load_profile([{"claim": "x"}])


def test_a_scalar_document_is_a_profile_error():
    with pytest.raises(gate.ProfileError):
        gate.load_profile("capabilities")


def test_a_non_mapping_capability_is_a_profile_error():
    with pytest.raises(gate.ProfileError):
        gate.load_profile({"capabilities": ["quantum computing"],
                           "do_not_claim": []})


def test_a_non_mapping_do_not_claim_entry_is_a_profile_error():
    with pytest.raises(gate.ProfileError):
        gate.load_profile({"capabilities": [],
                           "do_not_claim": ["data catalog"]})


def test_a_non_list_do_not_claim_is_a_profile_error():
    """A mapping here iterates its KEYS, so every entry reads as a string."""
    with pytest.raises(gate.ProfileError):
        gate.load_profile({"capabilities": [],
                           "do_not_claim": {"data catalog": ["catalog"]}})


def test_a_non_list_capabilities_is_a_profile_error():
    with pytest.raises(gate.ProfileError):
        gate.load_profile({"capabilities": {"claim": "x", "evidence": "y"},
                           "do_not_claim": []})


# `works_as` is read by title_check, which run_gate calls outside the try that
# guards extract(). load_profile validated the two ledger keys and not this one,
# so the P1 (a wrong-shaped master escaping run_gate past _fail, leaving the
# previous decision on the row for require_pass to honour) was still reachable
# through a third key.

def test_a_non_list_works_as_is_a_profile_error():
    with pytest.raises(gate.ProfileError):
        gate.load_profile({"works_as": 5, "capabilities": [], "do_not_claim": []})


def test_a_non_string_works_as_entry_is_a_profile_error():
    with pytest.raises(gate.ProfileError):
        gate.load_profile({"works_as": [2026], "capabilities": [],
                           "do_not_claim": []})


def test_a_bare_string_works_as_is_a_profile_error():
    """A string iterates into characters, so it would silently become
    ['m','a','n',...] and quietly break every title comparison."""
    with pytest.raises(gate.ProfileError):
        gate.load_profile({"works_as": "manager", "capabilities": [],
                           "do_not_claim": []})


def test_an_absent_or_empty_works_as_stays_valid():
    gate.load_profile({"capabilities": [], "do_not_claim": []})
    gate.load_profile({"works_as": [], "capabilities": [], "do_not_claim": []})

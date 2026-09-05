"""The ranker must not promote leads the Fit Gate will certainly refuse.

fit.score() knew nothing about do_not_claim, so a role whose central
requirement is a forbidden competency still scored on title and location alone.
The live case (2026-08-20): a "Platform Engineering Manager - US
Remote" scored 90 and sat at the top of the discovered pile, while its JD asked
for "Extensive hands-on Kubernetes administration at scale, including ArgoCD,
Helm and cluster lifecycle management". The gate returned DO_NOT_APPLY the
moment it was queued. The attention had already been spent by then.

This is a RANKING signal, not a second gate. It only ever moves a score DOWN,
it never blocks anything, and the gate remains the only thing that decides.
"""
from pathlib import Path

import fit
import gate

PROFILE = fit.load_profile(Path(__file__).resolve().parent / "profile.example.yaml")
_CAPS, LEDGER = gate.load_profile(
    fit.load_master(Path(__file__).resolve().parent / "master_resume.example.yaml"))

K8S_REQ = ("Extensive hands-on Kubernetes administration at scale, "
           "including ArgoCD, Helm and cluster lifecycle management.")


def _job(title, location="Remote", description="", salary_min=None):
    return {"title": title, "location": location,
            "description": description, "salary_min": salary_min}


def test_ledger_hit_caps_a_high_scoring_lead():
    """The live shape: strong title, forbidden central requirement."""
    job = _job("Platform Engineering Manager - US Remote", description=K8S_REQ)
    uncapped, _ = fit.score(job, PROFILE)
    capped, reasons = fit.score(job, PROFILE, do_not_claim=LEDGER)
    assert uncapped >= 80, "fixture must reproduce the high score, else it proves nothing"
    assert capped <= fit.LEDGER_CAP
    assert capped < uncapped
    assert "ledger:" in reasons


def test_clean_lead_is_untouched_by_the_ledger():
    job = _job("Platform Engineering Manager - US Remote",
               description="Terraform, AWS networking and CI/CD ownership.")
    without = fit.score(job, PROFILE)
    with_ledger = fit.score(job, PROFILE, do_not_claim=LEDGER)
    assert with_ledger == without
    assert "ledger:" not in with_ledger[1]


def test_omitting_the_ledger_preserves_old_behaviour():
    """Backwards compatible: the one existing caller must be free to not pass it."""
    job = _job("Platform Engineering Manager", description=K8S_REQ)
    assert fit.score(job, PROFILE) == fit.score(job, PROFILE, do_not_claim=None)


def test_empty_description_is_never_capped():
    """44 discovered rows carry no JD. Unseen is not the same as clean, so the
    filter fails SAFE and leaves them ranking normally rather than burying them."""
    job = _job("Platform Engineering Manager - US Remote", description="")
    assert fit.score(job, PROFILE, do_not_claim=LEDGER) == fit.score(job, PROFILE)


def test_title_only_ledger_hit_still_caps():
    job = _job("Kubernetes Platform Engineer", description="")
    capped, reasons = fit.score(job, PROFILE, do_not_claim=LEDGER)
    assert capped <= fit.LEDGER_CAP
    assert "ledger:" in reasons


def test_match_word_semantics_carry_over_from_the_gate():
    """'ecs' lives in match_word precisely so it cannot fire on 'specs'."""
    job = _job("Platform Engineering Manager",
               description="Write clear specs and review them within weeks.")
    assert "ledger:" not in fit.score(job, PROFILE, do_not_claim=LEDGER)[1]
    hit = _job("Platform Engineering Manager", description="Run workloads on ECS.")
    assert "ledger:" in fit.score(hit, PROFILE, do_not_claim=LEDGER)[1]


def test_reason_names_the_claim_so_the_demotion_is_auditable():
    job = _job("Platform Engineering Manager", description=K8S_REQ)
    _, reasons = fit.score(job, PROFILE, do_not_claim=LEDGER)
    assert "container orchestration" in reasons.lower()


def test_cap_is_a_ceiling_not_a_floor():
    """A lead that already scores below the cap must not be RAISED to it."""
    job = _job("Warehouse Associate", location="Dayton, OH", description=K8S_REQ)
    plain, _ = fit.score(job, PROFILE)
    capped, _ = fit.score(job, PROFILE, do_not_claim=LEDGER)
    assert capped <= plain

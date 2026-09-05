from pathlib import Path

import fit

# The tracked example profile, not the user-supplied profile.yaml, which is
# gitignored and absent from a fresh clone.
PROFILE = fit.load_profile(Path(__file__).resolve().parent / "profile.example.yaml")


def _job(title, location="Remote", description="", salary_min=None):
    return {"title": title, "location": location,
            "description": description, "salary_min": salary_min}


def test_strong_remote_title_scores_high():
    score, reasons = fit.score(
        _job("Staff Solutions Architect, Enterprise"), PROFILE)
    assert score >= 80
    assert "title" in reasons
    assert "remote" in reasons


def test_heavy_coding_ic_role_scores_low():
    # A hands-on IC role with coding markers must sink, however senior the title.
    job = _job("Senior Software Development Engineer",
               description="You will write production code and code daily.")
    score, _ = fit.score(job, PROFILE)
    assert score < 50


def test_excluded_hardware_role_is_penalized():
    job = _job("Solutions Architect",
               description="Defense hardware manufacturing program.")
    score, reasons = fit.score(job, PROFILE)
    assert "exclude" in reasons
    assert score < 60


def test_missing_salary_is_neutral_not_penalized():
    with_sal = fit.score(_job("Solutions Architect", salary_min=160000), PROFILE)[0]
    no_sal = fit.score(_job("Solutions Architect"), PROFILE)[0]
    below = fit.score(_job("Solutions Architect", salary_min=120000), PROFILE)[0]
    assert with_sal > no_sal > below


def test_non_remote_non_onsite_is_penalized():
    remote = fit.score(_job("Solutions Architect", location="Remote"), PROFILE)[0]
    nyc = fit.score(_job("Solutions Architect", location="New York, NY"), PROFILE)[0]
    onsite = fit.score(_job("Solutions Architect", location="Portland, OR"), PROFILE)[0]
    assert remote > onsite > nyc


def test_score_is_clamped_and_int():
    score, _ = fit.score(_job("Principal Staff Director Architect"), PROFILE)
    assert isinstance(score, int)
    assert 0 <= score <= 100


def test_reasons_have_no_em_dash():
    _, reasons = fit.score(_job("Solutions Architect"), PROFILE)
    assert "—" not in reasons


# --- presales "Solutions Architect" is a different job ----------------------
# A live 2026-07-24 lead: "Senior Solutions Architect, Identity Threat
# Protection" scored 90, the structural ceiling, on "title:strong; remote"
# alone. It is a quota-adjacent presales role. Neither marker list touched it,
# so nothing in the profile could tell it apart from the AWS cloud architecture
# work the profile actually targets.

PRESALES_JD = """
The Senior Identity Specialist is the most experienced technical subject matter
expert on the team, owning field credibility end to end. Lead the 60-minute
deep-dive meeting for any customer. Own the PoV motion for the most complex
opportunities as the named technical lead. Author the competitive positioning
strategy. Design and deliver technical training to SEs, AEs, PSCs and TAMs as
part of the enablement curriculum. Partner with product marketing on launch.
"""

CLOUD_SA_JD = """
Design and operate multi-account AWS environments for enterprise customers.
Own Terraform modules, landing zones, IAM boundaries, and cost governance.
Partner with platform and SRE teams on reliability, observability, and
incident response. Drive architecture reviews and well-architected assessments.
"""


def test_presales_solutions_architect_does_not_score_high():
    score, reasons = fit.score(
        _job("Senior Solutions Architect, Identity Threat Protection",
             location="Remote - USA", description=PRESALES_JD), PROFILE)
    assert score < 50, f"presales role scored {score} ({reasons})"
    assert "sales-role" in reasons


def test_presales_role_forfeits_the_strong_title_bonus():
    """The title bonus is the largest weight in the file, so it has to be
    withheld, not merely out-penalized."""
    _, reasons = fit.score(
        _job("Senior Solutions Architect, Identity Threat Protection",
             location="Remote - USA", description=PRESALES_JD), PROFILE)
    assert "title:strong" not in reasons


def test_genuine_cloud_solutions_architect_still_scores_high():
    """The fix must not cost the profile the AWS architecture roles it targets."""
    score, reasons = fit.score(
        _job("Senior Solutions Architect, Cloud Infrastructure",
             location="Remote - USA", description=CLOUD_SA_JD), PROFILE)
    assert score >= 80, f"cloud SA scored {score} ({reasons})"
    assert "title:strong" in reasons
    assert "sales-role" not in reasons


def test_platform_role_mentioning_customers_is_not_flagged_as_sales():
    """Guard against the markers being too broad: plenty of legitimate infra
    roles are customer-facing and mention demos or the field."""
    score, reasons = fit.score(
        _job("Principal Platform Engineer", location="Remote",
             description="Customer-facing platform team. You will demo new "
                         "capabilities to stakeholders and support field teams "
                         "with deep technical guidance on ideal deployments."),
        PROFILE)
    assert "sales-role" not in reasons, f"false positive ({reasons})"
    assert score >= 80


# --- professional services is a third category ------------------------------
# Four live leads all kept scoring 90 after the presales fix. They are
# customer-facing DELIVERY roles (professional services, engagements,
# trusted-advisor work), not presales, so the sales markers missed them. Same
# two words in the title, still not cloud architecture.

SERVICES_JD = """
Solutions Architects here are the core of our elite professional services
organization, helping customers realize value. You will be a trusted advisor
to technical and executive stakeholders throughout the engagement.
"""

ENGAGEMENT_JD = """
We are looking for a technical engagement manager to lead customer
implementations end to end, partnering with the sales team on scoping.
"""


def test_professional_services_role_does_not_score_high():
    score, reasons = fit.score(
        _job("Senior Solutions Architect", location="Remote - USA",
             description=SERVICES_JD), PROFILE)
    assert score < 50, f"services role scored {score} ({reasons})"
    assert "services-role" in reasons
    assert "title:strong" not in reasons


def test_customer_engagement_role_does_not_score_high():
    score, reasons = fit.score(
        _job("Solutions Architect", location="Remote",
             description=ENGAGEMENT_JD), PROFILE)
    assert score < 50, f"engagement role scored {score} ({reasons})"
    assert "title:strong" not in reasons


def test_platform_role_describing_services_is_not_flagged():
    """Guard: 'services' is ubiquitous in infra postings and must not trip it."""
    score, reasons = fit.score(
        _job("Staff Platform Engineer", location="Remote",
             description="Operate microservices and shared platform services. "
                         "Own service reliability, service meshes, and the "
                         "internal developer platform for product teams."),
        PROFILE)
    assert "services-role" not in reasons, f"false positive ({reasons})"
    assert "sales-role" not in reasons
    assert score >= 80


# --- title matching must not depend on word order ---------------------------
# A live "Manager, DevOps Engineering" lead scored 50 (base + remote, no title
# match at all) while its interview loop was already in the third round.
# "engineering manager" is not a SUBSTRING of "manager, devops engineering",
# though every word is present. 162 of 408 live rows matched no title term;
# 12 of them are this bug.


def test_reordered_title_still_matches_strong():
    score, reasons = fit.score(
        _job("Manager, DevOps Engineering", location="Tampa Hybrid or Remote"),
        PROFILE)
    assert "title:strong" in reasons, f"got {reasons}"
    assert score >= 80


def test_parenthesised_reordered_title_matches():
    _, reasons = fit.score(_job("Manager, Engineering (DevOps/SRE)"), PROFILE)
    assert "title:strong" in reasons, f"got {reasons}"


def test_reordered_good_title_matches():
    _, reasons = fit.score(_job("Lead Cloud Engineer"), PROFILE)
    assert "title:good" in reasons, f"got {reasons}"


def test_plural_substring_titles_keep_matching():
    """Regression guard: a pure word-set matcher would drop these four live
    titles, because 'architect' is not a word in 'Architects'."""
    for t in ("Manager, Solutions Architects",
              "Manager, Solutions Architects - US East",
              "Manager, Public Sector Solutions Architects"):
        _, reasons = fit.score(_job(t), PROFILE)
        assert "title:strong" in reasons, f"{t} -> {reasons}"


def test_unrelated_title_still_matches_nothing():
    _, reasons = fit.score(_job("Registered Nurse, Pediatrics"), PROFILE)
    assert "title:strong" not in reasons and "title:good" not in reasons

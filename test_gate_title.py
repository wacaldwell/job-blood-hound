import gate

MASTER = {"works_as": ["manager", "architect", "lead"]}


def test_engineer_title_mismatches_an_operations_leadership_profile():
    out = gate.title_check("Senior Staff Operations Engineer, AIOps", MASTER)
    assert out["role_noun"] == "engineer"
    assert out["mismatch"] is True
    assert "coding" in out["note"].lower()


def test_manager_title_matches():
    out = gate.title_check("Senior Engineering Manager, Platform", MASTER)
    assert out["role_noun"] == "manager"
    assert out["mismatch"] is False


def test_architect_title_matches():
    out = gate.title_check("Principal Solutions Architect", MASTER)
    assert out["mismatch"] is False
    assert out["role_noun"] == "architect"


def test_unknown_role_noun_is_not_a_mismatch():
    """Do not invent a mismatch out of a title we cannot parse."""
    out = gate.title_check("Cloud Wizard", MASTER)
    assert out["role_noun"] == ""
    assert out["mismatch"] is False


def test_title_check_is_a_flag_and_never_changes_the_decision():
    """Pinned so a later edit cannot quietly promote this to a blocker."""
    reqs = [{"quote": "q", "topic": "t", "hard": True, "confidence": "high",
             "verdict": "HAVE", "evidence": "e", "bridge": "", "forced": "",
             "ruled_by_human": False}]
    assert gate.decide(reqs) == gate.PROCEED  # decide() takes no title at all


def test_active_directory_engineer_is_an_engineer_not_a_director():
    """A raw substring match reads 'director' inside 'Directory'. Active Directory
    is common in the cloud and infrastructure titles this tool actually scans, so
    this would have mislabeled real postings."""
    out = gate.title_check("Active Directory Engineer", MASTER)
    assert out["role_noun"] == "engineer"
    assert out["mismatch"] is True


def test_leadership_does_not_match_lead():
    out = gate.title_check("VP of Engineering Leadership", MASTER)
    assert out["role_noun"] == ""
    assert out["mismatch"] is False


def test_engineering_manager_is_a_manager_not_an_engineer():
    """Pins the ROLE_NOUNS ordering, which is the whole reason the compound entry exists."""
    out = gate.title_check("Senior Engineering Manager, Platform", MASTER)
    assert out["role_noun"] == "manager"
    assert out["mismatch"] is False


def test_director_of_devops_engineering_is_a_director():
    """MASTER's works_as is manager/architect/lead, so a director title is a
    real mismatch here, not a false positive from the word-boundary fix."""
    out = gate.title_check("Director of DevOps Engineering", MASTER)
    assert out["role_noun"] == "director"
    assert out["mismatch"] is True

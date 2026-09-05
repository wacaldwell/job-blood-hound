"""Location is a hard eligibility gate: skills do not matter for a job you
cannot take.

The gate graded skills and was blind to location. On the manual `fetch` path
(which skips the scan's location filter) it recommended a Cleveland in-office
bank role as a strong match. The profile used here takes remote, or on-site in
the Portland/Beaverton area.
"""
import json
from pathlib import Path

import pytest

import fit
import gate
import jobdb


@pytest.fixture(autouse=True)
def _example_profile(monkeypatch):
    """`gate.location_ok` reads its remote_ok/onsite_ok lists from profile.yaml,
    which is user-supplied and absent from a fresh clone. Pin them to the tracked
    example so the on-site assertions below name one known area."""
    real = fit.load_profile
    example = Path(__file__).resolve().parent / "profile.example.yaml"
    monkeypatch.setattr(fit, "load_profile", lambda path=None: real(path or example))


def _db(tmp_path, location):
    db = jobdb.JobDB(tmp_path / "t.db")
    db.upsert_job({"ats": "greenhouse", "company": "huntington", "id": "1",
                   "title": "GCP Engineering Manager", "location": location})
    return db, db.get(jobdb.make_job_uid("greenhouse", "huntington", "1"))


def _strong_reqs():
    return [{"quote": "q", "topic": f"t{i}", "hard": True, "confidence": "high",
             "verdict": "HAVE", "evidence": "e", "bridge": "", "forced": "",
             "ruled_by_human": False} for i in range(8)]


def test_a_non_remote_role_is_blocked_regardless_of_strong_skills(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_APPS_DIR", str(tmp_path / "apps"))

    def fake_call(system, user, api_key):
        return json.dumps({"requirements": [
            {"quote": "q", "topic": f"t{i}", "hard": True, "confidence": "high",
             "verdict": "HAVE", "evidence": "e", "bridge": ""} for i in range(8)]})

    db, row = _db(tmp_path, "Cleveland, Ohio, US")
    master = {"works_as": ["manager"], "capabilities": [{"claim": "x", "evidence": "y"}],
              "do_not_claim": []}
    out = gate.run_gate(db, row, master, api_key="k", jd_text="jd", call=fake_call)

    assert out["decision"] == gate.NOT_REMOTE
    # The skills read is preserved so the report can say "right skills, wrong place".
    assert out["skills_decision"] == gate.RECOMMEND
    assert db.get(row["uid"])["gate_decision"] == gate.NOT_REMOTE


def test_a_remote_role_keeps_its_skills_verdict(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_APPS_DIR", str(tmp_path / "apps"))

    def fake_call(system, user, api_key):
        return json.dumps({"requirements": [
            {"quote": "q", "topic": f"t{i}", "hard": True, "confidence": "high",
             "verdict": "HAVE", "evidence": "e", "bridge": ""} for i in range(8)]})

    db, row = _db(tmp_path, "Remote - United States")
    master = {"works_as": ["manager"], "capabilities": [{"claim": "x", "evidence": "y"}],
              "do_not_claim": []}
    out = gate.run_gate(db, row, master, api_key="k", jd_text="jd", call=fake_call)
    assert out["decision"] == gate.RECOMMEND


def test_the_configured_onsite_area_is_eligible():
    assert gate.location_ok({"location": "Portland, OR"})["ok"] is True
    assert gate.location_ok({"location": "Beaverton, Oregon"})["ok"] is True


def test_a_far_onsite_city_is_not_eligible():
    r = gate.location_ok({"location": "Cleveland, Ohio, US"})
    assert r["ok"] is False
    assert "cleveland" in r["reason"].lower()


def test_an_onsite_us_role_spelling_out_the_country_is_blocked():
    """The big leak. 'City, State, United States' is the standard ATS format for
    an ON-SITE role. remote_ok in the profile contains 'united states' for the
    soft ranker, but as a hard gate that would pass every US on-site posting."""
    for loc in ("New York, NY, United States", "Charlotte, North Carolina, United States",
                "Austin, TX, U.S."):
        r = gate.location_ok({"title": "Cloud Architect", "location": loc})
        assert r["ok"] is False, f"{loc} should block, got {r}"


def test_explicit_remote_field_beats_a_stray_inoffice_line_in_the_jd():
    """A clearly-remote posting must not be blocked by an 'in-office' clause buried
    in the JD prose. The structured remote signal wins first."""
    r = gate.location_ok({"title": "Senior SRE, Remote", "location": "Remote - USA"},
                         jd_text="Standard work setting: in-office quarterly for offsites.")
    assert r["ok"] is True


def test_a_missing_location_is_not_manufactured_into_a_block():
    r = gate.location_ok({"location": ""})
    assert r["ok"] is True
    assert "verify" in r["reason"].lower()


def test_a_remote_role_listing_an_hq_city_is_not_blocked():
    """A live case: the location field says Charleston WV, but the JD says it is
    100% remote. Reading the field alone would wrongly block a remote job."""
    r = gate.location_ok({"title": "Senior AWS DevOps Engineer",
                          "location": "Charleston, West Virginia, USA"},
                         jd_text="This is a great role. 100% remote work. Competitive salary.")
    assert r["ok"] is True


def test_remote_in_the_title_counts():
    r = gate.location_ok({"title": "Senior DevOps Engineer - Remote - USA",
                          "location": "Charleston, West Virginia, USA"})
    assert r["ok"] is True


def test_a_bare_remote_mention_does_not_unblock_an_onsite_role():
    """A hybrid Cleveland role whose JD merely mentions 'remote collaboration'
    must still block. Only unambiguous remote phrases count."""
    r = gate.location_ok({"title": "Cloud Engineering Manager",
                          "location": "Cleveland, Ohio, US"},
                         jd_text="Hybrid role. We support remote collaboration with global teams.")
    assert r["ok"] is False


def test_workplace_type_office_is_authoritative_and_blocks():
    """The strongest signal. An explicit 'Workplace Type: Office' field settles
    it, ahead of any remote-sounding culture blurb in the same JD."""
    jd = ("Compensation and benefits. Workplace Type: Office. Our approach to "
          "flexibility combines in-office and work from home for remote roles.")
    r = gate.location_ok({"title": "GCP Engineering Manager",
                          "location": "Cleveland, Ohio, US"}, jd_text=jd)
    assert r["ok"] is False
    assert "office" in r["reason"].lower()


def test_workplace_type_remote_is_authoritative_and_allows():
    jd = "Details. Workplace Type: Remote. Some HQ address in Ohio."
    r = gate.location_ok({"title": "Cloud Architect", "location": "Columbus, Ohio"},
                         jd_text=jd)
    assert r["ok"] is True


def test_workplace_type_office_in_the_onsite_area_is_still_eligible():
    jd = "Workplace Type: Office."
    r = gate.location_ok({"title": "Cloud Lead", "location": "Portland, OR"}, jd_text=jd)
    assert r["ok"] is True


def test_generic_flexibility_boilerplate_does_not_unblock_an_onsite_role():
    """The generic-boilerplate trap. The JD carries the company's flexibility
    blurb ('in-office and work from home', 'Remote roles will come to offices'),
    which is policy, not a statement that THIS Cleveland role is remote."""
    jd = ("You may be eligible for a flexible work arrangement. We are combining "
          "the best of both worlds: in-office and work from home. Remote roles will "
          "also have the opportunity to come together in our offices for moments "
          "that matter.")
    r = gate.location_ok({"title": "GCP Engineering Manager",
                          "location": "Cleveland, Ohio, US"}, jd_text=jd)
    assert r["ok"] is False


def test_not_remote_blocks_drafting(tmp_path):
    db, row = _db(tmp_path, "Cleveland, Ohio, US")
    db.set_gate(row["uid"], gate.NOT_REMOTE, "{}", "/tmp/r.md")
    with pytest.raises(gate.GateBlocked):
        gate.require_pass(db, db.get(row["uid"]))


def test_recompute_preserves_the_location_block(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_APPS_DIR", str(tmp_path / "apps"))
    db, row = _db(tmp_path, "Cleveland, Ohio, US")
    reqs = _strong_reqs()
    db.set_gate(row["uid"], gate.NOT_REMOTE,
                json.dumps({"requirements": reqs, "title": {},
                            "location": {"ok": False, "reason": "cleveland"},
                            "skills_decision": gate.RECOMMEND}), "/tmp/r.md")
    out = gate.recompute(db, db.get(row["uid"]))
    assert out["decision"] == gate.NOT_REMOTE

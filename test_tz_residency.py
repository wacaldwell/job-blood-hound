"""Timezone/region residency filtering.

A whole-country "remote" role can still pin a non-Eastern US territory, either
in the title ("... - Central") or only in the JD body ("reside within the
Central Time Zone"). The filter treats US Eastern as the home zone, so those
are held for a human JD check ('verify') rather than surfaced as
reachable-remote.
"""
import argparse

import jobdb
import job_cli
import job_monitor as jm


# -- the shared predicate --------------------------------------------------

def test_central_time_zone_body_excludes_eastern():
    txt = "Reside within the Central Time Zone with Chicago, Dallas, or Austin preferred."
    assert jm.residency_excludes_eastern(txt) is True


def test_title_central_suffix_excludes_eastern():
    assert jm.residency_excludes_eastern(
        "Staff Solutions Architect, Growth - Central", in_title=True)


def test_title_west_suffix_excludes_eastern():
    # "- West" reads as a Pacific territory; no space before West is still a hit.
    assert jm.residency_excludes_eastern(
        "Staff Solutions Architect, New Logo -West", in_title=True)


def test_title_east_suffix_is_fine_for_eastern():
    assert jm.residency_excludes_eastern(
        "Staff Solutions Architect, New Logo - East", in_title=True) is False


def test_region_suffix_is_title_only_not_body():
    # A bare "- central" in prose is not a residency signal (fails open).
    assert jm.residency_excludes_eastern("collaboration - central to the role") is False
    # Same string, but read as a title, IS a territory suffix.
    assert jm.residency_excludes_eastern("collaboration - central", in_title=True) is True


def test_pacific_time_body_excludes_eastern():
    assert jm.residency_excludes_eastern("Must be located in the Pacific time zone.")


def test_abbreviations_exclude_eastern():
    assert jm.residency_excludes_eastern("Open only to candidates in CST or MST.")


def test_eastern_in_enumeration_is_not_excluded():
    # "Eastern or Central" includes Eastern -> eligible, not quarantined.
    assert jm.residency_excludes_eastern(
        "Open to candidates in the Eastern or Central time zones.") is False


def test_explicit_eastern_only_is_not_excluded():
    assert jm.residency_excludes_eastern("Eastern Standard Time (EST) required.") is False


def test_established_abbrev_does_not_inject_eastern():
    # Lowercase "est." (established) must NOT count as Eastern and let a
    # Central-only role slip through.
    txt = "Acme Corp (est. 2015). Reside within the Central Time Zone."
    assert jm.residency_excludes_eastern(txt) is True


def test_uppercase_eastern_abbrev_still_counts():
    # "EST or PST" includes Eastern, so this is eligible -> not excluded.
    assert jm.residency_excludes_eastern("Open to EST or PST candidates.") is False


def test_neutral_text_is_not_excluded():
    assert jm.residency_excludes_eastern("Senior Site Reliability Engineer") is False


def test_remote_location_field_alone_is_not_excluded():
    # The exact field that leaked the Temporal role: no zone signal here.
    assert jm.residency_excludes_eastern("United States - Remote Opportunity") is False


def test_all_zones_list_with_trailing_label_includes_eastern():
    # Eastern leads the list; the shared "Time Zones" label trails it. This is an
    # all-US-remote phrasing and must NOT be quarantined.
    assert jm.residency_excludes_eastern(
        "Open to candidates in Eastern, Central, Mountain, or Pacific Time Zones.") is False


def test_full_time_prefix_does_not_trip_a_zone_word():
    # "full-time" sits before "Central" but is not a time-zone signal.
    assert jm.residency_excludes_eastern(
        "Full-time remote role. Central operations team.") is False


def test_zone_list_after_time_zones_label_is_caught():
    # "time zones:" precedes the list, so the time/zone token is BEFORE each zone.
    assert jm.residency_excludes_eastern(
        "Must reside in US time zones: Central, Mountain, or Pacific.")


def test_mid_title_region_word_is_not_a_suffix():
    # "- Central Platform" is not a territory suffix; must not quarantine.
    assert jm.residency_excludes_eastern(
        "Engineering Manager - Central Platform", in_title=True) is False
    assert jm.residency_excludes_eastern(
        "Sales Lead - West Region Enablement", in_title=True) is False


def test_zone_word_without_time_context_does_not_trip():
    # "central" / "pacific" outside a time-zone context must not quarantine.
    assert jm.residency_excludes_eastern(
        "Central to our mission is reliability across the Pacific Ocean region.") is False


# -- classify_location title heuristic -------------------------------------

REMOTE = "United States - Remote Opportunity"


def test_classify_remote_with_central_title_is_verify():
    assert jm.classify_location("Staff Solutions Architect, Growth - Central", REMOTE, []) == "verify"


def test_classify_remote_with_east_title_stays_remote():
    assert jm.classify_location("Staff Solutions Architect, New Logo - East", REMOTE, []) == "remote"


def test_classify_plain_remote_stays_remote():
    assert jm.classify_location("Senior Site Reliability Engineer", REMOTE, []) == "remote"


# -- body scan during ingest ----------------------------------------------

def _match(ext, title, loc_type="remote"):
    return {"id": ext, "ats": "greenhouse", "company": "acme", "title": title,
            "location": REMOTE, "url": "http://x", "location_type": loc_type,
            "category": "enterprise"}


def test_ingest_body_scan_quarantines_restricted_remote(tmp_path, monkeypatch):
    db = jobdb.JobDB(tmp_path / "t.db")
    match = _match("1", "Staff Solutions Architect, Growth")  # neutral title
    monkeypatch.setattr(jm, "run_scan", lambda cfg, seen, verbose=False: ([], [match], []))

    def fake_describe(row):
        return "Reside within the Central Time Zone with Chicago preferred."

    job_cli.scan_and_ingest(db, cfg={}, describe=fake_describe)
    uid = jobdb.make_job_uid("greenhouse", "acme", "1")
    assert db.get(uid)["location_type"] == "verify"
    db.close()


def test_ingest_body_scan_leaves_clean_remote(tmp_path, monkeypatch):
    db = jobdb.JobDB(tmp_path / "t.db")
    match = _match("2", "Staff Solutions Architect, Growth")
    monkeypatch.setattr(jm, "run_scan", lambda cfg, seen, verbose=False: ([], [match], []))

    job_cli.scan_and_ingest(db, cfg={}, describe=lambda row: "Fully remote across the US.")
    uid = jobdb.make_job_uid("greenhouse", "acme", "2")
    assert db.get(uid)["location_type"] == "remote"
    db.close()


def test_ingest_body_scan_fetch_error_is_non_fatal(tmp_path, monkeypatch):
    db = jobdb.JobDB(tmp_path / "t.db")
    match = _match("3", "Staff Solutions Architect, Growth")
    monkeypatch.setattr(jm, "run_scan", lambda cfg, seen, verbose=False: ([], [match], []))

    def boom(row):
        raise RuntimeError("network down")

    # Must not raise; role simply stays as-is.
    job_cli.scan_and_ingest(db, cfg={}, describe=boom)
    uid = jobdb.make_job_uid("greenhouse", "acme", "3")
    assert db.get(uid)["location_type"] == "remote"
    db.close()

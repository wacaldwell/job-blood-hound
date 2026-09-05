"""build_evidence must not mangle employment dates.

The old `.strip(" to")` stripped the character set {space, t, o}, turning
"present" into "presen" and "oct 2019" into "ct 2019", misrepresenting tenure to
the grading model.
"""
import gate


def test_dates_are_joined_not_char_stripped():
    master = {"experience": [
        {"company": "X", "title": "Eng", "start": "oct 2019", "end": "present", "points": []},
    ]}
    ev = gate.build_evidence(master)
    assert ev["experience"][0]["dates"] == "oct 2019 to present"


def test_a_missing_end_date_has_no_trailing_separator():
    master = {"experience": [
        {"company": "X", "title": "Eng", "start": "Dec 2024", "end": "", "points": []},
    ]}
    assert gate.build_evidence(master)["experience"][0]["dates"] == "Dec 2024"

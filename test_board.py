#!/usr/bin/env python3
"""Tests for the interview round model (jobdb) and the board renderer."""

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

import board
import jobdb
from jobdb import JobDB, TransitionError


def make_db(tmp_path):
    return JobDB(tmp_path / "t.db")


def add(db, ident="acme", state="interviewing"):
    """Insert one job and walk it to `state`."""
    db.upsert_job({
        "id": ident, "ats": "greenhouse", "company": ident,
        "title": "DevOps Manager", "location": "Remote",
        "url": f"https://example.com/{ident}",
    })
    uid = jobdb.make_job_uid("greenhouse", ident, ident)
    path = ["queued", "drafted", "ready", "applied", "interviewing"]
    for s in path:
        db.set_state(uid, s)
        if s == state:
            break
    return uid


# --- round list -----------------------------------------------------------

def test_rounds_round_trip(tmp_path):
    db = make_db(tmp_path)
    uid = add(db)
    db.set_rounds(uid, ["recruiter", "hiring manager", "peer + TPM"])
    assert jobdb.rounds_of(db.get(uid)) == [
        "recruiter", "hiring manager", "peer + TPM"]


def test_rounds_strips_and_drops_blanks(tmp_path):
    db = make_db(tmp_path)
    uid = add(db)
    db.set_rounds(uid, ["  recruiter ", "", "   ", "technical"])
    assert jobdb.rounds_of(db.get(uid)) == ["recruiter", "technical"]


def test_rounds_rejects_empty_list(tmp_path):
    db = make_db(tmp_path)
    uid = add(db)
    with pytest.raises(ValueError):
        db.set_rounds(uid, ["   ", ""])


def test_rounds_rejects_too_many(tmp_path):
    db = make_db(tmp_path)
    uid = add(db)
    with pytest.raises(ValueError):
        db.set_rounds(uid, [f"r{i}" for i in range(jobdb.MAX_ROUNDS + 1)])


def test_shorter_list_clamps_a_dangling_marker(tmp_path):
    db = make_db(tmp_path)
    uid = add(db)
    db.set_rounds(uid, ["a", "b", "c", "d"])
    db.set_stage(uid, at=4)
    assert db.get(uid)["interview_at"] == 4
    db.set_rounds(uid, ["a", "b"])
    assert db.get(uid)["interview_at"] == 2


def test_rounds_audited(tmp_path):
    db = make_db(tmp_path)
    uid = add(db)
    db.set_rounds(uid, ["recruiter", "technical"])
    notes = [h["note"] for h in db.history(uid)]
    assert any(n and n.startswith("rounds: recruiter, technical") for n in notes)


def test_malformed_rounds_blob_degrades_to_empty(tmp_path):
    db = make_db(tmp_path)
    uid = add(db)
    db.conn.execute(
        "UPDATE jobs SET interview_rounds = ? WHERE uid = ?", ("{not json", uid))
    db.conn.commit()
    assert jobdb.rounds_of(db.get(uid)) == []


# --- marker ---------------------------------------------------------------

def test_stage_seeds_default_rounds(tmp_path):
    db = make_db(tmp_path)
    uid = add(db)
    db.set_stage(uid, at=1)
    assert jobdb.rounds_of(db.get(uid)) == jobdb.DEFAULT_ROUNDS
    assert db.get(uid)["interview_at"] == 1


def test_default_frame_is_recruiter_plus_three_rounds(tmp_path):
    """The base frame every application starts on: recruiter, three rounds,
    and a decision terminal appended at render time."""
    assert jobdb.DEFAULT_ROUNDS == ["recruiter", "round", "round", "round"]


def test_seeded_placeholders_name_no_number(tmp_path):
    """The seed may not carry a number that can disagree with its position.

    `set_stage` and `jh rounds` are 1-based over the whole list, so the old
    "round 1" seed sat in position 2: `jh stage <ident> 2` reported "round 1"
    while the board drew the same node as round 1 in position 2, and the two
    surfaces printed conflicting numbers for the same slot.
    """
    assert not [l for l in jobdb.DEFAULT_ROUNDS if any(c.isdigit() for c in l)]


def test_captions_derive_the_frame_from_position():
    """The caption is positional, never stored, so a relabelled round can
    never put a label naming one number into a slot holding another."""
    assert board.captions(
        ["recruiter", "hiring manager", "peer & TPM", "technical", "decision"]
    ) == [
        ("recruiter", ""),
        ("round 1", "hiring manager"),
        ("round 2", "peer & TPM"),
        ("round 3", "technical"),
        ("decision", ""),
    ]


def test_captions_drop_a_detail_that_repeats_its_caption():
    """The unfilled "round 2" seed must not print twice."""
    assert board.captions(["recruiter", "round 1", "round 2", "decision"]) == [
        ("recruiter", ""), ("round 1", ""), ("round 2", ""), ("decision", ""),
    ]


def test_captions_drop_a_placeholder_naming_another_slot():
    """A generic "round N" is a placeholder wherever it lands, not a detail.

    Dropping only the one that happens to match its caption left the worst
    case standing: a seeded "round 3" that a reorder or a shortened list moved
    into the second slot still printed under the derived "round 1", which is
    the exact contradiction the derived caption exists to remove.
    """
    assert board.captions(["recruiter", "round 3", "decision"]) == [
        ("recruiter", ""), ("round 1", ""), ("decision", ""),
    ]
    # the bare seed carries no information either
    assert board.captions(["recruiter", "round", "decision"]) == [
        ("recruiter", ""), ("round 1", ""), ("decision", ""),
    ]
    # a real label that merely starts with the word survives
    assert board.captions(["round table with the platform team", "decision"]) == [
        ("round 1", "round table with the platform team"), ("decision", ""),
    ]


def test_captions_number_rounds_without_a_recruiter_screen():
    assert board.captions(["hiring manager", "panel", "decision"]) == [
        ("round 1", "hiring manager"),
        ("round 2", "panel"),
        ("decision", ""),
    ]


def test_render_puts_the_frame_above_the_people(tmp_path):
    db = make_db(tmp_path)
    uid = add(db, "vertex")
    db.set_rounds(uid, ["recruiter", "hiring manager", "peer & TPM", "technical"])
    db.set_stage(uid, at=4)
    out = board.render(db.interviewing())
    assert '<span class="cap">round 3</span>' in out
    assert '<span class="det">technical</span>' in out
    assert '<span class="cap">recruiter</span>' in out
    # the recruiter and decision stops carry no duplicate detail
    assert '<span class="det">recruiter</span>' not in out
    assert '<span class="det">decision</span>' not in out


def test_default_frame_renders_five_nodes(tmp_path):
    db = make_db(tmp_path)
    uid = add(db, "fresh")
    db.set_stage(uid, at=1)
    out = board.render(db.interviewing())
    # four seeded rounds plus the decision terminal
    assert out.count('class="stop ') == 5
    assert 'class="stop future terminal"' in out


def test_stage_rejects_marker_past_the_list(tmp_path):
    db = make_db(tmp_path)
    uid = add(db)
    db.set_rounds(uid, ["recruiter", "technical"])
    with pytest.raises(ValueError):
        db.set_stage(uid, at=3)
    with pytest.raises(ValueError):
        db.set_stage(uid, at=0)


def test_stage_rejects_non_integer_marker(tmp_path):
    db = make_db(tmp_path)
    uid = add(db)
    with pytest.raises(ValueError):
        db.set_stage(uid, at="2")
    with pytest.raises(ValueError):
        db.set_stage(uid, at=True)


def test_decision_sets_flag_and_clears_position(tmp_path):
    db = make_db(tmp_path)
    uid = add(db)
    db.set_rounds(uid, ["recruiter", "technical"])
    db.set_stage(uid, at=2)
    db.set_stage(uid, decision=True)
    row = db.get(uid)
    assert row["interview_decision"] == 1
    assert row["interview_at"] is None
    # decision is never stored as a round
    assert "decision" not in jobdb.rounds_of(row)


def test_next_note_clears_when_omitted(tmp_path):
    db = make_db(tmp_path)
    uid = add(db)
    db.set_stage(uid, at=1, next_note="screen on Friday")
    assert db.get(uid)["interview_next"] == "screen on Friday"
    db.set_stage(uid, at=1)
    assert db.get(uid)["interview_next"] is None


def test_stage_audited(tmp_path):
    db = make_db(tmp_path)
    uid = add(db)
    db.set_rounds(uid, ["recruiter", "technical"])
    db.set_stage(uid, at=2, next_note="not yet scheduled")
    notes = [h["note"] for h in db.history(uid)]
    assert any(n and n.startswith("stage: technical") and "not yet scheduled" in n
               for n in notes)


def test_set_fields_refuses_interview_columns(tmp_path):
    db = make_db(tmp_path)
    uid = add(db)
    with pytest.raises(ValueError, match="set_rounds or set_stage"):
        db.set_fields(uid, interview_at=3)
    with pytest.raises(ValueError, match="set_rounds or set_stage"):
        db.set_fields(uid, interview_rounds=json.dumps(["x"]))


# --- the board query ------------------------------------------------------

def test_interviewing_returns_only_live_conversations(tmp_path):
    db = make_db(tmp_path)
    live = add(db, "live", state="interviewing")
    add(db, "waiting", state="applied")
    rows = db.interviewing()
    assert [r["uid"] for r in rows] == [live]


# --- renderer -------------------------------------------------------------

def test_render_empty(tmp_path):
    out = board.render([])
    assert "No live conversations" in out
    assert "<!doctype html>" in out


def test_render_places_marker_and_labels(tmp_path):
    db = make_db(tmp_path)
    uid = add(db, "vertex")
    db.set_rounds(uid, ["recruiter", "hiring manager", "peer + TPM", "technical"])
    db.set_stage(uid, at=4, next_note="final round, not yet scheduled")
    out = board.render(db.interviewing())

    assert "peer + TPM" in out
    assert "final round, not yet scheduled" in out
    # three rounds behind the marker, the marked one, and the decision terminal
    assert out.count('class="stop done"') == 3
    assert out.count('class="stop here"') == 1
    assert 'class="stop future terminal"' in out


def test_render_decision_marks_the_terminal_node(tmp_path):
    db = make_db(tmp_path)
    uid = add(db, "initech")
    db.set_rounds(uid, ["recruiter", "hiring manager"])
    db.set_stage(uid, decision=True, next_note="awaiting offer or rejection")
    out = board.render(db.interviewing())
    assert 'class="stop here terminal"' in out
    assert out.count('class="stop done"') == 2


def test_render_unset_marker_still_draws_a_lane(tmp_path):
    db = make_db(tmp_path)
    add(db, "quiet")
    out = board.render(db.interviewing())
    assert "quiet" in out
    assert 'class="stop here"' not in out
    assert "no note on what is next" in out


def test_render_escapes_untrusted_text(tmp_path):
    db = make_db(tmp_path)
    uid = add(db, "xss")
    db.set_rounds(uid, ["<script>alert(1)</script>"])
    db.set_stage(uid, at=1, next_note="<img src=x onerror=alert(2)>")
    out = board.render(db.interviewing())
    # what matters is that no tag survives, not that the words are gone: an
    # escaped "<img ... onerror=...>" is inert text and reads back correctly.
    assert "<script>" not in out
    assert "<img" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out
    assert "&lt;img src=x onerror=alert(2)&gt;" in out


def test_render_has_no_external_references(tmp_path):
    db = make_db(tmp_path)
    uid = add(db, "acme")
    db.set_stage(uid, at=1)
    out = board.render(db.interviewing())
    # the job's own posting URL is the only outbound link the page may carry
    urls = re.findall(r'https?://[^\s"\'<>]+', out)
    assert urls == ["https://example.com/acme"]
    for bad in ("cdn.", "fonts.googleapis", "<script", "fetch(", "@import"):
        assert bad not in out


def test_render_scales_lane_width_by_round_count(tmp_path):
    """A short loop must not draw as wide as a long one."""
    db = make_db(tmp_path)
    short = add(db, "short")
    long_ = add(db, "long")
    db.set_rounds(short, ["a", "b"])
    db.set_rounds(long_, ["a", "b", "c", "d", "e"])
    out = board.render(db.interviewing())
    # fixed-width connectors, never flex-grow, is what makes that true
    assert "flex:0 0 46px" in out
    assert "flex:1" not in out.split("</style>")[0].split(".link")[1][:80]


def test_accent_resolves_on_the_lane_not_the_root(tmp_path):
    """Regression: --accent:var(--a) on :root computes against an undefined
    variable, is guaranteed-invalid, and inherits down dead, which silently
    renders every lane monochrome (no accent border, no filled nodes, no
    connector lines). --a only exists on .lane, so --accent must too."""
    css = board.CSS
    for selector, body in re.findall(r'(:root[^{]*)\{([^}]*)\}', css):
        if ".lane" in selector:
            continue   # ":root[data-theme=dark] .lane" is the correct form
        assert "--accent" not in body, \
            f"--accent must not be declared on {selector.strip()!r}"
    assert ".lane{--accent:var(--a);}" in css


def test_lane_carries_both_accent_values(tmp_path):
    db = make_db(tmp_path)
    uid = add(db, "acme")
    db.set_stage(uid, at=1)
    out = board.render(db.interviewing())
    light, dark = board.ACCENTS[0]
    assert f"--a:{light}" in out and f"--a-dark:{dark}" in out


def test_display_company():
    assert board.display_company("vertex-analytics") == "Vertex Analytics"
    assert board.display_company("globex") == "Globex"
    # already capitalised names are left exactly as they are
    assert board.display_company("UmbraLabs") == "UmbraLabs"
    assert board.display_company("SoyLent") == "SoyLent"
    assert board.display_company("") == ""
    assert board.display_company(None) == ""


def test_days_since_and_age_label():
    now = datetime.now(timezone.utc)
    assert board.days_since(None) is None
    assert board.days_since("not a date") is None
    assert board.age_label(now.isoformat()) == "today"
    assert board.age_label((now - timedelta(days=3)).isoformat()) == "3d"


def test_write_creates_the_file(tmp_path):
    db = make_db(tmp_path)
    uid = add(db, "acme")
    db.set_stage(uid, at=1)
    p = board.write(db.interviewing(), tmp_path / "sub" / "interviews.html")
    assert p.exists()
    assert "Interviews" in p.read_text()


# --- the CLI reads the same frame the board draws --------------------------

def test_cli_stage_reports_the_derived_caption(tmp_path, capsys):
    """`jh stage <ident> N` names the slot the board draws at position N.

    The CLI used to echo the stored label, so on the seeded frame `jh stage
    <ident> 2` answered "at round 1" for the node the board captions "round 1"
    in position 2. Both surfaces now derive the caption from position, so
    there is only one numbering in the product.
    """
    import argparse
    import job_cli

    db = make_db(tmp_path)
    uid = add(db, "acme")
    args = argparse.Namespace(ident="acme", round="2", next=None, on=None)
    job_cli.cmd_stage(db, args)
    assert "at round 1" in capsys.readouterr().out
    assert db.get(uid)["interview_at"] == 2
    db.close()


def test_cli_stage_reports_caption_and_the_real_label(tmp_path, capsys):
    db = make_db(tmp_path)
    uid = add(db, "vertex")
    db.set_rounds(uid, ["recruiter", "hiring manager", "peer + TPM"])
    import argparse
    import job_cli
    args = argparse.Namespace(ident="vertex", round="3", next=None, on=None)
    job_cli.cmd_stage(db, args)
    out = capsys.readouterr().out
    assert "at round 2 (peer + TPM)" in out
    db.close()


def test_the_audit_note_pins_down_which_slot_moved(tmp_path):
    """Three stagings of the seeded frame must not read identically.

    The placeholders are numberless now, so `stage: round` alone cannot say
    which of positions 2, 3 and 4 moved, and the row keeps only the CURRENT
    marker. The note carries the position so a past entry stays reconstructible
    from the log rather than from the row.
    """
    db = make_db(tmp_path)
    uid = add(db, "acme")
    db.set_stage(uid, at=2)
    db.set_stage(uid, at=3)
    notes = [h["note"] for h in db.history(uid) if (h["note"] or "").startswith("stage:")]
    assert len(notes) == len(set(notes))
    assert any("position 2" in n for n in notes)
    assert any("position 3" in n for n in notes)
    db.close()


def test_cli_rounds_never_prints_a_number_that_fights_its_position(tmp_path, capsys):
    """`jh rounds` is the third surface, and it had no migration.

    Every job already staged in production carries the old "round 1".."round 3"
    seed, so listing them printed "2. round 1": the position and the label
    naming two different numbers on one line. The position is what `jh stage`
    takes, so it stays; the placeholder is suppressed rather than renumbered.
    """
    import argparse
    import job_cli

    db = make_db(tmp_path)
    uid = add(db, "legacy")
    # exactly what a pre-existing production row looks like
    db.set_rounds(uid, ["recruiter", "round 1", "round 2", "hiring manager"])
    job_cli.cmd_rounds(db, argparse.Namespace(ident="legacy", labels=None, add=None))
    out = capsys.readouterr().out
    assert "round 1" not in out and "round 2" not in out
    assert "1. recruiter" in out
    assert "4. hiring manager" in out
    assert out.count("(unnamed)") == 2
    db.close()


def test_cli_rounds_hint_shows_no_placeholder_labels(tmp_path, capsys):
    import argparse
    import job_cli
    db = make_db(tmp_path)
    add(db, "fresh")
    job_cli.cmd_rounds(db, argparse.Namespace(ident="fresh", labels=None, add=None))
    out = capsys.readouterr().out
    assert "no rounds set" in out
    assert "round 1" not in out
    db.close()

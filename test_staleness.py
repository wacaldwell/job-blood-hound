"""Tests for staleness.py, the 'how long since the human acted' clock."""

from datetime import datetime, timedelta, timezone

import staleness as st

NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)


def ago(days):
    """ISO timestamp `days` before NOW."""
    return (NOW - timedelta(days=days)).isoformat()


def test_idle_days_counts_whole_and_partial_days():
    assert st.idle_days(ago(3), now=NOW) == 3.0
    assert round(st.idle_days(ago(0.5), now=NOW), 2) == 0.5


def test_idle_days_is_none_when_undatable():
    assert st.idle_days(None, now=NOW) is None
    assert st.idle_days("", now=NOW) is None
    assert st.idle_days("not a timestamp", now=NOW) is None


def test_the_seven_day_boundary():
    assert st.is_stale("drafted", ago(6.9), now=NOW) is False
    assert st.is_stale("drafted", ago(7.1), now=NOW) is True


def test_every_committed_state_can_go_stale():
    for state in ("queued", "drafted", "ready", "interviewing"):
        assert st.is_stale(state, ago(30), now=NOW) is True, state


def test_uncommitted_states_never_go_stale():
    # discovered has cost nothing yet; the terminal states are done. Warning
    # about either is noise that trains the human to ignore the signal.
    for state in ("discovered", "applied", "skipped", "closed"):
        assert st.is_stale(state, ago(365), now=NOW) is False, state


def test_bad_data_is_silent_rather_than_alarming():
    # A missing or unreadable clock must never manufacture a warning.
    assert st.is_stale("drafted", None, now=NOW) is False
    assert st.is_stale("drafted", "garbage", now=NOW) is False
    assert st.is_stale(None, ago(30), now=NOW) is False


def test_label_reads_as_days_or_is_none():
    assert st.staleness_label("drafted", ago(24), now=NOW) == "idle 24d"
    assert st.staleness_label("drafted", ago(2), now=NOW) is None
    assert st.staleness_label("discovered", ago(99), now=NOW) is None
    # None, not "", so callers can tell "fresh" from "stale but unlabelable".
    assert st.staleness_label("drafted", None, now=NOW) is None


def test_future_timestamps_are_not_stale():
    # Clock skew between hosts should not read as negative idle time.
    future = (NOW + timedelta(days=2)).isoformat()
    assert st.is_stale("drafted", future, now=NOW) is False

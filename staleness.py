#!/usr/bin/env python3
"""
staleness.py - how long since the operator acted on a lead.

Sibling to freshness.py, and deliberately a different question. freshness.py
asks how old a POSTING is. This asks how long a lead the operator already
committed to has sat without anything being done about it.

The motivating bug: one lead (score 93) reached `drafted` on 2026-07-02 with
generated documents and was never submitted. Nothing surfaced it for 24 days.
A generated package that is never sent is the most wasteful state in the
pipeline, because the tailoring cost was paid and no application resulted.

The clock is state_log, never jobs.updated_at: the nightly scoring pass bumps
updated_at without changing state, so it would make every lead look
permanently fresh. Gate runs and read stamps are excluded by the caller
(jobdb.last_activity) because neither is a decision about the lead; counting
them would let a lead quiet its own alarm.

Pure and import-safe: no network, no database, no file writes.
"""

from datetime import datetime, timezone

import freshness as fr

# Only a lead the operator has committed to can go stale. `discovered` has cost
# nothing yet, and the terminal states are done.
#
# DUPLICATED, ON PURPOSE, IN ANOTHER REPO. The web inbox carries both
# constants: COMMITTED_STATES in its lib/job-sort.ts, STALE_AFTER_DAYS in its
# lib/job-format.ts. Separate git repos deployed to the same host
# against one shared database, with no build step between them, so nothing
# generates one from the other and no test on either side can catch a drift.
# These are the ONLY definitions on this side: read them from here, never
# re-spell either value at a call site.
#
# What drift costs: raise the threshold here alone and the CLI goes quiet on a
# lead the web inbox table still flags; edit the state set on one side
# alone and the two surfaces disagree about which leads are exempt from the
# freshness filter, which is the exact bug class this feature already hit six
# times. Change either value in both repos in the same sitting, or not at all.
COMMITTED_STATES = {"queued", "drafted", "ready", "interviewing"}

STALE_AFTER_DAYS = 7


def idle_days(last_activity_at, now=None):
    """Days since the last real action, or None if undatable.

    Reuses freshness.parse_iso so both clocks read timestamps the same way.
    """
    dt = fr.parse_iso(last_activity_at)
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - dt).total_seconds() / 86400.0


def is_stale(state, last_activity_at, now=None):
    """True when a committed lead has sat untouched past the threshold.

    Every uncertain path returns False. A warning that cannot be trusted is
    worse than no warning, because it trains the reader to skip the section.
    """
    if state not in COMMITTED_STATES:
        return False
    days = idle_days(last_activity_at, now=now)
    if days is None:
        return False
    return days >= STALE_AFTER_DAYS


def staleness_label(state, last_activity_at, now=None):
    """'idle 24d' for a stale lead, else None.

    None rather than an empty string so a caller can distinguish "fresh" from
    "stale but unlabelable" without a second call.

    The threshold check is inlined rather than delegated to is_stale so the
    clock is read once per label. Calling is_stale and then idle_days parses
    the timestamp and subtracts twice for one line of output, and the two
    reads could in principle straddle a `now` default computed a moment apart.
    Any change here has to keep the two functions agreeing on the boundary.
    """
    if state not in COMMITTED_STATES:
        return None
    days = idle_days(last_activity_at, now=now)
    if days is None or days < STALE_AFTER_DAYS:
        return None
    return f"idle {int(days)}d"

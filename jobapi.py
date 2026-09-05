#!/usr/bin/env python3
"""
jobapi.py - the local write API for the job pipeline.

The web inbox opens jobs.db read-only and cannot write it. This service is
how that inbox records the three things a human does while triaging: a
vote, a note, and a lifecycle transition. Every endpoint is a thin wrapper
around an audited jobdb.py setter, so jobdb.py stays the only writer and the
state machine lives in exactly one language.

Bound to localhost, bearer token from JOB_API_TOKEN. Not exposed off the host.

HARD RULE: nothing here submits an application, fills an external form, or
logs into a job site. Stamping 'applied' is a state write only, the same thing
the MCP server's job_apply already does. Do not add an endpoint that crosses
that line.
"""
import os

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

import jobdb


def _db():
    path = os.environ.get("JOB_DB")
    if not path:
        raise HTTPException(503, "JOB_DB is not configured")
    db = jobdb.JobDB(path)
    try:
        yield db
    finally:
        db.close()


def _auth(authorization: str = Header(default="")):
    token = os.environ.get("JOB_API_TOKEN", "")
    if not token:
        raise HTTPException(503, "JOB_API_TOKEN is not configured")
    if authorization != f"Bearer {token}":
        raise HTTPException(401, "bad or missing bearer token")


app = FastAPI(title="job-hound write API", dependencies=[Depends(_auth)])


def _resolve(db, ident):
    """Accept a uid, a slug, or a unique slug prefix, exactly like bin/jh."""
    try:
        row = db.resolve(ident)
    except jobdb.TransitionError as e:   # an ambiguous prefix
        raise HTTPException(400, str(e))
    if not row:
        raise HTTPException(404, f"no job matching '{ident}'")
    return row


def _payload(row):
    """The fields the inbox re-renders after a write. Not the whole row."""
    return {
        "uid": row["uid"],
        "slug": row["slug"],
        "state": row["state"],
        "vote": row["vote"],
        "vote_note": row["vote_note"],
        "notes": row["notes"],
        "read_at": row["read_at"],
        "updated_at": row["updated_at"],
    }


class VoteIn(BaseModel):
    vote: str | None = None
    note: str | None = None


# A vote note is the one-line reason attached to a vote, not a working note.
# This API is the only thing that writes it, so the cap is enforced here.
# Truncated rather than rejected, the same way jobdb caps the longer notes
# field, so a long paste loses its tail instead of losing the vote.
VOTE_NOTE_MAX = 280


@app.post("/jobs/{ident}/vote")
def post_vote(ident: str, body: VoteIn, db=Depends(_db)):
    row = _resolve(db, ident)
    note = body.note[:VOTE_NOTE_MAX] if body.note else body.note
    try:
        return _payload(db.set_vote(row["uid"], body.vote, note=note))
    except ValueError as e:
        raise HTTPException(400, str(e))


class NoteIn(BaseModel):
    text: str = ""


@app.post("/jobs/{ident}/note")
def post_note(ident: str, body: NoteIn, db=Depends(_db)):
    row = _resolve(db, ident)
    return _payload(db.set_notes(row["uid"], body.text))


class ReadIn(BaseModel):
    read: bool = True


@app.post("/jobs/{ident}/read")
def post_read(ident: str, body: ReadIn, db=Depends(_db)):
    row = _resolve(db, ident)
    return _payload(db.set_read(row["uid"], read=body.read))


class StateIn(BaseModel):
    state: str
    note: str | None = None
    outcome: str | None = None
    reason: str | None = None


@app.post("/jobs/{ident}/state")
def post_state(ident: str, body: StateIn, db=Depends(_db)):
    """Advance a lead through the lifecycle.

    An unknown state is a bad request; a known state that is not reachable
    from here is a conflict, and the message from TransitionError travels to
    the UI verbatim so it can say exactly what is not allowed.
    """
    if body.state not in jobdb.STATES:
        raise HTTPException(400, f"unknown state: {body.state}")
    # jobdb raises for this too, but as a TransitionError, which this endpoint
    # maps to a 409. A reason sent for a state that has no column is a
    # malformed request rather than a refused transition, so it is caught here
    # and answered 400. Silently dropping a string the operator typed into a
    # mandatory field would be worse than either.
    if body.reason and body.state not in jobdb.REASON_COLUMN:
        raise HTTPException(
            400,
            f"reason is only accepted for {sorted(jobdb.REASON_COLUMN)}, not "
            f"'{body.state}'. Use note, which is audited for every state.")
    # Same reasoning for outcome, which only a close records. It matters more
    # than it reads: the reopen guard decides from `outcome`, so a live row
    # left saying 'ghosted' would stay reopenable forever.
    if body.outcome and body.state != "closed":
        raise HTTPException(
            400,
            f"outcome is only accepted for 'closed', not '{body.state}'.")
    row = _resolve(db, ident)
    # Reposting the state a lead is already in is deliberately a 200, not a
    # 409: jobdb.set_state returns early on a no-op instead of consulting
    # TRANSITIONS. That makes the endpoint safe to retry after a lost
    # response, which matters more here than reporting a state that never
    # actually changed.
    try:
        updated = db.set_state(row["uid"], body.state, note=body.note,
                               outcome=body.outcome, reason=body.reason)
    except jobdb.TransitionError as e:
        raise HTTPException(409, str(e))
    # Advancing state is an explicit disposition, so the lead leaves the
    # unread queue in the same call. Requiring a separate keypress afterwards
    # would leave queued leads sitting in the inbox, which is the bug this
    # whole surface exists to fix.
    return _payload(db.set_read(updated["uid"], read=True))


@app.get("/jobs/{ident}/transitions")
def get_transitions(ident: str, db=Depends(_db)):
    """The legal next states for this lead.

    This endpoint is why the inbox is an API client and not a spool writer:
    the UI never holds a copy of TRANSITIONS. The state machine stays in one
    file, in one language.

    A closed lead is the one row whose answer depends on more than its state:
    only a `ghosted` close can reopen, so next_states reads the outcome too and
    a rejected lead still offers nothing.

    Legal is not the same as appropriate. This returns the true state machine,
    so a queued lead lists 'drafted' and a drafted one lists 'ready'. Both of
    those mean documents exist on disk: 'drafted' is stamped by the generation
    path in job_cli.py and job_ingest.py, and 'ready' says the operator reviewed that
    package. Neither is a triage decision, so the inbox should offer only
    'queued', 'skipped', and 'discovered'. The rest belong to the draft
    pipeline.
    """
    row = _resolve(db, ident)
    return {"state": row["state"], "next": jobdb.next_states(row)}

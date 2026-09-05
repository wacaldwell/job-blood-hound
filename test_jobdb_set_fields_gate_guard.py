"""set_fields is a generic UPDATE with no allowlist and no audit trail. Every
other gate write in jobdb.py (set_gate, set_override) is deliberately narrow
and logged to state_log. A reviewer grepped every call site and confirmed
none pass a gate column today, but the day a future MCP tool or CLI command
threads a user-supplied field name into set_fields, it becomes an instant,
unaudited gate_decision = 'PROCEED' bypass. set_fields must refuse to write
any gate column, full stop.
"""
import pytest

import jobdb


def _db(tmp_path):
    db = jobdb.JobDB(tmp_path / "t.db")
    db.upsert_job({"ats": "greenhouse", "company": "acme", "id": "1",
                   "title": "Senior SRE", "location": "Remote"})
    return db, db.get(jobdb.make_job_uid("greenhouse", "acme", "1"))


def test_set_fields_refuses_to_write_gate_columns(tmp_path):
    """set_fields is a generic UPDATE with no audit trail. If it could write
    gate_decision, that would be an unaudited PROCEED, which is the one thing
    this whole gate exists to prevent."""
    db, row = _db(tmp_path)
    with pytest.raises(ValueError, match="gate"):
        db.set_fields(row["uid"], gate_decision="PROCEED")
    with pytest.raises(ValueError):
        db.set_fields(row["uid"], gate_override_reason="nah it's fine")
    # And the legitimate use still works.
    db.set_fields(row["uid"], notes="a normal field")
    assert db.get(row["uid"])["notes"] == "a normal field"

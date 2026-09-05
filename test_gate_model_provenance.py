import json
import gate
from jobdb import JobDB


def test_backfill_stamps_existing_gated_rows(tmp_path):
    db = JobDB(str(tmp_path / "t.db"))
    db.upsert_job({"ats": "manual", "company": "acme", "id": "x1",
                   "title": "Platform Engineer", "location": "Remote", "url": ""})
    uid = db.resolve("acme")["uid"]
    # Simulate a pre-provenance gated row: gate_decision set, gate_model absent.
    db.set_gate(uid, "PROCEED", json.dumps({"requirements": []}), "/tmp/r.md")
    db.conn.execute("UPDATE jobs SET gate_model = NULL WHERE uid = ?", (uid,))
    db.conn.commit()

    # Re-open triggers _migrate, which backfills.
    db2 = JobDB(str(tmp_path / "t.db"))
    row = db2.get(uid)
    assert row["gate_model"] == "claude-opus-4-8"


def test_set_gate_records_model(tmp_path):
    db = JobDB(str(tmp_path / "t.db"))
    db.upsert_job({"ats": "manual", "company": "beta", "id": "y1",
                   "title": "SRE", "location": "Remote", "url": ""})
    uid = db.resolve("beta")["uid"]
    db.set_gate(uid, "PROCEED", json.dumps({"requirements": []}), "/tmp/r.md",
                model="kimi-k2-0711-preview")
    assert db.get(uid)["gate_model"] == "kimi-k2-0711-preview"


def test_recompute_preserves_backfilled_gate_model(tmp_path, monkeypatch):
    """A backfilled pre-provenance row has gate_model set on the column but
    no "model" key in gate_json (the migration only touched the column).
    recompute() (the gate-rule path) must not erase that provenance by
    persisting model=None just because gate_json has nothing to recover.
    """
    monkeypatch.setenv("JOB_APPS_DIR", str(tmp_path / "apps"))
    db = JobDB(str(tmp_path / "t.db"))
    db.upsert_job({"ats": "manual", "company": "acme", "id": "x1",
                   "title": "Platform Engineer", "location": "Remote", "url": ""})
    uid = db.resolve("acme")["uid"]

    # Simulate the backfilled state directly: gate_json has no "model" key,
    # but gate_model was stamped onto the column by the migration.
    db.set_gate(uid, "PROCEED", json.dumps({"requirements": []}), "/tmp/r.md",
                model=None)
    db.conn.execute("UPDATE jobs SET gate_model = ? WHERE uid = ?",
                     ("claude-opus-4-8", uid))
    db.conn.commit()

    row = db.get(uid)
    assert "model" not in json.loads(row["gate_json"])
    assert row["gate_model"] == "claude-opus-4-8"

    gate.recompute(db, row)

    after = db.get(uid)
    assert after["gate_model"] == "claude-opus-4-8", (
        "recompute erased the backfilled gate_model provenance")

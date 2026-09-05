#!/usr/bin/env python3
"""
job_hound_mcp.py - Expose the job-hound pipeline to an MCP-speaking agent.

This is the surface a chat agent loads so the whole search can be driven
from Discord: discover, triage, draft, and track, all as structured tool calls
over the same SQLite system of record the CLI uses. It is a thin adapter, not a
second implementation: every tool calls the existing stage code (jobdb,
job_cli.scan_and_ingest / refine_pipeline, job_generate.generate) and returns
plain JSON-serializable dicts.

Hard rule, enforced by the surface itself: there is NO tool that submits an
application, fills a form, or logs into a job site. `job_apply` only stamps the
state to 'applied' after a human has applied by hand. Discovery and prep only.

The tool functions are plain, typed, documented module-level functions so they
are unit-testable without the `mcp` package installed or a server running.
`build_server()` is the only thing that imports FastMCP and wires them up.

Run (on the host where jobs.db lives, next to the agent):
    pip install mcp        # the official Python MCP SDK
    JOB_DB=~/job-hound/jobs.db python job_hound_mcp.py

Environment (shared with the CLI; see CLAUDE.md):
    JOB_DB, JOB_APPS_DIR, JOB_MODEL, ANTHROPIC_API_KEY, JOB_PDF
    JOB_CONFIG   scan config (default ./companies.yaml)
    JOB_PROFILE  fit profile (default profile.yaml)
    JOB_MASTER   master resume (default master_resume.yaml)
"""

import os
from pathlib import Path
from typing import Optional

import yaml

import gate
import job_cli as jc
import fit
import freshness as fr
from jobdb import JobDB, TransitionError, STATES, OUTCOMES

HERE = Path(__file__).resolve().parent


# --- shared helpers -------------------------------------------------------

def _open() -> JobDB:
    """Open the system of record. A fresh connection per call keeps the tools
    safe under whatever threading the MCP server uses (mirrors the CLI, which
    opens one connection per command)."""
    return JobDB(jc.resolve_db_path(None))


def _cfg_path() -> str:
    return os.environ.get("JOB_CONFIG") or str(HERE / "companies.yaml")


def _master_path() -> Optional[str]:
    return os.environ.get("JOB_MASTER") or None


def _profile_path() -> Optional[str]:
    return os.environ.get("JOB_PROFILE") or None


def _load_master() -> dict:
    mp = _master_path() or str(HERE / "master_resume.yaml")
    return yaml.safe_load(Path(mp).read_text())


def _age(row) -> str:
    try:
        return fr.freshness_label(row["posted_at"], row["date_source"])
    except (KeyError, IndexError, TypeError):
        return "age unknown"


def _brief(row) -> dict:
    """Compact one-line view of a job, for lists and digests."""
    r = dict(row)
    return {
        "slug": r["slug"],
        "title": r["title"],
        "company": r["company"],
        "location": r["location"] or None,
        "state": r["state"],
        "fit_score": r["fit_score"],
        "llm_fit_score": r["llm_fit_score"],
        "age": _age(r),
        "url": r["url"],
    }


def _full(db, row) -> dict:
    """Detailed view of one job: fields, files, and state history."""
    r = dict(row)
    out = {
        "slug": r["slug"],
        "uid": r["uid"],
        "title": r["title"],
        "company": r["company"],
        "ats": r["ats"],
        "location": r["location"] or None,
        "state": r["state"],
        "outcome": r["outcome"],
        "age": _age(row),
        "url": r["url"],
        "folder": r["folder"],
        "fit_score": r["fit_score"],
        "fit_reasons": r["fit_reasons"],
        "llm_fit_score": r["llm_fit_score"],
        "llm_rationale": r["llm_rationale"],
        "llm_coding_bar": r["llm_coding_bar"],
        "skip_reason": r["skip_reason"],
        "close_reason": r["close_reason"],
        "files": [
            {"kind": f["kind"], "version": f["version"], "path": f["path"]}
            for f in db.files_for(r["uid"])
        ],
        "history": [
            {"at": h["at"], "from": h["from_state"], "to": h["to_state"],
             "note": h["note"]}
            for h in db.history(r["uid"])
        ],
    }
    return out


def _resolve(db, ident: str):
    """Return (row, None) on success, or (None, error_dict) the agent can relay."""
    try:
        row = db.resolve(ident)
    except TransitionError as e:  # ambiguous prefix
        return None, {"error": str(e)}
    if not row:
        return None, {"error": f"No job matching '{ident}'."}
    return row, None


def _transition(db, ident: str, to_state: str, note=None, outcome=None,
                fields=None) -> dict:
    row, err = _resolve(db, ident)
    if err:
        return err
    try:
        updated = db.set_state(row["uid"], to_state, note=note, outcome=outcome)
    except TransitionError as e:
        return {"error": str(e)}
    if fields:
        db.set_fields(row["uid"], **fields)
    return {"ok": True, "slug": row["slug"],
            "from": row["state"], "to": updated["state"]}


# --- read tools -----------------------------------------------------------

def job_stats() -> dict:
    """Pipeline counts by state, plus the total. Use this for a quick overview
    of where everything stands."""
    db = _open()
    try:
        counts = db.counts()
        return {"total": sum(counts.values()), "by_state": counts}
    finally:
        db.close()


def job_list(state: Optional[str] = None, max_age_hours: Optional[float] = None,
             include_all: bool = False, limit: Optional[int] = None) -> dict:
    """List jobs in the pipeline, ranked by fit (best first).

    By default this hides postings older than 30 days and onsite-metro leads
    held for a location check, matching the CLI's default view. A lead already
    committed to (queued, drafted, ready, interviewing) is exempt from the age
    cut and always listed, however old its posting. It is exempt from `limit`
    the same way: `limit` is a budget on discoveries, so the returned list can
    exceed it by the number of committed leads. Set
    include_all=True to show everything, or state to filter to one lifecycle
    state (discovered, queued, drafted, ready, applied, interviewing, closed,
    skipped).
    """
    db = _open()
    try:
        max_age = max_age_hours if max_age_hours is not None else jc.DEFAULT_MAX_AGE_HOURS
        rows = db.list(state=state, limit=None)
        rows, verify_hidden = jc._verify_filter(rows, include_all)
        rows, hidden = jc._fresh_filter(rows, max_age, include_all)
        rows = sorted(rows, key=lambda r: fit.sort_key(dict(r)), reverse=True)
        rows = jc._apply_limit(rows, limit)
        return {
            "jobs": [_brief(r) for r in rows],
            "shown": len(rows),
            "hidden_by_age": hidden,
            "hidden_location_check": verify_hidden,
        }
    finally:
        db.close()


def job_show(ident: str) -> dict:
    """Full detail for one job: fields, generated files, and state history.
    `ident` is a slug, a unique slug prefix, or the full uid."""
    db = _open()
    try:
        row, err = _resolve(db, ident)
        if err:
            return err
        return _full(db, row)
    finally:
        db.close()


def job_next() -> dict:
    """The next job for the human to submit (oldest 'ready' first). Falls back
    to the next queued job awaiting a draft when nothing is ready."""
    db = _open()
    try:
        r = db.next_to_apply()
        if r:
            return {"action": "apply", "job": _brief(r),
                    "folder": r["folder"], "url": r["url"]}
        nd = db.next_to_draft()
        if nd:
            return {"action": "draft", "job": _brief(nd)}
        return {"action": "none",
                "message": "Queue is empty. Run a scan to find roles, "
                           "then queue one to pursue."}
    finally:
        db.close()


# --- discovery / generation ----------------------------------------------

def job_scan() -> dict:
    """Run a discovery scan against the configured public ATS APIs and ingest
    new postings as 'discovered'. Returns how many matched, how many were newly
    added, and any companies that need manual checking (no public feed)."""
    db = _open()
    try:
        try:
            cfg = jc.load_cfg(_cfg_path())
        except SystemExit as e:
            # load_cfg exits the process on a missing config (right for the CLI,
            # fatal for a long-running server); turn it into a clean error.
            return {"error": str(e) or f"config not found: {_cfg_path()}"}
        r = jc.scan_and_ingest(db, cfg, verbose=False)
        return {
            "matches": r["matches"],
            "added": r["added"],
            "dates_upgraded": r["upgraded"],
            "location_held": r.get("location_held", 0),
            "manual": [
                {"name": m["name"], "ats": m["ats"],
                 "careers_url": m.get("careers_url")}
                for m in r["manual"]
            ],
        }
    finally:
        db.close()


def job_refine(top: int = jc.DEFAULT_LLM_TOP, no_llm: bool = False,
               max_age_hours: Optional[float] = None,
               include_all: bool = False) -> dict:
    """Score and rank the active pipeline and return the ranked digest text
    (relay it to the user; do not also push it - the daily cron owns the
    Discord webhook). Runs an LLM fit verdict on the fresh top-N unless
    no_llm=True. `top` bounds LLM calls (API spend) per pass."""
    db = _open()
    try:
        try:
            profile = fit.load_profile(_profile_path())
            mp = _master_path() or str(HERE / "master_resume.yaml")
            master = yaml.safe_load(Path(mp).read_text())
        except (FileNotFoundError, OSError) as e:
            return {"error": f"config file missing for refine: {e}"}
        api_key = None if no_llm else os.environ.get("ANTHROPIC_API_KEY")
        max_age = max_age_hours if max_age_hours is not None else jc.DEFAULT_MAX_AGE_HOURS
        r = jc.refine_pipeline(db, profile=profile, master=master, top=top,
                               no_llm=no_llm, max_age=max_age,
                               show_all=include_all, api_key=api_key)
        if r["active"] == 0:
            return {"digest": None, "message": "No active leads to refine."}
        return {
            "digest": r["digest"],
            "active": r["active"],
            "hidden_by_age": r["hidden"],
            "hidden_location_check": r["verify_hidden"],
            "verdict_failures": r["verdict_failures"],
            "llm_used": bool(api_key),
        }
    finally:
        db.close()


def job_draft(ident: str) -> dict:
    """Generate a tailored resume + cover letter package for a queued (or
    already-drafted) job and move it to 'drafted'. Returns the package folder,
    the file paths, and the tailoring note (which calls out gaps honestly).
    This NEVER submits anything; it only produces documents for human review.
    The files live on this host - retrieve them to apply by hand."""
    db = _open()
    try:
        row, err = _resolve(db, ident)
        if err:
            return err
        if row["state"] not in ("queued", "drafted"):
            return {"error": f"{row['slug']} is '{row['state']}'. "
                             f"Queue it first with job_queue."}
        import job_generate
        try:
            result = job_generate.generate(db, row, master_path=_master_path())
        except Exception as e:
            return {"error": f"draft failed: {e}"}
        # Generation succeeded and wrote files to disk. Advance state separately
        # so a transition error is reported distinctly, never as "draft failed".
        if row["state"] == "queued":
            try:
                db.set_state(row["uid"], "drafted",
                             note=f"generated v{result['version']}")
            except TransitionError as e:
                return {"error": f"package generated but state not advanced: {e}",
                        "folder": result["folder"]}
        return {
            "ok": True,
            "slug": row["slug"],
            "version": result["version"],
            "folder": result["folder"],
            "files": {k: str(v) for k, v in result["files"].items()},
            "pdf": result["pdf"],
            "tailoring_note": result["tailoring_note"],
            "state": "drafted",
        }
    finally:
        db.close()


def job_gate(ident: str) -> dict:
    """Run (or re-run) the fit gate against a posting's full job description,
    exactly as `jh gate` does on the CLI. Returns the decision (PROCEED,
    CONDITIONAL, NEEDS_REVIEW, DO_NOT_APPLY, or ERROR), the requirement counts,
    and the path to the written fit report. This never bypasses a block: there
    is no override tool here by design. If a job comes back blocked, relay
    that to the operator and tell them to use the CLI (jh gate-rule / jh gate-override)."""
    db = _open()
    try:
        row, err = _resolve(db, ident)
        if err:
            return err
        out = gate.run_gate(db, row, _load_master())
        return {
            "slug": row["slug"],
            "decision": out["decision"],
            "counts": out["counts"],
            "report_path": str(out["report_path"]),
        }
    finally:
        db.close()


# --- lifecycle (state stamps only; never submits) -------------------------

def job_queue(ident: str, note: Optional[str] = None) -> dict:
    """Mark a job as one to pursue (discovered -> queued), then run the fit
    gate against the full job description, exactly as `jh queue` does on the
    CLI. Returns the transition result plus the gate decision, counts, and
    report path, so a blocked job is visible immediately from Discord."""
    db = _open()
    try:
        res = _transition(db, ident, "queued", note=note)
        if "error" in res:
            return res
        row, err = _resolve(db, ident)
        if err:
            return err
        out = gate.run_gate(db, row, _load_master())
        res["gate_decision"] = out["decision"]
        res["gate_counts"] = out["counts"]
        res["gate_report_path"] = str(out["report_path"])
        return res
    finally:
        db.close()


def job_ready(ident: str, note: Optional[str] = None) -> dict:
    """Mark a drafted package as reviewed and ready to submit (drafted -> ready)."""
    db = _open()
    try:
        return _transition(db, ident, "ready", note=note)
    finally:
        db.close()


def job_apply(ident: str, note: Optional[str] = None) -> dict:
    """Record that the HUMAN has submitted this application (ready -> applied,
    date stamped). This only stamps state; it does not and cannot submit
    anything itself."""
    db = _open()
    try:
        return _transition(db, ident, "applied", note=note)
    finally:
        db.close()


def job_skip(ident: str, reason: Optional[str] = None,
             note: Optional[str] = None) -> dict:
    """Drop a job from consideration (-> skipped). `reason` is stored and feeds
    the fit ranking so similar roles rank lower later."""
    db = _open()
    try:
        fields = {"skip_reason": reason} if reason else None
        return _transition(db, ident, "skipped", note=note, fields=fields)
    finally:
        db.close()


def job_close(ident: str, outcome: str, reason: Optional[str] = None,
              note: Optional[str] = None) -> dict:
    """Close a job with a terminal outcome (rejected, withdrawn, offer,
    accepted, ghosted, other). `reason` is stored and feeds fit ranking."""
    db = _open()
    try:
        if outcome not in OUTCOMES:
            return {"error": f"unknown outcome '{outcome}'; "
                             f"choose one of {OUTCOMES}."}
        fields = {"close_reason": reason} if reason else None
        return _transition(db, ident, "closed", note=note,
                           outcome=outcome, fields=fields)
    finally:
        db.close()


def job_state(ident: str, state: str, note: Optional[str] = None) -> dict:
    """Set a job's state directly (validated against the state machine). Prefer
    the named verbs (job_queue/ready/apply/skip/close); use this only for moves
    they don't cover, like un-skipping or stepping back."""
    db = _open()
    try:
        if state not in STATES:
            return {"error": f"unknown state '{state}'; choose one of {STATES}."}
        return _transition(db, ident, state, note=note)
    finally:
        db.close()


# --- server wiring --------------------------------------------------------

TOOLS = [
    job_stats, job_list, job_show, job_next,
    job_scan, job_refine, job_draft,
    job_gate, job_queue, job_ready, job_apply, job_skip, job_close, job_state,
]


def build_server():
    """Wire the tools into a FastMCP server. Imported lazily so the module
    itself (and its tests) load without the `mcp` package present."""
    from mcp.server.fastmcp import FastMCP
    server = FastMCP("job-hound")
    for fn in TOOLS:
        server.tool()(fn)
    return server


def main():
    build_server().run()


if __name__ == "__main__":
    main()

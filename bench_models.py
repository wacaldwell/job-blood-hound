#!/usr/bin/env python3
"""Compare gate decisions across models on already-evaluated jobs.

READ-ONLY on job data. Runs the gate's own decision logic (extract -> enforce ->
decide + location overlay) against the stored JD for each model, and reports
where the models disagree. Never writes a job's gate_decision; the only output is
a markdown report. The operator adjudicates disagreements.

Usage:
    python bench_models.py --models claude-opus-4-8,kimi-k2.6 [ident ...]
With no idents, benches every row that already has a gate_decision.
"""
import argparse
import os
import sys
from pathlib import Path

import yaml

import gate
import llm
from jobdb import JobDB, DBPathError, TransitionError, resolve_db_path


def _provider_for(model_name):
    """Resolve a provider whose model is model_name. Picks the registry provider
    whose default model shares the family prefix, then overrides the model."""
    name = "kimi" if model_name.startswith("kimi") else "anthropic"
    prov = llm.resolve_provider(name)
    return prov._replace(model=model_name)


def evaluate_job(job_row, jd_text, master, provider, call=None):
    """Run the gate decision path once for one model. No persistence."""
    caps_dnc = gate.load_profile(master)
    _caps, dnc = caps_dnc
    evidence = gate.build_evidence(master)

    def _default_call(system, user, api_key):
        return llm.call_messages(system, user, max_tokens=gate.GATE_MAX_TOKENS,
                                 provider=provider, raise_on_truncation=True,
                                 component="benchmark")

    call = call or _default_call
    raw = gate.extract(dict(job_row), jd_text, evidence, provider.api_key, call=call)
    reqs = gate.enforce(raw, dnc)
    skills_decision = gate.decide(reqs)
    loc = gate.location_ok(dict(job_row), jd_text)
    decision = gate._apply_location(skills_decision, loc)
    return {"decision": decision, "skills_decision": skills_decision,
            "counts": gate.counts(reqs)}


def run_bench(db, idents, model_names, master):
    if idents:
        rows = []
        for i in idents:
            try:
                row = db.resolve(i)
            except TransitionError as e:
                print(f"bench: skipping ambiguous ident '{i}': {e}", file=sys.stderr)
                continue
            if row is None:
                print(f"bench: skipping unknown ident '{i}'", file=sys.stderr)
                continue
            rows.append(row)
    else:
        rows = [r for r in db.list() if r["gate_decision"] is not None]
    results = []
    for row in rows:
        jd = row["description"]
        if not (jd or "").strip():
            continue
        decisions = {}
        for m in model_names:
            prov = _provider_for(m)
            try:
                decisions[m] = evaluate_job(row, jd, master, prov)["decision"]
            except Exception as e:  # one model failing must not sink the run
                decisions[m] = f"ERROR: {e}"
        results.append({"slug": row["slug"], "title": row["title"],
                        "decisions": decisions})
    return results


def render_report(results):
    disagree = [r for r in results
                if len(set(r["decisions"].values())) > 1]
    lines = ["# Model bench: gate decision agreement", ""]
    lines.append(f"{len(results)} job(s) benched, {len(disagree)} disagreement(s).")
    lines.append("")
    lines.append("## Disagreements")
    if not disagree:
        lines.append("")
        lines.append("(none)")
    for r in disagree:
        lines.append(f"- {r['slug']} ({r['title']})")
        for m, d in r["decisions"].items():
            lines.append(f"    - {m}: {d}")
    lines.append("")
    lines.append("## All results")
    lines.append("")
    for r in results:
        cells = ", ".join(f"{m}={d}" for m, d in r["decisions"].items())
        lines.append(f"- {r['slug']}: {cells}")
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("idents", nargs="*", help="job idents; default: all gated jobs")
    ap.add_argument("--models", required=True,
                    help="comma-separated model ids to compare")
    ap.add_argument("--out", default=None, help="report path (default: stdout)")
    args = ap.parse_args(argv)

    try:
        db = JobDB(resolve_db_path())
    except DBPathError as e:
        sys.exit(str(e))
    master_path = Path(os.environ.get("JOB_MASTER") or
                       (Path(__file__).resolve().parent / "master_resume.yaml"))
    master = yaml.safe_load(master_path.read_text())
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    results = run_bench(db, args.idents, models, master)
    report = render_report(results)
    if args.out:
        Path(args.out).expanduser().write_text(report)
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

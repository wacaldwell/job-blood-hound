#!/usr/bin/env python3
"""
gate.py - the Fit Gate. Fails closed before any prep artifact is generated.

Deliberately separate from fit.py. fit.py is a soft RANKER whose job is to sort
leads by attractiveness. This is a DISQUALIFIER whose job is to say no. Keeping
them apart stops the ranker's optimism from leaking into the gate.

The LLM never renders the verdict. It extracts requirements and proposes
verdicts; Python enforces and decides. Every enforcement rule is one-directional:
code can only make a verdict harsher, never softer.

Import-safe and side-effect-free. `call` is injectable so this tests offline.
No em dashes in any emitted string.
"""

import json
import os
import re
from pathlib import Path

import llm

HERE = Path(__file__).resolve().parent

# Decisions. Everything except PROCEED blocks artifact generation.
RECOMMEND = "RECOMMEND"       # strong match, no gaps: apply
PROCEED = "PROCEED"           # clear enough to draft
CONDITIONAL = "CONDITIONAL"
NEEDS_REVIEW = "NEEDS_REVIEW"
DO_NOT_APPLY = "DO_NOT_APPLY"
NOT_REMOTE = "NOT_REMOTE"     # skills may fit, but the location does not: skip
ERROR = "ERROR"


class ProfileError(ValueError):
    """master_resume.yaml's capability ledger is malformed."""


class GateBlocked(RuntimeError):
    """Raised when an artifact is requested for a job the gate has not cleared."""


def load_profile(master):
    """Read the capability ledger out of master_resume.yaml.

    A claim with no evidence is not a capability, so it is an error, not a
    silently-weaker claim.

    SHAPE is validated here too, not just content. master_resume.yaml is a
    hand-edited file, so "valid YAML, wrong shape" is a real state on disk: an
    emptied file parses as None, a stray leading dash makes the document a
    list, and a mis-indented entry makes a ledger item a bare string. Every one
    of those used to raise AttributeError from a `.get`, which is not what
    callers catch. run_gate maps ProfileError to ERROR (fail closed) and
    job_cli.refine_pipeline maps it to un-demoted scoring (fail safe); an
    AttributeError sailed past both, and took the unattended nightly digest
    down with it. One error type, so every caller's policy actually applies.

    That covers `works_as` too, which no ledger reader touches: title_check
    reads it, and run_gate calls title_check OUTSIDE the try that guards
    extract(). Validating only the two ledger keys left the same fail-open hole
    reachable through a third one, and a shape check that stops one key short of
    its readers is not a shape check.
    """
    if not isinstance(master, dict):
        raise ProfileError(
            "master_resume.yaml must be a mapping, got "
            f"{type(master).__name__}")
    caps = master.get("capabilities") or []
    if not isinstance(caps, list):
        raise ProfileError(
            f"capabilities must be a list, got {type(caps).__name__}")
    for c in caps:
        if not isinstance(c, dict):
            raise ProfileError(f"capability entry is not a mapping: {c!r}")
        if not (c.get("claim") or "").strip():
            raise ProfileError("capability with no claim")
        if not (c.get("evidence") or "").strip():
            raise ProfileError(
                f"capability {c.get('claim')!r} has no evidence. "
                "A claim with no evidence is not a capability.")
    dnc = master.get("do_not_claim") or []
    if not isinstance(dnc, list):
        raise ProfileError(
            f"do_not_claim must be a list, got {type(dnc).__name__}")
    for d in dnc:
        if not isinstance(d, dict):
            raise ProfileError(f"do_not_claim entry is not a mapping: {d!r}")
        if not (d.get("claim") or "").strip():
            raise ProfileError("do_not_claim entry with no claim")
        if not (d.get("match") or d.get("match_word")):
            raise ProfileError(
                f"do_not_claim {d.get('claim')!r} has no match tokens. "
                "Without them it can never fire.")
    # A bare string is rejected rather than accepted, because iterating one
    # yields characters: `works_as: manager` would quietly become
    # ['m','a','n',...] and every title comparison after it would be nonsense
    # while nothing raised. Silent wrong beats loud wrong nowhere in this file.
    works_as = master.get("works_as") or []
    if not isinstance(works_as, list):
        raise ProfileError(
            f"works_as must be a list, got {type(works_as).__name__}")
    for w in works_as:
        if not isinstance(w, str):
            raise ProfileError(f"works_as entry is not a string: {w!r}")
    return caps, dnc


def _norm(s):
    """Lowercase and collapse every run of non-alphanumeric characters to a single
    space. Makes the substring test robust to punctuation and whitespace variance,
    so 'data-catalog', 'data  catalog', 'data\\ncatalog' and 'data catalog' all read
    alike, and 'python, or go' matches the token 'python or go'.

    This is the ONLY normalization here, on purpose. Matching stays a crude,
    auditable substring test (see _touches): a human can read the ledger tokens and
    know exactly what fires. Inflections and paraphrases (correlate vs correlation,
    'ship code daily' vs 'proficient in python') are DELIBERATELY not handled here.
    Generalizing to unseen wording is the semantic screen's job (semantic_screen),
    kept separate so this layer never becomes a black box.
    """
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


# A token whose punctuation sits BETWEEN alphanumeric runs is using it as a
# separator ('data-catalog', 'python, or go'), so normalizing it away is exactly
# the point. Punctuation at either END is part of the competency name itself
# ('C++', 'C#', '.NET'), and normalizing it away leaves a substring broad enough
# to match almost anything: 'C++' would become 'c', which is inside
# 'architecture'. The ledger is user-extensible, so that is a live foot-gun, not
# a hypothetical. Edge-punctuated tokens are matched verbatim instead.
_SEPARATOR_PUNCT = re.compile(r"[a-z0-9]+([^a-z0-9]+[a-z0-9]+)*")


def _touches(req, entry):
    """True if a do_not_claim entry's match tokens appear in the requirement.

    Tokens are lowercase substrings tested against the verbatim quote and the
    topic label. Substring matching is crude on purpose: it is auditable, and the
    ledger author controls the tokens. It fires only on the phrasings the ledger
    spells out; the semantic screen catches requirements that MEAN a forbidden
    competency in words the ledger has never seen.

    Normalization (see _norm) is applied per token, and only where punctuation is
    separating words rather than naming the competency (see _SEPARATOR_PUNCT). A
    token that keeps its punctuation is tested raw, which is exactly how it
    behaved before normalization existed, so this stays strictly additive: every
    match that fired before still fires.

    `match_word` is the second list, for a competency whose name is a short word
    that occurs INSIDE unrelated ordinary words: 'ecs' is inside 'specs' and
    'eks' is inside 'weeks', both of which are common job-description vocabulary.
    A substring token there would fire on the wrong requirements, and spelling
    out every safe multi-word form instead ('amazon ecs', 'ecs or eks', ...) just
    loses to the next phrasing nobody predicted, which is how a standalone 'ECS'
    requirement slipped this ledger the first time. These tokens match on whole
    normalized words only. Kept as a separate key rather than making every token
    word-bounded, because that would silently stop existing tokens from firing
    ('correlation' would no longer match 'correlations') and the one thing this
    matcher may never do is match LESS than it did yesterday.
    """
    return _matches_text(f"{req.get('quote', '')} {req.get('topic', '')}", entry)


def ledger_hit(text, do_not_claim):
    """The first do_not_claim entry whose tokens appear in `text`, else None.

    The public form of the same matcher `_touches` uses, so the ranker in fit.py
    can demote a lead the gate will certainly refuse WITHOUT a second copy of the
    tokens or of the matching rules. One matcher, three callers: _touches (per
    requirement), ledger_sweep (raw JD), and this (ranking).
    """
    for entry in do_not_claim or []:
        if _matches_text(text, entry):
            return entry
    return None


def _matches_text(text, entry):
    """True if any of the entry's match tokens appear in `text`.

    The shared core of _touches and ledger_sweep. They MUST agree: if the raw-JD
    sweep matched on rules the per-requirement test did not, the dedup in
    ledger_sweep would miss and the same entry would be counted twice.
    """
    raw = (text or "").lower()
    hay = _norm(raw)
    for tok in entry.get("match", []):
        tok = (tok or "").strip().lower()
        if not tok:
            continue  # an empty token would silently match every requirement
        if _SEPARATOR_PUNCT.fullmatch(tok):
            if _norm(tok) in hay:
                return True
        elif tok in raw:
            return True
    # _norm has already collapsed every run of punctuation and whitespace to a
    # single space, so padding both sides makes " tok " a whole-word test with no
    # regex involved: 'ecs' hits 'ECS/EKS' and 'on ECS' but not 'specs'.
    padded = f" {hay} "
    for tok in entry.get("match_word", []):
        tok = _norm(tok)
        if not tok:
            continue
        if f" {tok} " in padded:
            return True
    return False


def _norm_with_map(text):
    """_norm, but also returning raw-offset per normalized character.

    Lets _match_span report where a normalized token actually sits in the
    original text. `text` must already be lowercased, exactly as _norm does.
    """
    out, idx, prev_sep = [], [], True
    for i, ch in enumerate(text):
        if ("a" <= ch <= "z") or ("0" <= ch <= "9"):
            out.append(ch); idx.append(i); prev_sep = False
        elif not prev_sep:
            out.append(" "); idx.append(i); prev_sep = True
    while out and out[-1] == " ":      # mirror _norm's .strip()
        out.pop(); idx.pop()
    return "".join(out), idx


def _match_span(text, entry):
    """(start, end) of the first token of `entry` inside `text`, or None.

    Same token rules as _matches_text, both lists included: an entry that fires
    only through `match_word` must still resolve to a span, or _ledger_quote
    falls back to the head of the JD and the ledger attribution is lost.
    """
    raw = (text or "").lower()
    hay, imap = _norm_with_map(raw)

    def _span(j, length):
        return imap[j], imap[j + length - 1] + 1

    for tok in entry.get("match", []):
        tok = (tok or "").strip().lower()
        if not tok:
            continue
        if _SEPARATOR_PUNCT.fullmatch(tok):
            ntok = _norm(tok)
            j = hay.find(ntok) if ntok else -1
            if j != -1:
                return _span(j, len(ntok))
        elif (j := raw.find(tok)) != -1:
            return j, j + len(tok)

    # Whole-word tokens: mirror the padded " tok " test in _matches_text, then
    # translate the padded offset back to an offset within `hay`.
    padded = f" {hay} "
    for tok in entry.get("match_word", []):
        tok = _norm(tok)
        if not tok:
            continue
        j = padded.find(f" {tok} ")
        if j != -1:
            return _span(j, len(tok))  # j indexes hay: the pad shifts by exactly 1
    return None


def _ledger_quote(jd_text, entry, limit=300):
    """A JD excerpt that trips this entry, so the report is auditable.

    A synthetic requirement with no real quote would be unreviewable: the operator
    could not see WHY the ledger fired without re-reading the posting. Worse,
    a quote that does not contain its own token cannot be re-matched by
    enforce(), so `forced` stays empty and the report loses the ledger
    attribution entirely while still blocking the draft.

    The quote is therefore guaranteed to contain the text that fired.
    """
    text = jd_text or ""
    for chunk in re.split(r"(?<=[.!?])\s+|\n+", text):
        chunk = chunk.strip()
        # Test the TRUNCATED chunk, not the whole one: a token sitting past the
        # cap in a run-on line would be sliced off the returned quote.
        if chunk and _matches_text(chunk[:limit], entry):
            return chunk[:limit]

    # No single chunk carries it. Either the token spans a chunk boundary (_norm
    # collapses the newline inside 'data\ncatalog') or it sits past the cap in a
    # run-on line. Centre a window on the real match instead of falling back to
    # the head of the JD, which is almost never the relevant text.
    span = _match_span(text, entry)
    if span is None:
        return text.strip()[:limit]
    start, end = span
    if end - start >= limit:
        return text[start:start + limit].strip()
    pad = (limit - (end - start)) // 2
    ws = max(0, start - pad)
    we = min(len(text), ws + limit)
    ws = max(0, we - limit)
    return ("..." if ws > 0 else "") + text[ws:we].strip() + ("..." if we < len(text) else "")


def ledger_sweep(jd_text, do_not_claim, requirements):
    """Fire the ledger against the RAW job description, not just the extraction.

    The ledger is absolute, but until now it was only ever tested against the
    requirements the model chose to extract. Extraction reliably turns bulleted
    requirements into quotes and reliably DROPS role-framing prose, which is
    exactly where disqualifying phrasing lives. On one live posting
    (2026-08-18) the JD said "This is a player-coach role" verbatim, the token
    had been in the ledger since 2026-07-24, and the gate still returned
    NEEDS_REVIEW because no extracted quote contained it.

    Returns one synthetic hard-NONE requirement per ledger entry that the JD
    trips and the extraction missed. One per ENTRY, not per token, so a stack
    list naming both Kafka and Kinesis cannot inflate a single gap into two.
    Entries already caught by a real requirement are skipped, because
    double-counting one gap would reach DO_NOT_APPLY on its own.

    Only ever ADDS hard NONEs, so like every other rule here it is
    one-directional: it can harshen a decision and can never soften one.
    """
    out = []
    for entry in do_not_claim or []:
        if not _matches_text(jd_text, entry):
            continue
        if any(_touches(r, entry) for r in requirements):
            continue  # the extraction already surfaced it; enforce() will force it
        out.append({
            "quote": _ledger_quote(jd_text, entry),
            "topic": entry.get("claim", ""),
            "hard": True,
            "confidence": "high",
            "verdict": "NONE",
            "evidence": "",
            "bridge": "",
            "forced": "",
            "ruled_by_human": False,
        })
    return out


# Markers for a verdict that is not open to argument. The substring ledger and
# the semantic screen are the SAME absolute do_not_claim list, one matched by
# keyword and one by meaning, so neither is adjudicable with `jh gate-rule`
# (see forced_by_ledger and cmd_gate_rule). The "no-evidence" and "no-bridge"
# markers are deliberately NOT here: those demote a verdict, and ruling on the
# hard-versus-soft classification of such a requirement is still a fair call.
LEDGER_FORCED = "do-not-claim: "
SCREEN_FORCED = "semantic-screen: "


def forced_by_ledger(req):
    """True if this requirement's NONE came from the do_not_claim list, whether
    matched by substring (LEDGER_FORCED) or by meaning (SCREEN_FORCED)."""
    return (req.get("forced") or "").startswith((LEDGER_FORCED, SCREEN_FORCED))


def enforce(requirements, do_not_claim):
    """Apply the deterministic demotion rules to the model's proposed verdicts.

    Every rule is one-directional: a verdict can only get harsher. There is no
    path here that upgrades a NONE, which is what makes "bias toward NONE when
    evidence is thin" an invariant instead of a request the model can talk
    itself out of.

    Returns a new list. Does not mutate the input.
    """
    out = []
    for r in requirements:
        r = dict(r)  # copy; never mutate the caller's list
        r["forced"] = ""  # reset every run: a stale forced label must not survive
        r.setdefault("ruled_by_human", False)

        # Rule 3 first: the ledger overrules the model outright, whatever it said.
        hit = next((d for d in do_not_claim if _touches(r, d)), None)
        if hit:
            r["verdict"] = "NONE"
            r["forced"] = f"{LEDGER_FORCED}{hit['claim']}"
            out.append(r)
            continue

        # Rule 1: a claim with no evidence is not a capability.
        if r.get("verdict") == "HAVE" and not (r.get("evidence") or "").strip():
            r["verdict"] = "NONE"
            r["forced"] = "no-evidence"

        # Rule 2: a PARTIAL with no written bridge is a rationalization.
        elif r.get("verdict") == "PARTIAL" and not (r.get("bridge") or "").strip():
            r["verdict"] = "NONE"
            r["forced"] = "no-bridge"

        out.append(r)
    return out


def _is_known_hard_none(r):
    """A confidently-HARD requirement he does not have. The disqualifier.

    A human ruling counts as confident, because the operator adjudicated it.
    """
    confident = r.get("confidence") == "high" or r.get("ruled_by_human")
    return bool(r.get("hard")) and confident and r.get("verdict") == "NONE"


def _is_unresolved(r):
    """Classification we are unsure of, on something he does NOT clearly have.

    If he HAS it, hard-versus-soft cannot change the outcome, so there is
    nothing worth asking him about. Adjudication only happens where his ruling
    could actually move the decision.
    """
    if r.get("ruled_by_human"):
        return False
    return (r.get("confidence") == "low"
            and r.get("verdict") in ("NONE", "PARTIAL"))


def _could_become_hard_none(r):
    """An unresolved item that, if the operator ruled it HARD, would be a hard NONE.

    Only a NONE-verdict unresolved item can. An unresolved PARTIAL, even ruled
    HARD, stays a PARTIAL, so it is not a latent disqualifier."""
    return _is_unresolved(r) and r.get("verdict") == "NONE"


def fit_strength(requirements):
    """A positive readout of how well the background covers the HARD requirements,
    separate from the block decision. STRONG when direct HAVEs dominate the hard
    requirements; used to decide when a clean gate is a RECOMMEND, not a bare
    PROCEED.

    Numerator and denominator are both over HARD requirements, so soft
    nice-to-haves graded HAVE cannot inflate the ratio and make a role STRONG when
    its one hard requirement is only a PARTIAL."""
    have_hard = sum(1 for r in requirements
                    if r.get("hard") and r.get("verdict") == "HAVE")
    substantive = sum(1 for r in requirements
                      if r.get("hard") and r.get("verdict") in ("HAVE", "PARTIAL", "NONE"))
    ratio = (have_hard / substantive) if substantive else 0.0
    if have_hard >= 3 and ratio >= 0.55:
        return "STRONG"
    if have_hard >= 2 and ratio >= 0.4:
        return "SOLID"
    return "THIN"


def counts(requirements):
    return {
        "known_hard_none": sum(1 for r in requirements if _is_known_hard_none(r)),
        "unresolved": sum(1 for r in requirements if _is_unresolved(r)),
    }


def decide(requirements):
    """Render the verdict in Python, from the enforced requirement list.

    Monotone on the downside: unresolved items can only make the decision worse.
    When the known-bad already reaches DO NOT APPLY, that is final. The upside
    is guarded by a WORST-CASE check: a job is only surfaced positively when even
    ruling every unresolved item badly could not push it past a single gap, so a
    green light can never hide a disqualifier.
    """
    if not requirements:
        return ERROR  # an empty extraction means the gate learned nothing

    known = sum(1 for r in requirements if _is_known_hard_none(r))
    worst = known + sum(1 for r in requirements if _could_become_hard_none(r))
    unresolved = sum(1 for r in requirements if _is_unresolved(r))

    if known >= 2:
        return DO_NOT_APPLY          # already disqualified
    if worst >= 2:
        return NEEDS_REVIEW          # could become a 2-gap job; the operator must rule
    if known == 1:
        return CONDITIONAL           # one real gap, plan it
    if worst >= 1:
        # A single POSSIBLE gap (an unresolved item that could be a hard NONE).
        # Do not green-light it; route to the operator, who is the final gauge.
        # They rule it, then it recomputes to RECOMMEND, PROCEED, or CONDITIONAL.
        return NEEDS_REVIEW
    # known == 0 and worst == 0: no gaps, and none hiding in the unsure items.
    if fit_strength(requirements) == "STRONG":
        return RECOMMEND             # strong coverage, no gaps possible: apply
    if unresolved > 0:
        return NEEDS_REVIEW          # not clearly strong; confirm the unsure items
    return PROCEED


EXTRACT_SYSTEM = """You are a hiring gate. Your job is to DISQUALIFY, not to
encourage. You are the last line of defense against a candidate wasting a week
preparing for a role that will reject him for a gap that was visible in the job
description on day one. That has already happened once. Do not let it happen again.

You are given a candidate's verified capabilities (each with evidence) and a job
description. Extract EVERY requirement from the job description and judge each one.

RULE 1: QUOTE VERBATIM. Copy the requirement text exactly as written. Never
paraphrase. Paraphrasing is where a hard requirement gets quietly softened.

RULE 2: CLASSIFY HARD OR SOFT. This is the most important judgment you make.

  HARD when the requirement is:
    - stated as expertise, proficiency, or deep experience with no hedge
    - repeated across more than one bullet
    - implied by the role-noun in the title
    - a standalone item under a requirements or expertise heading

  SOFT when the requirement is:
    - hedged with "and/or", "familiarity with", "exposure to", "nice to have",
      "bonus", "a plus"
    - bundled so that any ONE item in the list satisfies it

  THE AND/OR TRAP. These two look alike and are not alike:

    "15+ years across AIOps, data catalog architecture, product development,
     AND/OR Technical Operations infrastructure"
       -> SOFT. Hedged across DISTINCT DOMAINS. Any one item satisfies it.

    "Deep expertise in ... AI/automation, data catalog architecture, workflows,
     AND correlation"
       -> HARD. A CONJUNCTIVE list of focus areas, no hedge, under an expertise
          heading. SPLIT IT into one requirement per item: one for
          "AI/automation", one for "data catalog architecture", one for
          "workflows", one for "correlation".

  A bare "or" between INTERCHANGEABLE INSTANCES of ONE competency is still HARD:

    "Proficient in Python or Go"
       -> HARD. Proficiency in a language is required; which language is the
          candidate's choice. If he has neither, this is a hard NONE. Do not
          call this soft just because you see the word "or".

RULE 3: VERDICT. Grade against the candidate's WHOLE BACKGROUND (the summary,
every experience bullet, skills, certifications, education, and the capabilities
highlights), not just the highlights. Credit adjacent and transferable experience:
if he did the thing under a different name, on a different cloud, or at a foundational
level, that counts. Managing DNS on-prem is real DNS experience. A cloud migration
is real cloud-integration experience. Do not require a verbatim keyword match.

    HAVE    - he can defend it. Cite the specific evidence from his background
              (name the role or the skill). Direct experience, or clearly
              equivalent experience under another name. No citable evidence
              anywhere in the background means it is not a HAVE.
    PARTIAL - he has adjacent, transferable, or foundational experience but not
              the full depth the requirement asks for. This is the common case
              for a senior generalist, and it is the RIGHT verdict when the answer
              is "I have done the neighboring thing." Write the bridge out in full
              so he can hear how it sounds in an interview. Example: the JD wants
              deep multi-cloud networking with Transit Gateway and ExpressRoute,
              he has run DNS and led a cloud migration; that is a PARTIAL with a
              real bridge, not a NONE.
    NONE    - he has NOTHING to stand on. No direct experience, and nothing
              adjacent, transferable, or foundational anywhere in the background.
              NONE means genuinely absent, not "lacks the deepest level" and not
              "no exact keyword". If you can write an honest bridge, it is a
              PARTIAL, not a NONE.

  DO NOT STRETCH. A PARTIAL needs REAL adjacent, transferable, or foundational
  experience and a bridge you would actually say out loud in an interview. If the
  bridge is a reach, if it leans on a single tangential bullet, or if you find
  yourself arguing FOR it, it is a NONE. A PARTIAL is "I have done the neighboring
  thing", never "I could probably spin this". Rationalizing a real gap into a
  PARTIAL is the exact failure that costs him a week and an interview.

  CALIBRATION. A false NONE makes him skip a job he would have won. A false HAVE
  or an over-generous PARTIAL walks him into an interview ambush. BOTH are real
  costs. So: reserve HAVE for solid evidence, reserve NONE for genuine absence,
  and use PARTIAL only for the honest middle where a senior person has genuinely
  done the neighboring work. Do not inflate a real gap into a HAVE or a PARTIAL,
  and do not deflate genuine adjacent experience into a NONE. When you truly
  cannot tell, grade it NONE and let the human be the judge; he reviews every
  gate and can rule it up.

  THE ABSOLUTE FLOOR. Some things he genuinely cannot claim at any level, and
  those are always NONE no matter how the requirement is phrased. That list is
  enforced separately in code, so you do not have to police it. Your job is to
  grade the rest honestly.

RULE 4: CONFIDENCE. Set confidence to "low" ONLY when you are genuinely unsure
whether a requirement is HARD or SOFT. Confidence is about the HARD/SOFT
classification ONLY. It is NEVER about the evidence: if you are unsure whether
he has something, that is a NONE, not a low confidence.

Hard voice rule: never use em dashes or double hyphens. Use commas, parentheses,
or separate sentences.

Return ONLY valid JSON, no markdown fences, with this exact shape:
{
  "requirements": [
    {"quote": "verbatim text from the JD",
     "topic": "short label, e.g. data catalog architecture",
     "hard": true,
     "confidence": "high",
     "verdict": "NONE",
     "evidence": "cited from the capabilities list, or empty string",
     "bridge": "the written bridge if PARTIAL, else empty string"}
  ]
}"""


# The gate's output is one JSON object per requirement, each carrying a VERBATIM
# quote from the JD, so it is far longer than the generator's output for the same
# posting. A senior JD routinely yields 20 or more requirements. At 4000 the model
# ran out of room mid-string on a real 11.7k-character Humana posting, the JSON came
# back truncated, and the gate ERRORed. That failed closed, which is correct, but it
# made the gate unusable on exactly the long postings worth gating.
# Output cap for the extraction. Sized for the most verbose supported provider,
# not the leanest: on a 10k-char JD, Opus finishes well inside 16000 but Kimi
# (kimi-k2.6) used 22000 output tokens for 51 requirements and stopped naturally.
# At 16000 that same run was cut off mid-JSON and the gate failed closed. This is
# a cap, not a spend, so the headroom costs nothing on providers that finish early.
GATE_MAX_TOKENS = 32000


class TruncatedResponse(ValueError):
    """The model hit max_tokens and the JSON is cut off. A ValueError subclass so
    run_gate's existing handler still catches it and fails closed."""


def _call_anthropic(system, user, api_key):
    # api_key is honored as an override for the DEFAULT provider only (the CLI and
    # ingest thread ANTHROPIC_API_KEY here). JOB_PROVIDER still selects base_url +
    # model. A non-default provider carries its own key from env.
    prov = llm.resolve_provider(component="gate")
    if api_key and prov.name == llm.DEFAULT_PROVIDER:
        prov = prov._replace(api_key=api_key)
    try:
        return llm.call_messages(system, user, max_tokens=GATE_MAX_TOKENS,
                                 provider=prov, raise_on_truncation=True,
                                 component="gate")
    except llm.OutputTruncated:
        raise TruncatedResponse(
            f"the model hit the {GATE_MAX_TOKENS} token output cap and the JSON was "
            "cut off mid-response. The job description is long enough that the "
            "requirement list did not fit. Raise GATE_MAX_TOKENS in gate.py.")


def _call_screen(system, user, api_key):
    """Run the semantic screen on the gate model with its own cost label."""
    prov = llm.resolve_provider(component="gate")
    if api_key and prov.name == llm.DEFAULT_PROVIDER:
        prov = prov._replace(api_key=api_key)
    return llm.call_messages(system, user, max_tokens=1000, provider=prov,
                             component="gate_screen")


def _parse_requirements(raw):
    """Parse the model's JSON. Defaults every missing field to the HARSH side,
    so a half-populated response can never accidentally PASS."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(json)?", "", text).rsplit("```", 1)[0].strip()
    data = json.loads(text)  # ValueError (JSONDecodeError) on garbage
    items = data.get("requirements")
    if items is None:
        raise ValueError("model returned no 'requirements' key")
    if not isinstance(items, list):
        raise ValueError(f"'requirements' must be a list, got {type(items).__name__}")

    out = []
    for it in items:
        if not isinstance(it, dict):
            raise ValueError(f"each requirement must be an object, got {type(it).__name__}")
        verdict = (it.get("verdict") or "NONE").upper()
        if verdict not in ("HAVE", "PARTIAL", "NONE"):
            verdict = "NONE"
        confidence = (it.get("confidence") or "low").lower()
        if confidence not in ("high", "low"):
            confidence = "low"
        # Default HARD: an unclassified requirement is treated as blocking.
        # dict.get(key, default) only substitutes when the key is ABSENT, so
        # an explicit "hard": null still needs its own None check here, or it
        # would read as False and land on the lenient side.
        hard_val = it.get("hard")
        hard = True if hard_val is None else bool(hard_val)
        out.append({
            "quote": it.get("quote") or "",
            "topic": it.get("topic") or "",
            "hard": hard,
            "confidence": confidence,
            "verdict": verdict,
            "evidence": it.get("evidence") or "",
            "bridge": it.get("bridge") or "",
            "forced": "",
            "ruled_by_human": False,
        })
    return out


def build_evidence(master):
    """Assemble the whole resume as the evidence the gate grades against.

    Previously the gate saw only the hand-written `capabilities` list (9 entries),
    so anything outside those 9 resolved to NONE by construction, and the gate
    falsely reported that the operator had no bachelor's degree. The resume said
    otherwise; the degree was just never in the 9. Grade against everything the
    resume actually states:
    location, experience bullets, skills, certifications, education, plus the
    curated capabilities highlights. do_not_claim still overrules all of it.

    Anything omitted here is a false NONE waiting to happen, so add a category
    rather than leaving the model to infer it.
    """
    exp = []
    for role in master.get("experience", []) or []:
        # Join with " to " only the parts that exist. NOT .strip(" to"), which
        # strips the character set {space, t, o} and mangles values like "present"
        # -> "presen" or "oct 2019" -> "ct 2019", misrepresenting tenure to the model.
        ends = [str(role.get(k, "")).strip() for k in ("start", "end")]
        dates = " to ".join(p for p in ends if p)
        exp.append({
            "company": role.get("company"),
            "title": role.get("title"),
            "dates": dates,
            "points": role.get("points", []),
        })
    return {
        # Where the operator lives. Without this every residency or commuting
        # requirement ("must reside within commuting distance of the office")
        # is NONE by construction, which is the same false-NONE-by-omission bug
        # this function was written to fix for the bachelor's degree. It bit a
        # live on-site role: two different models BOTH graded location a hard
        # NONE while master_resume.yaml named a qualifying home town the whole time.
        # Only the location is taken from `contact`; email and phone are not
        # evidence and do not belong in a prompt.
        "location": (master.get("contact") or {}).get("location", ""),
        "summary": master.get("summary", ""),
        "experience": exp,
        "skills": master.get("skills", []),
        "certifications": master.get("certifications", []),
        "education": master.get("education", []),
        "capabilities_highlights": master.get("capabilities", []),
    }


def extract(job, jd_text, evidence, api_key, call=None):
    """One LLM call. Returns raw requirements, BEFORE enforcement.

    `evidence` is the candidate's verified background (build_evidence(master), or
    a plain capabilities list in older tests). It is the only thing the candidate
    may be said to HAVE.
    """
    call = call or _call_anthropic
    user = f"""CANDIDATE'S VERIFIED BACKGROUND, from the resume (the only things he may be said to HAVE):
{json.dumps(evidence, indent=2)}

ROLE:
Company: {job.get('company')}
Title: {job.get('title')}

JOB DESCRIPTION:
{(jd_text or '')[:20000]}

Extract and judge every requirement now. Return the JSON."""
    return _parse_requirements(call(EXTRACT_SYSTEM, user, api_key))


# The substring ledger in enforce() only catches phrasings its tokens spell out.
# It missed one live posting's coding bar until a token was added by hand, AFTER the
# fact. This second pass generalizes the SAME do_not_claim list by MEANING, so a
# requirement worded differently ("ship production code weekly", "fluency in a
# backend language") is caught without waiting for a lost week to teach the ledger
# a new token. It only ever forces NONE; it is a disqualifier, never a rescue.
SCREEN_SYSTEM = """You are a disqualification screen, the last check before a
candidate commits a week to a job. You are given a short list of competencies the
candidate DEFINITIVELY LACKS (each is absolute and not arguable), and a list of job
requirements that a first pass graded as things he HAS or PARTIALLY has.

Your ONLY job: find requirements that actually REQUIRE one of the lacked
competencies but were graded too generously because they were worded differently
from how the lacked competency is named. Judge by MEANING, not keywords.

  REQUIRES a lacked competency (disqualify): "ship production code every week",
  "write and review code daily", "fluency in a backend language such as Go or
  Java", "hands-on development in Python" all REQUIRE hand-writing production code,
  whatever words they use. "build and own event correlation across telemetry"
  REQUIRES event correlation even if it never says the word "correlation".

  Does NOT require it (leave alone): "code review", "set technical direction",
  "drive engineering excellence", "partner with engineers", "read code". Reviewing,
  directing, and reading are not writing. Do not disqualify a leadership or
  architecture requirement just because software is nearby. Be strict, not paranoid:
  a false disqualification makes him skip a job he would have won.

Return ONLY valid JSON, no markdown fences, exactly:
{"disqualified": [{"index": 0, "claim": "hand-writing production code"}]}
Use the integer index shown for each requirement, and the verbatim text of the
lacked competency for "claim". Include ONLY requirements that truly require a
lacked competency. Empty list if none. Never use em dashes or double hyphens."""


def _screen_enabled():
    """The screen is on by default. JOB_GATE_SCREEN=off disables it (one fewer API
    call per gate) and the gate falls back to the substring ledger alone, which is
    exactly the pre-screen behavior."""
    return os.environ.get("JOB_GATE_SCREEN", "on").strip().lower() not in (
        "off", "0", "false", "no")


def semantic_screen(requirements, do_not_claim, api_key, call=None):
    """Second, one-directional disqualifier pass over the enforced requirements.

    Only looks at requirements the first pass was GENEROUS on (HAVE or PARTIAL)
    and that the ledger did not already force. A requirement already NONE, or
    already forced by the ledger, needs no second look, so a clean-reject JD costs
    no extra call. It can only HARSHEN: it forces HAVE/PARTIAL to NONE and never
    upgrades anything, the same one-directional invariant enforce() holds.

    Returns (new_requirements, info) where info is
    {"screened": n, "forced": [...], "error": str|None}. On any model or parse
    error it returns the requirements UNCHANGED with info["error"] set. This pass
    is protection layered ON TOP of the ledger decision, so when it cannot run the
    gate stays exactly as strict as the ledger already made it, never weaker and
    never crashed by a flaky second call. run_gate surfaces the error in the report.
    """
    reqs = [dict(r) for r in requirements]
    claims = [d["claim"] for d in do_not_claim if d.get("claim")]
    if not _screen_enabled() or not claims:
        return reqs, {"screened": 0, "forced": [], "error": None}

    candidates = [(i, r) for i, r in enumerate(reqs)
                  if not (r.get("forced") or "").strip()
                  and r.get("verdict") in ("HAVE", "PARTIAL")]
    if not candidates:
        return reqs, {"screened": 0, "forced": [], "error": None}

    call = call or _call_screen
    listing = "\n".join(
        f'{i}. topic: {r.get("topic", "")} | requirement: {r.get("quote", "")}'
        for i, r in candidates)
    user = f"""COMPETENCIES THE CANDIDATE DEFINITIVELY LACKS (absolute):
{chr(10).join('- ' + c for c in claims)}

REQUIREMENTS GRADED HAVE OR PARTIAL (screen only these, by their index):
{listing}

Return the JSON."""

    try:
        raw = call(SCREEN_SYSTEM, user, api_key)
        text = (raw or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(json)?", "", text).rsplit("```", 1)[0].strip()
        data = json.loads(text)
        hits = data.get("disqualified")
        if not isinstance(hits, list):
            raise ValueError("screen response has no 'disqualified' list")
    except Exception as e:
        return reqs, {"screened": len(candidates), "forced": [], "error": str(e)}

    valid = {i for i, _ in candidates}  # never force a req we deliberately skipped
    forced = []
    # This loop sits OUTSIDE the try above, on purpose: a single malformed hit
    # should cost that hit, not every hit the screen got right. That only holds
    # if the fields are type-checked before use. Untyped, an unhashable index
    # ([]) raises on `idx in valid` and a non-string claim (1) raises on
    # .strip(), and either would crash the gate with no decision persisted
    # instead of falling back to the ledger result the docstring promises.
    # bool is excluded explicitly because it is an int subclass, so a JSON
    # `true` would otherwise silently force requirement 1.
    for h in hits:
        if not isinstance(h, dict):
            continue
        idx = h.get("index")
        if isinstance(idx, bool) or not isinstance(idx, int) or idx not in valid:
            continue
        claim = h.get("claim")
        claim = (claim.strip() if isinstance(claim, str) else "") or "semantic match"
        reqs[idx]["verdict"] = "NONE"
        reqs[idx]["forced"] = f"{SCREEN_FORCED}{claim}"
        forced.append({"index": idx, "claim": claim})
    return reqs, {"screened": len(candidates), "forced": forced, "error": None}


# Role-nouns we can recognize in a title. Order matters only where entries
# overlap: "engineering manager" must precede "manager" so the compound reads
# as manager, and "manager" must precede "engineer" for the same reason.
ROLE_NOUNS = ["engineering manager", "manager", "architect", "director",
              "lead", "engineer"]

# How the title's role-noun reads, for the report.
_NOUN_NOTE = {
    "engineer": "An Engineer title predicts live coding and code review in the "
                "loop, whatever the JD emphasizes.",
    "manager": "A Manager title predicts people and delivery questions.",
    "architect": "An Architect title predicts design and tradeoff questions.",
    "director": "A Director title predicts org and strategy questions.",
    "lead": "A Lead title predicts a mix of hands-on and direction-setting.",
}


def title_check(title, master):
    """Compare the title's role-noun against how the operator actually works.

    A FLAG, not a blocker. It never enters the decision arithmetic (decide()
    does not even take a title). It predicts the SHAPE of the interview loop,
    which is what one live loop punished.
    """
    t = (title or "").lower()
    noun = next((n for n in ROLE_NOUNS
                 if re.search(rf"\b{re.escape(n)}\b", t)), "")
    # "engineering manager" is a manager.
    if noun == "engineering manager":
        noun = "manager"

    works_as = [w.lower() for w in (master.get("works_as") or [])]
    mismatch = bool(noun) and bool(works_as) and noun not in works_as

    note = _NOUN_NOTE.get(noun, "")
    if mismatch:
        # A heads-up about the interview shape, not a reason to avoid the role.
        # An IC title is not a dealbreaker; just know the loop will lean hands-on.
        note = (f"This is a '{noun}' title, not one of your usual "
                f"({', '.join(works_as)}). Not a dealbreaker, just expect a more "
                f"hands-on loop. {note}")
    return {"role_noun": noun, "mismatch": mismatch, "note": note}


def _no_dash(s):
    """Project hard rule. Same net as fit._no_dash."""
    return (s or "").replace("—", ", ").replace("–", ", ").replace("--", ", ")


_DECISION_LABEL = {
    RECOMMEND: "RECOMMEND (strong match, apply)",
    PROCEED: "PROCEED",
    CONDITIONAL: "CONDITIONAL",
    NEEDS_REVIEW: "NEEDS REVIEW",
    DO_NOT_APPLY: "DO NOT APPLY",
    NOT_REMOTE: "SKIP (not remote, not your area)",
    ERROR: "ERROR",
}


def _location_rules():
    """Remote and on-site eligibility, from profile.yaml, the same lists the scan
    and the ranker use. A fallback keeps the gate working if profile.yaml is
    absent.

    The on-site fallback is EMPTY on purpose. Where you will commute is personal
    configuration and there is no sane guess for it, so with no profile.yaml the
    gate accepts remote roles and calls every on-site role NOT_REMOTE. That errs
    toward blocking, which is the direction this gate is allowed to err in; a
    guessed home town would err toward passing a role you cannot take. Set
    onsite_ok in profile.yaml to open your own commuting radius."""
    try:
        import fit
        prof = fit.load_profile()
        return ([t.lower() for t in prof.get("remote_ok", [])],
                [t.lower() for t in prof.get("onsite_ok", [])])
    except Exception:
        return (["remote", "anywhere", "distributed", "united states", "u.s."],
                [])


# Unambiguous, ROLE-SPECIFIC remote phrases. Deliberately strict. A bare "remote"
# or even "work from home" often appears in a company's generic flexibility
# boilerplate on an on-site posting (one live in-office role advertised "the best
# of both worlds: in-office and work from home"), so those are NOT here. These
# phrases state that THIS role is remote.
_REMOTE_PHRASES = [
    "100% remote", "fully remote", "fully-remote", "remote-first", "remote first",
    "work from anywhere", "this is a remote", "position is remote", "role is remote",
    "is a remote position", "is a fully remote",
]

# Location-field tokens that unambiguously mean the ROLE is remote. Deliberately
# does NOT include "united states"/"u.s." (profile.yaml's remote_ok has those for
# the soft ranker, but an on-site role also spells out its country, so as a HARD
# gate they would pass every US on-site posting).
_REMOTE_LOC_TOKENS = ("remote", "anywhere", "distributed")


def location_ok(job, jd_text=""):
    """Is this role remote-eligible or in the operator's on-site area?

    Location is a hard eligibility gate: skills do not matter for a job you cannot
    take. Reuses the remote_ok/onsite_ok rules from profile.yaml. Crucially, a
    remote role often lists an HQ city in its location field (one live "Senior
    DevOps, Remote - USA" posting listed Charleston, WV), so the title and an
    explicit remote phrase in the JD count too, not just the structured field.
    A posting with no location signal at all is not blocked; the report flags it
    for a human check.

    Returns {"ok": bool, "reason": str}.
    """
    _, onsite_ok = _location_rules()
    raw = job.get("location") or ""
    loc = raw.lower()
    title = (job.get("title") or "").lower()
    jd = (jd_text or "").lower()

    # Explicit remote in the STRUCTURED location field or the title wins first, so
    # a clearly-remote posting is never overridden by a stray "in-office" clause in
    # the JD prose. Deliberately strict: NOT "united states"/"u.s.", because an
    # on-site role also spells out its country ("New York, NY, United States"), and
    # treating that as remote defeats the gate for every US on-site posting.
    if any(t in loc for t in _REMOTE_LOC_TOKENS) or "remote" in title:
        return {"ok": True, "reason": f"remote-eligible ({raw or 'per title'})"}

    # Then the authoritative structured field, when present. Many ATS pages carry
    # an explicit "Workplace Type: Office | Remote | Hybrid". One live in-office
    # role says "Workplace Type: Office" even while its culture blurb mentions
    # working from home. This runs AFTER the explicit-remote check above, so a
    # "Remote - USA" posting is not blocked by a stray workplace-type line.
    m = re.search(r"(?:workplace|work|location)\s*(?:type|setting)\s*[:\-]?\s*"
                  r"(remote|office|on[\s-]?site|in[\s-]?person|in[\s-]?office|hybrid)",
                  jd)
    if m:
        val = m.group(1)
        if "remote" in val:
            return {"ok": True, "reason": "workplace type is remote"}
        if any(t in loc for t in onsite_ok):
            return {"ok": True, "reason": f"workplace type is {val}, but in your area ({raw})"}
        return {"ok": False,
                "reason": f"workplace type is '{val}', not remote and not your area"}

    if any(p in jd for p in _REMOTE_PHRASES):
        return {"ok": True,
                "reason": f"the JD says remote, though the posting lists '{raw}'"}
    if any(t in loc for t in onsite_ok):
        return {"ok": True, "reason": f"on-site in your area ({raw})"}
    if not loc.strip():
        return {"ok": True, "reason": "no location on the posting, verify by hand"}
    return {"ok": False,
            "reason": f"location reads as '{raw}', not remote and not in your "
                      "on-site area, and the JD names no remote arrangement"}


def _apply_location(skills_decision, loc):
    """Location overlays the skills verdict. A job he cannot take is NOT_REMOTE
    no matter how strong the skills; otherwise the skills verdict stands."""
    if loc and not loc.get("ok", True):
        return NOT_REMOTE
    return skills_decision


def render_report(job_row, requirements, title, decision, cnt,
                  loc=None, skills_decision=None, screen=None):
    """The fit report. Every requirement, verbatim, with what backs it or the
    explicit absence of anything backing it."""
    L = [f"# Fit report: {_no_dash(job_row['title'])} at {_no_dash(job_row['company'])}",
         "",
         f"## Decision: {_DECISION_LABEL.get(decision, decision)}",
         "",
         f"{cnt['known_hard_none']} hard requirement(s) with no evidence. "
         f"{cnt['unresolved']} item(s) awaiting your ruling.",
         ""]

    if screen and screen.get("error"):
        L += ["The semantic screen (the second, meaning-based disqualifier pass) "
              "could not run this time, so this decision rests on the substring "
              f"ledger alone. Reason: {_no_dash(str(screen['error']))}.", ""]

    if decision == NOT_REMOTE:
        sk = _DECISION_LABEL.get(skills_decision, skills_decision) if skills_decision else "not assessed"
        L += [f"This role is not remote and not in your area, so it is a skip "
              f"regardless of fit. {_no_dash((loc or {}).get('reason', ''))}.",
              "",
              f"On skills alone the gate read this as {sk}, so if the location is "
              "wrong (a remote role listing an HQ city, say), override with a reason.",
              ""]
    elif decision == DO_NOT_APPLY:
        L += ["Two or more hard requirements have no evidence behind them. "
              "Applying means walking into an interview loop that will probe "
              "exactly these. Override only with a reason you would say out loud.", ""]
    elif decision == CONDITIONAL:
        L += ["One hard requirement has no evidence. Applying is fine. Applying "
              "while unprepared for a known gap is not. Plan the gap below "
              "(plan, hours, deadline) before any package is generated.", ""]
    elif decision == NEEDS_REVIEW:
        L += ["Some requirements could not be confidently classified as hard or "
              "soft. Rule on them with `jh gate-rule`, and the decision recomputes "
              "with no further API call.", ""]

    if title.get("mismatch"):
        L += ["## Title check (heads up)", "", _no_dash(title["note"]), ""]
    elif title.get("note"):
        L += ["## Title check", "", _no_dash(title["note"]), ""]

    L += ["## Requirements", ""]
    for i, r in enumerate(requirements, 1):
        kind = "HARD" if r["hard"] else "SOFT"
        if r["confidence"] == "low" and not r["ruled_by_human"]:
            kind += " (UNSURE)"
        L.append(f"### {i}. [{kind} / {r['verdict']}] {_no_dash(r['topic'])}")
        L.append("")
        L.append(f"> {_no_dash(r['quote'])}")
        L.append("")
        if r["forced"]:
            src = ("semantic screen" if r["forced"].startswith(SCREEN_FORCED)
                   else "ledger")
            L.append(f"Overruled by the {src}: {_no_dash(r['forced'])}.")
        if r["verdict"] == "HAVE":
            L.append(f"Evidence: {_no_dash(r['evidence'])}")
        elif r["verdict"] == "PARTIAL":
            L.append(f"Bridge: {_no_dash(r['bridge'])}")
        else:
            L.append("Evidence: NONE.")
        L.append("")

    gaps = [r for r in requirements if r["hard"] and r["verdict"] == "NONE"]
    if gaps:
        L += ["## Gaps (tracked, not notes)", ""]
        for g in gaps:
            L.append(f"- {_no_dash(g['topic'])}: {_no_dash(g['quote'])}")
        L += ["", "Plan each with: jh gaps <ident>", ""]

    return "\n".join(L) + "\n"


def _report_path(job_row):
    """Written into the package folder, created at GATE time rather than draft
    time, so the report exists at the moment the decision is made."""
    import job_generate
    folder = job_generate.package_folder(job_row)
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "fit-report.md"


def _persist(db, job_row, requirements, title, decision, cnt,
             loc=None, skills_decision=None, model=None, screen=None):
    path = _report_path(job_row)
    path.write_text(render_report(job_row, requirements, title, decision, cnt,
                                  loc=loc, skills_decision=skills_decision,
                                  screen=screen))
    db.set_gate(job_row["uid"], decision,
                json.dumps({"requirements": requirements, "title": title,
                            "location": loc, "skills_decision": skills_decision,
                            "model": model, "screen": screen}),
                str(path), model=model)
    return path


def _reconcile_gaps(db, uid, requirements):
    """Make the gaps table a pure function of the current hard-NONE set,
    while respecting WHY a gap closed.

    For each requirement currently in the hard-NONE set:
      - an OPEN gap is left alone.
      - no gap at all gets one added.
      - a gap most recently closed as 'reclassified' (the SYSTEM closed it,
        because an earlier ruling moved the requirement off the hard-NONE
        set) REOPENS: the human never did any work here, so if the
        requirement is a hard NONE again, the gap must be too. Its plan,
        hours, and deadline, if any, are preserved; require_pass still
        blocks until all three are present.
      - a gap most recently closed as 'planned' (the HUMAN closed it, via
        `jh gap-close`, because he did the work) stays closed. This
        invariant must never break: a re-run must never undo work he
        actually did.

    Closes any OPEN gap whose requirement is no longer in the current
    hard-NONE set, as 'reclassified'. Touches only gaps for this job.
    """
    hard_none = {r["quote"] for r in requirements
                 if r["hard"] and r["verdict"] == "NONE"}
    for quote in hard_none:
        gap = db.gap_for_requirement(uid, quote)
        if gap is None:
            db.add_gap(uid, quote)
        elif gap["status"] == "open":
            pass  # already tracked, nothing to do
        elif gap["closed_reason"] == "reclassified":
            db.reopen_gap(gap["id"])
        # else: closed_reason == 'planned' (or unknown/legacy) -> the human
        # did the work, or we cannot prove he did not. Leave it closed.
    db.close_gaps_not_in(uid, hard_none)


def run_gate(db, job_row, master, api_key=None, jd_text=None,
             fetch_jd=None, call=None):
    """Run the gate for one job. One LLM call. Fails closed on every error path.

    jd_text/fetch_jd/call are injectable so this tests offline.
    """
    # Nothing reads `master` until it has been validated, title_check included.
    # title_check calls master.get(), so a wrong-shaped file (an emptied
    # master_resume.yaml parses as None, a stray leading dash makes it a list)
    # raised AttributeError straight out of run_gate, past the _fail handler
    # below. No ERROR was recorded, the job kept whatever decision it last had,
    # and require_pass() would then draft a stale PROCEED against a corrupt
    # ledger. A gate that fails open is not a gate. The neutral title stands in
    # only for the window before validation, so _fail can always serialize one.
    title = {"role_noun": "", "mismatch": False, "note": ""}
    model = None  # provenance; set once the provider resolves

    def _fail(reason):
        cnt = {"known_hard_none": 0, "unresolved": 0}
        reqs = []
        path = _report_path(job_row)
        path.write_text(
            f"# Fit report: {_no_dash(job_row['title'])}\n\n"
            f"## Decision: ERROR\n\nThe gate could not run: {_no_dash(reason)}.\n\n"
            "A gate that fails open is not a gate, so drafting stays blocked. "
            "Fix the cause and re-run, or override with a written reason.\n")
        db.set_gate(job_row["uid"], ERROR,
                    json.dumps({"requirements": [], "title": title,
                                "error": reason, "model": model}),
                    str(path), model=model)
        return {"decision": ERROR, "requirements": reqs, "title": title,
                "report_path": path, "counts": cnt, "error": reason}

    try:
        caps, dnc = load_profile(master)
    except ProfileError as e:
        return _fail(f"the capability ledger in master_resume.yaml is malformed: {e}")

    title = title_check(job_row["title"], master)

    try:
        provider = llm.resolve_provider(component="gate")
    except ValueError as e:
        return _fail(f"unrecognized JOB_PROVIDER: {e}")
    if api_key and provider.name == llm.DEFAULT_PROVIDER:
        provider = provider._replace(api_key=api_key)
    api_key = provider.api_key
    model = provider.model

    if not api_key:
        return _fail(f"{provider.name} API key not set")

    if jd_text is None:
        try:
            fetch_jd = fetch_jd or _default_fetch
            jd_text = fetch_jd(job_row)
        except Exception as e:
            return _fail(f"could not fetch the job description: {e}")
    if not (jd_text or "").strip():
        return _fail("the job description was empty")

    try:
        # extract() reads the job with dict.get(); job_row may be a sqlite3.Row,
        # which has no .get(), so pass a plain dict. Grade against the whole resume,
        # not just the capabilities highlights, so real experience is not a false NONE.
        raw = extract(dict(job_row), jd_text, build_evidence(master), api_key, call=call)
    except TruncatedResponse as e:
        return _fail(str(e))
    except Exception as e:
        return _fail(f"the model's response could not be parsed: {e}")

    # Sweep the raw JD before enforcing: the ledger must fire on disqualifying
    # prose the extractor never turned into a requirement (see ledger_sweep).
    reqs = enforce(raw + ledger_sweep(jd_text, dnc, raw), dnc)
    # Second, one-directional pass: catch requirements that MEAN a forbidden
    # competency in words the substring ledger has not seen. Can only harshen. A
    # failure here leaves the enforced (ledger) decision untouched, never blocks.
    reqs, screen = semantic_screen(reqs, dnc, api_key, call=call)
    skills_decision = decide(reqs)
    loc = location_ok(dict(job_row), jd_text)
    decision = _apply_location(skills_decision, loc)
    cnt = counts(reqs)
    path = _persist(db, job_row, reqs, title, decision, cnt,
                    loc=loc, skills_decision=skills_decision, model=model,
                    screen=screen)
    _reconcile_gaps(db, job_row["uid"], reqs)

    return {"decision": decision, "requirements": reqs, "title": title,
            "report_path": path, "counts": cnt, "location": loc,
            "skills_decision": skills_decision, "model": model, "screen": screen}


def _default_fetch(job_row):
    import job_generate
    return job_generate.fetch_description(job_row)


def require_pass(db, job_row):
    """Raise GateBlocked unless this job may produce artifacts.

    Called from job_generate.generate(), which is the single choke point every
    artifact passes through. Guarding the CLI instead would leave the MCP tool
    that a chat agent calls from Discord wide open.
    """
    # An override waives a decision that was actually rendered. set_gate clears the override on
    # every fresh decision, so a surviving override necessarily belongs to the current one.
    if (job_row["gate_override_reason"] or "").strip() and job_row["gate_at"]:
        return

    decision = job_row["gate_decision"]

    if not decision:
        raise GateBlocked(
            f"{job_row['slug']} has not been gated. "
            f"Run: jh gate {job_row['slug']}")

    if decision in (RECOMMEND, PROCEED):
        return

    if decision == CONDITIONAL:
        unplanned = db.unplanned_gaps(job_row["uid"])
        if not unplanned:
            return
        names = ", ".join(g["requirement"] for g in unplanned)
        raise GateBlocked(
            f"{job_row['slug']} is CONDITIONAL and has {len(unplanned)} "
            f"unplanned gap(s): {names}. Each needs a plan, an hours estimate, "
            f"and a deadline before a package is generated. "
            f"Run: jh gaps {job_row['slug']}")

    label = _DECISION_LABEL.get(decision, decision)
    raise GateBlocked(
        f"{job_row['slug']} gated {label}. See {job_row['gate_report_path']}. "
        f"To proceed anyway: jh gate-override {job_row['slug']} --reason \"...\"")


def recompute(db, job_row):
    """Re-decide from the stored gate_json. No API call, no network.

    Used after the operator rules on an UNSURE item.
    """
    stored = json.loads(job_row["gate_json"] or "{}")
    reqs = stored.get("requirements") or []
    title = stored.get("title") or {}
    loc = stored.get("location")
    # Preserve provenance: recompute makes no API call, so the model that
    # actually produced these requirements is whatever the original run
    # stamped into gate_json, not None. Backfilled pre-provenance rows have
    # gate_json with no "model" key but do have the gate_model column set,
    # so fall back to the column rather than erasing it with None.
    model = stored.get("model") or job_row["gate_model"]
    # Same reason: _persist rewrites gate_json wholesale, so anything not passed
    # back through is erased. Dropping "screen" would quietly delete the record
    # that the semantic pass failed on the original run, taking the report's
    # "this rests on the substring ledger alone" warning with it, the moment an
    # unrelated requirement gets ruled on.
    screen = stored.get("screen")
    skills_decision = decide(reqs)
    decision = _apply_location(skills_decision, loc)
    cnt = counts(reqs)
    path = _persist(db, job_row, reqs, title, decision, cnt,
                    loc=loc, skills_decision=skills_decision, model=model,
                    screen=screen)
    _reconcile_gaps(db, job_row["uid"], reqs)
    return {"decision": decision, "requirements": reqs, "title": title,
            "report_path": path, "counts": cnt, "location": loc,
            "skills_decision": skills_decision, "model": model,
            "screen": screen}

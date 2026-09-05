# job-hound Retargeting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-target job-hound away from elite tech firms toward employers where the operator has real odds (mid-market, regional within ~3h drive, gov/defense, enterprise), and raise filter precision so the larger list returns relevant roles instead of noise.

**Architecture:** Three layers change. (1) `job_monitor.py` gets word-boundary matching to fix the international location leak, plus additive enrichment fields on each match (`company_display`, `category`, `location_type`) surfaced only in the scanner's own reporting. (2) `companies.yaml` gets precise title/exclude/location term lists and a categorized, slug-verified company list. (3) A stdlib `unittest` filter test locks the behavior. DB and CLI are untouched.

**Tech Stack:** Python 3, `requests`, `pyyaml`, `unittest` (stdlib, no new dependency).

---

## Pre-flight notes

- Run everything from the project root with the venv active: `source .venv/bin/activate`.
- **This directory is not git-initialized.** Either run `git init` once before starting (recommended, so the commit steps work), or skip every `git commit` step. Commit steps below are marked accordingly.
- The full reference design is in `docs/superpowers/specs/2026-06-16-job-hound-retargeting-design.md`.
- `j['company']` (the slug) is load-bearing for uid/slug generation and the Greenhouse date-upgrade call in `job_cli.py`. **Never reassign it.** Enrichment uses new keys only.

---

## Task 1: Word-boundary matching in the filter

**Files:**
- Modify: `job_monitor.py` (`compile_patterns`, ~line 228; add `REMOTE_TERMS` constant near other module constants ~line 85)
- Test: `tests/test_filters.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_filters.py`:

```python
import unittest
import job_monitor as jm

# Starting term lists mirror the new companies.yaml (Task 3). Kept inline so the
# filter behavior is tested independently of the config file.
TITLE_TERMS = [
    "site reliability", "SRE", "production engineer", "platform engineer",
    "cloud engineer", "cloud architect", "cloud infrastructure",
    "infrastructure engineer", "devops engineer", "devops", "devsecops",
    "ml platform", "ai platform", "solutions architect",
    "director of infrastructure", "director of platform", "head of platform",
    "engineering manager",
]
EXCLUDE_TERMS = [
    "intern", "junior", "jr", "new grad", "sales", "sales engineer",
    "recruiter", "compensation", "logistics", "supply chain", "real estate",
    "investment", "human resources", "HR business", "program manager",
    "product manager", "PR",
]
# remote terms + within-3h cities + united states
LOCATION_TERMS = jm.REMOTE_TERMS + [
    "portland", "beaverton", "charlotte", "greenville", "spartanburg",
    "knoxville", "columbia", "winston-salem", "greensboro", "atlanta",
    "chattanooga", "johnson city", "kingsport", "bristol", "hickory", "gastonia",
]


def mk(title, location):
    return {"title": title, "location": location}


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.tp = jm.compile_patterns(TITLE_TERMS)
        self.lp = jm.compile_patterns(LOCATION_TERMS, boundary=True)
        self.ep = jm.compile_patterns(EXCLUDE_TERMS, boundary=True)

    def keep(self, title, loc):
        return jm.matches(mk(title, loc), self.tp, self.lp, self.ep)

    # --- should KEEP ---
    def test_remote_sre_kept(self):
        self.assertTrue(self.keep("Senior Site Reliability Engineer", "United States"))

    def test_charlotte_platform_kept(self):
        self.assertTrue(self.keep("Platform Engineer", "Charlotte, NC"))

    def test_scoped_leadership_kept(self):
        self.assertTrue(self.keep("Director of Infrastructure", "Remote"))

    def test_wholesale_does_not_trigger_sales_exclude(self):
        # 'sales' must not match inside 'wholesale' (word boundary)
        self.assertTrue(self.keep("Wholesale Platform Engineer", "Remote"))

    # --- should DROP ---
    def test_sunnyvale_substring_regression(self):
        # OLD BUG: 'us' matched inside 'Sunnyvale'. On-site CA, not in radius.
        self.assertFalse(self.keep("Site Reliability Engineer", "Sunnyvale, CA"))

    def test_foreign_location_dropped(self):
        self.assertFalse(self.keep("Site Reliability Engineer", "Bangkok, th"))

    def test_hr_director_dropped_no_title_term(self):
        self.assertFalse(self.keep("Senior Director, HR Business Partnering", "Remote"))

    def test_compensation_director_dropped(self):
        self.assertFalse(self.keep("Equity and Executive Compensation Director", "Remote"))

    def test_logistics_director_dropped(self):
        self.assertFalse(self.keep("Director - Logistics & Supply Chain", "Charlotte, NC"))

    def test_sales_engineer_dropped(self):
        self.assertFalse(self.keep("Sales Engineer, Platform", "Remote"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `source .venv/bin/activate && python -m unittest tests.test_filters -v`
Expected: FAIL — `AttributeError: module 'job_monitor' has no attribute 'REMOTE_TERMS'` (and `compile_patterns()` has no `boundary` arg).

- [ ] **Step 3: Add the `REMOTE_TERMS` constant**

In `job_monitor.py`, near the other module constants (after `SLEEP_BETWEEN_CALLS`, ~line 85), add:

```python
# Location tokens that mean "remote-eligible" (used for the location_type tag and
# included in companies.yaml location_terms). "united states" is a documented
# heuristic: ATS listings that name the whole country are almost always remote-US.
REMOTE_TERMS = ["remote", "anywhere", "distributed", "united states"]
```

- [ ] **Step 4: Add word-boundary support to `compile_patterns`**

Replace `compile_patterns` (~line 228):

```python
def compile_patterns(terms, boundary=False):
    # boundary=True wraps each term in \b...\b so short tokens like "us" or "PR"
    # match whole words only (fixes "us" matching inside "Sunnyvale").
    pats = []
    for t in terms:
        esc = re.escape(t)
        if boundary:
            esc = r"\b" + esc + r"\b"
        pats.append(re.compile(esc, re.IGNORECASE))
    return pats
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `source .venv/bin/activate && python -m unittest tests.test_filters -v`
Expected: PASS (10 tests OK).

- [ ] **Step 6: Commit** *(skip if repo not git-initialized)*

```bash
git add job_monitor.py tests/test_filters.py
git commit -m "[job_monitor]: word-boundary filter matching + filter tests"
```

---

## Task 2: Enrich matches and reporting

**Files:**
- Modify: `job_monitor.py` (`run_scan` ~line 317, `write_report` ~line 294, `notify_discord` ~line 269, dry-run print in `main` ~line 401)
- Test: `tests/test_enrich.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_enrich.py`:

```python
import unittest
from unittest import mock
import job_monitor as jm

CFG = {
    "title_terms": ["site reliability", "platform engineer"],
    "location_terms": jm.REMOTE_TERMS + ["charlotte"],
    "exclude_terms": ["sales"],
    "companies": [
        {"name": "Acme Co", "ats": "greenhouse", "slug": "acme",
         "category": "mid_market"},
    ],
}

FAKE_JOBS = [
    {"id": "1", "title": "Senior Site Reliability Engineer", "location": "Remote",
     "url": "u1", "company": "acme", "ats": "greenhouse",
     "posted_at": "", "date_source": ""},
    {"id": "2", "title": "Platform Engineer", "location": "Charlotte, NC",
     "url": "u2", "company": "acme", "ats": "greenhouse",
     "posted_at": "", "date_source": ""},
]


class EnrichTests(unittest.TestCase):
    def test_enrichment_fields(self):
        with mock.patch.object(jm, "fetch_greenhouse", return_value=FAKE_JOBS):
            new, all_matches, manual = jm.run_scan(CFG, seen=set())
        self.assertEqual(len(all_matches), 2)
        by_id = {j["id"]: j for j in all_matches}
        # company slug preserved (load-bearing); display name added separately
        self.assertEqual(by_id["1"]["company"], "acme")
        self.assertEqual(by_id["1"]["company_display"], "Acme Co")
        self.assertEqual(by_id["1"]["category"], "mid_market")
        self.assertEqual(by_id["1"]["location_type"], "remote")
        self.assertEqual(by_id["2"]["location_type"], "onsite/hybrid")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `source .venv/bin/activate && python -m unittest tests.test_enrich -v`
Expected: FAIL — `KeyError: 'company_display'`.

- [ ] **Step 3: Enrich matches in `run_scan`**

In `job_monitor.py`, inside `run_scan`, locate the per-company block that computes
`hits` (~line 362):

```python
        hits = [j for j in jobs if matches(j, title_pats, location_pats, exclude_pats)]
        if verbose:
            print(f"  {name} ({ats}): {len(jobs)} total, {len(hits)} match filters")
        all_matches.extend(hits)
```

Replace with:

```python
        hits = [j for j in jobs if matches(j, title_pats, location_pats, exclude_pats)]
        category = c.get("category", "")
        for j in hits:
            j["company_display"] = name
            j["category"] = category
            loc = j.get("location", "")
            j["location_type"] = (
                "remote" if any(p.search(loc) for p in remote_pats) else "onsite/hybrid"
            )
        if verbose:
            print(f"  {name} ({ats}): {len(jobs)} total, {len(hits)} match filters")
        all_matches.extend(hits)
```

Then, where the other pattern lists are compiled at the top of `run_scan` (~line 324), add `remote_pats`:

```python
    title_pats = compile_patterns(cfg.get("title_terms", []))
    location_pats = compile_patterns(cfg.get("location_terms", []), boundary=True)
    exclude_pats = compile_patterns(cfg.get("exclude_terms", []), boundary=True)
    remote_pats = compile_patterns(REMOTE_TERMS, boundary=True)
```

(Note this also switches `location_pats`/`exclude_pats` to `boundary=True` — that is the intended wiring of Task 1's fix into the live scan.)

- [ ] **Step 4: Run the test to verify it passes**

Run: `source .venv/bin/activate && python -m unittest tests.test_enrich -v`
Expected: PASS.

- [ ] **Step 5: Use the new fields in reporting**

In `write_report` (~line 303), replace the match line:

```python
            for j in new_jobs:
                f.write(f"- **{j['title']}** | {j['company']} ({j['ats']}) | "
                        f"{j['location'] or 'n/a'}\n  {j['url']}\n")
```

with:

```python
            for j in new_jobs:
                disp = j.get("company_display", j["company"])
                cat = j.get("category", "")
                lt = j.get("location_type", "")
                tag = " ".join(t for t in [cat, lt] if t)
                f.write(f"- **{j['title']}** | {disp} ({j['ats']}) | "
                        f"{j['location'] or 'n/a'}"
                        f"{(' [' + tag + ']') if tag else ''}\n  {j['url']}\n")
```

In `notify_discord` (~line 273), replace the `lines` comprehension:

```python
    lines = [
        f"**{j['title']}** - {j['company']} ({j['location'] or 'n/a'})\n{j['url']}"
        for j in new_jobs
    ]
```

with:

```python
    lines = [
        f"**{j['title']}** - {j.get('company_display', j['company'])} "
        f"({j['location'] or 'n/a'}) "
        f"[{' '.join(t for t in [j.get('category',''), j.get('location_type','')] if t)}]"
        f"\n{j['url']}"
        for j in new_jobs
    ]
```

In `main`, the dry-run print (~line 403), replace:

```python
        for j in all_matches:
            print(f"  - {j['title']} | {j['company']} ({j['ats']}) | {j['location'] or 'n/a'}")
            print(f"    {j['url']}")
```

with:

```python
        for j in all_matches:
            disp = j.get("company_display", j["company"])
            tag = " ".join(t for t in [j.get("category", ""), j.get("location_type", "")] if t)
            print(f"  - {j['title']} | {disp} ({j['ats']}) | {j['location'] or 'n/a'}"
                  f"{(' [' + tag + ']') if tag else ''}")
            print(f"    {j['url']}")
```

- [ ] **Step 6: Re-run both test modules**

Run: `source .venv/bin/activate && python -m unittest tests.test_filters tests.test_enrich -v`
Expected: PASS (all tests).

- [ ] **Step 7: Commit** *(skip if repo not git-initialized)*

```bash
git add job_monitor.py tests/test_enrich.py
git commit -m "[job_monitor]: enrich matches with display name, category, location_type"
```

---

## Task 3: Rewrite the filter term lists in `companies.yaml`

**Files:**
- Modify: `companies.yaml` (`title_terms`, `location_terms`, `exclude_terms`, header comment)

- [ ] **Step 1: Replace `title_terms`**

```yaml
title_terms:
  # senior IC
  - "site reliability"
  - "SRE"
  - "production engineer"
  - "platform engineer"
  - "platform engineering"
  - "cloud engineer"
  - "cloud architect"
  - "cloud infrastructure"
  - "cloud operations"
  - "infrastructure engineer"
  - "reliability engineer"
  - "devops engineer"
  - "devops"
  - "devsecops"
  - "ml platform"
  - "machine learning platform"
  - "ai infrastructure"
  - "ai platform"
  - "solutions architect"
  # scoped leadership (bare "director"/"head of" deliberately omitted - too noisy)
  - "director of infrastructure"
  - "director of platform"
  - "director of engineering"
  - "director of site reliability"
  - "director of cloud"
  - "infrastructure director"
  - "platform director"
  - "engineering director"
  - "head of infrastructure"
  - "head of platform"
  - "head of engineering"
  - "head of sre"
  - "head of reliability"
  - "head of cloud"
  - "VP engineering"
  - "VP of engineering"
  - "VP infrastructure"
  - "VP of infrastructure"
  - "vice president, engineering"
  - "engineering manager"   # noisiest term; tune exclude_terms if too loose
```

- [ ] **Step 2: Replace `location_terms`**

```yaml
# Matched with word boundaries (see compile_patterns boundary=True). A role passes
# if it is remote-eligible OR physically within ~3h drive of Beaverton NC.
location_terms:
  # remote-eligible
  - "remote"
  - "anywhere"
  - "distributed"
  - "united states"
  # within ~3h drive
  - "portland"
  - "beaverton"
  - "charlotte"
  - "greenville"
  - "spartanburg"
  - "knoxville"
  - "columbia"
  - "winston-salem"
  - "greensboro"
  - "atlanta"
  - "chattanooga"
  - "johnson city"
  - "kingsport"
  - "bristol"
  - "hickory"
  - "gastonia"
```

- [ ] **Step 3: Replace `exclude_terms`**

```yaml
exclude_terms:
  - "intern"
  - "junior"
  - "jr"
  - "new grad"
  - "entry level"
  - "entry-level"
  - "apprentice"
  - "sales"
  - "account executive"
  - "sales engineer"
  - "recruiter"
  - "legal"
  - "attorney"
  - "counsel"
  - "paralegal"
  - "tax"
  - "communications"
  - "public relations"
  - "PR"
  - "procurement"
  - "product manager"
  - "program manager"
  - "clinical"
  - "nurse"
  - "nursing"
  - "accountant"
  - "accounting"
  - "controller"
  - "human resources"
  - "HR business"
  - "talent"
  - "compensation"
  - "real estate"
  - "logistics"
  - "supply chain"
  - "investment"
  - "marketing"
```

- [ ] **Step 4: Verify the config still parses**

Run: `source .venv/bin/activate && python -c "import yaml; yaml.safe_load(open('companies.yaml')); print('ok')"`
Expected: `ok`

- [ ] **Step 5: Re-run the filter tests against the live config shape**

Run: `source .venv/bin/activate && python -m unittest tests.test_filters -v`
Expected: PASS (term lists in the test mirror these; behavior unchanged).

- [ ] **Step 6: Commit** *(skip if repo not git-initialized)*

```bash
git add companies.yaml
git commit -m "[companies.yaml]: precise title/exclude/location term lists"
```

---

## Task 4: Build and verify the categorized company list

This task is research + verification, not TDD. The deliverable is a `companies:`
block where every scannable slug resolves. Work category by category.

**Files:**
- Modify: `companies.yaml` (`companies:` block; remove the elite entries)

- [ ] **Step 1: Remove the elite companies**

Delete the current `companies:` entries under "AI-native / high-growth" and
"Cloud / infra vendors & scale-ups" (Anthropic, OpenAI, Databricks, Datadog,
Cloudflare, MongoDB, etc.). Keep the existing manual gov entries (Capital One,
Snowflake, Peraton, Booz Allen, Leidos, GDIT) and add `category: gov_defense` to
each.

- [ ] **Step 2: Draft candidates per category**

For each of `mid_market`, `regional`, `gov_defense`, `enterprise`, assemble
candidate companies known (or likely) to use Greenhouse / Lever / Ashby /
SmartRecruiters. Prioritize: remote-friendly, HQ within ~3h drive (Charlotte,
Greenville/Spartanburg, Knoxville, Columbia, Atlanta, etc.), age-friendly, and
clearable/public-trust for gov. Use WebSearch/WebFetch to confirm the careers
page and the ATS slug for each candidate. Write each as:

```yaml
  - {name: "Company Name", ats: greenhouse, slug: theslug, category: mid_market}
```

Companies with no public feed (Workday/iCIMS): record with `careers_url` and
`category`, e.g.:

```yaml
  - {name: "Regional Bank", ats: workday, careers_url: "https://...", category: regional}
```

Aim for a meaningful list (target ~30-60 scannable companies across categories);
quality and correct slugs matter more than count.

- [ ] **Step 3: Run the verification dry-run**

Run: `source .venv/bin/activate && python job_monitor.py --dry-run 2>scan.err`
Then inspect failures: `grep -iE "HTTP|skipping|parse error" scan.err`

- [ ] **Step 4: Fix or drop every failing slug**

For each company logged as HTTP 404/403 or parse error, open its real careers
page, find the correct slug, and correct it — or drop the company if it has no
public feed (move it to a manual `workday`/`icims` entry with `careers_url`).
Re-run Step 3 until no scannable company errors.

- [ ] **Step 5: Confirm the config parses and matches return**

Run: `source .venv/bin/activate && python job_monitor.py --dry-run 2>/dev/null | tail -40`
Expected: a list of matches showing real company names with `[category location_type]`
tags, and no foreign-only locations.

- [ ] **Step 6: Commit** *(skip if repo not git-initialized)*

```bash
git add companies.yaml
git commit -m "[companies.yaml]: categorized, slug-verified company list (drop elite)"
```

---

## Task 5: Before/after delta and final verification

**Files:**
- None modified (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `source .venv/bin/activate && python -m unittest discover -s tests -v`
Expected: all tests PASS.

- [ ] **Step 2: Produce the delta summary**

Run a fresh dry-run and count matches and junk:

```bash
source .venv/bin/activate
python job_monitor.py --dry-run 2>/dev/null | tee scan-new.txt | grep -c "https"
# foreign-leak check (should be 0): non-US country-code suffixes in locations
grep -iE "\b(th|de|eg|br|mx|cz|in|sg|uk|fr)\b" scan-new.txt | grep -vi "remote\|united states" | head
```

Compare against the baseline (32 matches, ~50% junk, foreign roles leaking).
Expected: more relevant matches, the baseline junk families gone, zero foreign-only roles.

- [ ] **Step 3: Report results to the operator**

Summarize: total matches, breakdown by category, a few example roles, and any
companies that had to be moved to the manual bucket. Confirm the success criteria
in the spec are met. Note any title term still producing noise (likely
`engineering manager`) as a tuning candidate.

---

## Self-review notes

- **Spec coverage:** company restructure (Task 4), filter overhaul (Tasks 1-3),
  location fix (Task 1), readable output (Task 2), build-and-verify method
  (Task 4), mocked filter tests + live scan + delta (Tasks 1-2, 5). All spec
  sections map to a task.
- **Non-goals respected:** no scoring layer, no DB schema change, no auto-apply,
  public ATS only.
- **Type consistency:** enrichment keys `company_display` / `category` /
  `location_type` and `compile_patterns(..., boundary=True)` are used identically
  across Tasks 1, 2, and the tests.

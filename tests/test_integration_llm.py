"""
LLM/Reranker eval tests — real API calls, run each scenario N times.

Like deepeval's trial-based approach: each eval runs N_TRIALS times and
must pass at least PASS_THRESHOLD of them. This catches stochastic failures
where the model occasionally produces nonsense.

Run with keys set:
    OPENROUTER_API_KEY=... pytest tests/test_integration_llm.py -v -s
    OPENROUTER_API_KEY=... JINA_API_KEY=... pytest tests/test_integration_llm.py -v -s

Estimated cost per full run: ~$0.01 at DeepSeek V4 Flash prices.
"""
from __future__ import annotations
import json
import os

import pytest
import requests

# ── Config ────────────────────────────────────────────────────────────────────

OR_KEY   = os.environ.get("OPENROUTER_API_KEY", "")
JINA_KEY = os.environ.get("JINA_API_KEY", "")
OR_MODEL = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")

needs_openrouter = pytest.mark.skipif(not OR_KEY, reason="OPENROUTER_API_KEY not set")
needs_jina       = pytest.mark.skipif(not JINA_KEY, reason="JINA_API_KEY not set")

N_TRIALS        = 5    # trials per eval
PASS_THRESHOLD  = 4    # must pass this many (80%) — allows 1 stochastic failure

# ── Eval runner ───────────────────────────────────────────────────────────────

def eval_n_times(label: str, fn, n: int = N_TRIALS, threshold: int = PASS_THRESHOLD):
    """
    Run fn() n times, collect pass/fail with detail, assert >= threshold pass.
    fn() should return a descriptive string on pass, raise AssertionError on fail.
    """
    passes = 0
    lines = []
    for i in range(n):
        try:
            detail = fn()
            passes += 1
            lines.append(f"  PASS trial {i + 1}: {detail}")
        except AssertionError as e:
            lines.append(f"  FAIL trial {i + 1}: {e}")
    pct = passes / n
    print(f"\n[{label}] {passes}/{n} passed ({pct:.0%})")
    for ln in lines:
        print(ln)
    assert passes >= threshold, (
        f"[{label}] passed {passes}/{n} ({pct:.0%}) — need {threshold}/{n} (80%)"
    )

# ── API helpers ───────────────────────────────────────────────────────────────

def _or_complete(prompt: str, temperature: float = 0.3) -> str:
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OR_KEY}", "Content-Type": "application/json"},
        json={
            "model": OR_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _parse_json(content: str) -> dict:
    if "```" in content:
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
    return json.loads(content.strip())


def _jina_rerank(query: str, documents: list[str]) -> list[dict]:
    resp = requests.post(
        "https://api.jina.ai/v1/rerank",
        headers={"Authorization": f"Bearer {JINA_KEY}", "Content-Type": "application/json"},
        json={
            "model": "jina-reranker-v3",
            "query": query,
            "documents": documents,
            "top_n": len(documents),
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["results"]

# ── Test data ─────────────────────────────────────────────────────────────────

CANDIDATE = (
    "Serina Zheng — Toronto-based B2B sales professional, 4 years experience. "
    "Quota-carrying Account Executive at two SaaS companies. Proficient in HubSpot "
    "and Salesforce. Closed $1.2M ARR last year. Seeking AE or senior BDR roles at "
    "growth-stage tech companies. Prefers hybrid or remote. Based in Toronto, ON."
)

JOBS = {
    "ae_saas": {
        "title": "Account Executive",
        "company": "CloudMetrics",
        "location": "Toronto, ON (Hybrid)",
        "description": (
            "Own the full sales cycle for our B2B SaaS platform. Manage a pipeline "
            "of 30+ mid-market accounts, run demos, negotiate contracts. HubSpot required. "
            "OTE $120k, $700k quota. 3+ yrs AE experience needed."
        ),
    },
    "bdr_remote": {
        "title": "Business Development Rep",
        "company": "SaaSly",
        "location": "Remote (Canada)",
        "description": (
            "Outbound prospecting via cold call, LinkedIn, and email sequences. "
            "Qualify inbound leads and hand off to AE. Salesforce CRM. Great stepping "
            "stone to AE. 1-2 yrs sales experience preferred."
        ),
    },
    "sales_mgr_manufacturing": {
        "title": "Sales Manager",
        "company": "Brampton Industrial Supply",
        "location": "Brampton, ON (On-site)",
        "description": (
            "Manage a team of 6 field reps selling industrial parts to manufacturing "
            "plants across Ontario. Deep knowledge of hydraulics and pneumatics an asset. "
            "5+ years in industrial or distribution sales required."
        ),
    },
    "ae_vancouver_onsite": {
        "title": "Account Executive",
        "company": "PacificSaaS",
        "location": "Vancouver, BC (On-site only)",
        "description": (
            "Join our growing Vancouver team selling to Canadian enterprise accounts. "
            "Must be located in or willing to relocate to Metro Vancouver. "
            "3+ yrs SaaS AE experience. Salesforce, full-cycle sales."
        ),
    },
    "driver": {
        "title": "Delivery Driver",
        "company": "SwiftRoute Logistics",
        "location": "Mississauga, ON",
        "description": (
            "Drive a 5-ton truck on routes across the GTA. Load and unload parcels "
            "up to 50 lbs. Valid G licence required. Forklift certification an asset. "
            "Monday–Friday, 6am–3pm. Union position."
        ),
    },
    "french_sales": {
        "title": "Représentant·e des ventes",
        "company": "Acme Québec",
        "location": "Montréal, QC (Présentiel)",
        "description": (
            "Vendre nos solutions logicielles aux PME québécoises. Maîtrise du français "
            "obligatoire. Connaissance du marché québécois essentielle. "
            "Expérience en vente B2B requise. Poste basé à Montréal."
        ),
    },
    "warehouse": {
        "title": "Warehouse Associate",
        "company": "Fulfillment Plus",
        "location": "Scarborough, ON",
        "description": (
            "Pick, pack, and ship e-commerce orders. Operate RF scanners and pallet "
            "jacks. Physically demanding role — must be able to stand for 10-hour shifts. "
            "No experience required, training provided."
        ),
    },
    "ae_fintech_toronto": {
        "title": "Account Executive — Fintech",
        "company": "PayEdge",
        "location": "Toronto, ON (Hybrid)",
        "description": (
            "Sell our B2B payments platform to CFOs and finance leaders at mid-market "
            "companies. Full-cycle from outbound to close. Salesforce, 2+ yrs AE required. "
            "OTE $110k. Strong preference for candidates with fintech or SaaS background."
        ),
    },
}


def _doc(job: dict) -> str:
    return f"{job['title']} at {job['company']} ({job['location']})\n{job['description']}"


SCORING_PROMPT = """\
You are evaluating a job listing for a specific candidate.

CANDIDATE:
{candidate}

JOB:
Title: {title}
Employer: {company}
Location: {location}
Description: {description}

Score how well this job fits the candidate 0-100:
- 90-100: exceptional fit on all dimensions
- 70-89: strong fit, minor gaps
- 50-69: partial fit, notable gaps
- below 50: poor fit

Use the full range. Avoid round numbers — 73 is better than 70.
Score below 30 if the role requires credentials clearly absent from the resume,
or is in an unrelated field.

Return ONLY valid JSON: {{"score": <integer 0-100>, "reason": "<1-2 sentences>"}}"""

RERANK_PROMPT = """\
Rank these job postings by relevance to this candidate.
Return ONLY valid JSON with no explanation.

CANDIDATE:
{candidate}

JOBS:
{job_lines}

Return: {{"ranked": [indices best to worst], "scores": [relevance 0.0-1.0 per ranked job]}}"""


# ── Scoring evals ─────────────────────────────────────────────────────────────

@needs_openrouter
def test_score_strong_fit_ae():
    """AE SaaS Toronto role must score >= 65 for this profile, 4/5 trials."""
    job = JOBS["ae_saas"]

    def trial():
        result = _parse_json(_or_complete(
            SCORING_PROMPT.format(candidate=CANDIDATE, **job)
        ))
        score = result["score"]
        assert score >= 65, f"score={score}, reason={result['reason']}"
        return f"score={score}"

    eval_n_times("score_strong_fit_ae", trial)


@needs_openrouter
def test_score_poor_fit_driver():
    """Delivery Driver must score <= 35 for a sales profile, 4/5 trials."""
    job = JOBS["driver"]

    def trial():
        result = _parse_json(_or_complete(
            SCORING_PROMPT.format(candidate=CANDIDATE, **job)
        ))
        score = result["score"]
        assert score <= 35, f"score={score}, reason={result['reason']}"
        return f"score={score}"

    eval_n_times("score_poor_fit_driver", trial)


@needs_openrouter
def test_score_gap_ae_vs_non_sales():
    """AE must outscore both Driver and Warehouse by >= 30 pts, 4/5 trials."""
    ae_job = JOBS["ae_saas"]
    bad_jobs = [JOBS["driver"], JOBS["warehouse"]]

    def trial():
        ae_result = _parse_json(_or_complete(
            SCORING_PROMPT.format(candidate=CANDIDATE, **ae_job)
        ))
        ae_score = ae_result["score"]
        gaps = []
        for bad in bad_jobs:
            bad_result = _parse_json(_or_complete(
                SCORING_PROMPT.format(candidate=CANDIDATE, **bad)
            ))
            gap = ae_score - bad_result["score"]
            assert gap >= 30, (
                f"AE ({ae_score}) vs {bad['title']} ({bad_result['score']}): gap={gap} < 30"
            )
            gaps.append(f"{bad['title']}={bad_result['score']}")
        return f"AE={ae_score}, others=[{', '.join(gaps)}]"

    eval_n_times("score_gap_ae_vs_non_sales", trial)


@needs_openrouter
def test_score_vancouver_penalised():
    """Vancouver on-site AE should score lower than Toronto hybrid AE, 4/5 trials."""
    toronto = JOBS["ae_saas"]
    vancouver = JOBS["ae_vancouver_onsite"]

    def trial():
        t = _parse_json(_or_complete(SCORING_PROMPT.format(candidate=CANDIDATE, **toronto)))
        v = _parse_json(_or_complete(SCORING_PROMPT.format(candidate=CANDIDATE, **vancouver)))
        assert t["score"] > v["score"], (
            f"Toronto AE ({t['score']}) should outscore Vancouver AE ({v['score']})"
        )
        return f"Toronto={t['score']}, Vancouver={v['score']}"

    eval_n_times("score_vancouver_penalised", trial)


# ── LLM re-ranking evals ──────────────────────────────────────────────────────

def _build_rerank_prompt(job_keys: list[str]) -> tuple[str, list[str]]:
    selected = [JOBS[k] for k in job_keys]
    job_lines = "\n".join(
        f"{i}. {j['title']} at {j['company']} ({j['location']}): {j['description'][:250]}"
        for i, j in enumerate(selected)
    )
    prompt = RERANK_PROMPT.format(candidate=CANDIDATE, job_lines=job_lines)
    return prompt, [j["title"] for j in selected]


@needs_openrouter
def test_llm_rerank_sales_top2():
    """AE and BDR must occupy top-2 out of 5 jobs (AE SaaS, BDR, Industrial, Driver, Warehouse), 4/5."""
    keys = ["ae_saas", "bdr_remote", "sales_mgr_manufacturing", "driver", "warehouse"]
    sales_indices = {0, 1}  # ae_saas, bdr_remote

    def trial():
        prompt, titles = _build_rerank_prompt(keys)
        result = _parse_json(_or_complete(prompt, temperature=0.1))
        ranked = result["ranked"]
        top2 = set(ranked[:2])
        missing = sales_indices - top2
        assert not missing, (
            f"Sales roles not in top 2: {[titles[i] for i in missing]}. Order: {[titles[i] for i in ranked]}"
        )
        return f"top2=[{titles[ranked[0]]}, {titles[ranked[1]]}]"

    eval_n_times("llm_rerank_sales_top2", trial)


@needs_openrouter
def test_llm_rerank_french_not_top2():
    """French Montréal job must NOT be in top 2 of 5, 4/5 trials."""
    keys = ["ae_saas", "bdr_remote", "ae_fintech_toronto", "driver", "french_sales"]
    french_idx = 4

    def trial():
        prompt, titles = _build_rerank_prompt(keys)
        result = _parse_json(_or_complete(prompt, temperature=0.1))
        ranked = result["ranked"]
        pos = ranked.index(french_idx)
        assert pos >= 2, (
            f"French job ranked {pos + 1}/5 — expected rank 3 or lower. "
            f"Order: {[titles[i] for i in ranked]}"
        )
        return f"French job rank={pos + 1}/5, order=[{', '.join(titles[i] for i in ranked)}]"

    eval_n_times("llm_rerank_french_not_top2", trial)


@needs_openrouter
def test_llm_rerank_unrelated_roles_bottom():
    """Driver and Warehouse must both be in bottom 3 of 6 jobs, 4/5 trials."""
    keys = ["ae_saas", "bdr_remote", "ae_fintech_toronto", "sales_mgr_manufacturing", "driver", "warehouse"]
    bad_indices = {4, 5}

    def trial():
        prompt, titles = _build_rerank_prompt(keys)
        result = _parse_json(_or_complete(prompt, temperature=0.1))
        ranked = result["ranked"]
        bottom3 = set(ranked[3:])
        missing = bad_indices - bottom3
        assert not missing, (
            f"Non-sales roles not in bottom 3: {[titles[i] for i in missing]}. "
            f"Order: {[titles[i] for i in ranked]}"
        )
        return f"bottom3=[{', '.join(titles[i] for i in ranked[3:])}]"

    eval_n_times("llm_rerank_unrelated_bottom", trial)


@needs_openrouter
def test_llm_rerank_toronto_over_vancouver():
    """Toronto hybrid AE must rank above Vancouver on-site AE for this Toronto candidate, 4/5."""
    keys = ["ae_saas", "bdr_remote", "ae_vancouver_onsite", "driver", "warehouse"]
    toronto_idx = 0
    vancouver_idx = 2

    def trial():
        prompt, titles = _build_rerank_prompt(keys)
        result = _parse_json(_or_complete(prompt, temperature=0.1))
        ranked = result["ranked"]
        t_pos = ranked.index(toronto_idx)
        v_pos = ranked.index(vancouver_idx)
        assert t_pos < v_pos, (
            f"Toronto AE (rank {t_pos + 1}) should beat Vancouver AE (rank {v_pos + 1})"
        )
        return f"Toronto rank={t_pos + 1}, Vancouver rank={v_pos + 1}"

    eval_n_times("llm_rerank_toronto_over_vancouver", trial)


# ── Jina re-ranking evals ─────────────────────────────────────────────────────

@needs_jina
def test_jina_rerank_sales_top2():
    """Jina: AE and BDR must be in top 2 of 5, 4/5 trials."""
    keys = ["ae_saas", "bdr_remote", "sales_mgr_manufacturing", "driver", "warehouse"]
    jobs = [JOBS[k] for k in keys]
    titles = [j["title"] + " @ " + j["company"] for j in jobs]
    sales_indices = {0, 1}
    documents = [_doc(j) for j in jobs]

    def trial():
        results = _jina_rerank(CANDIDATE, documents)
        ranked = [r["index"] for r in results]
        top2 = set(ranked[:2])
        missing = sales_indices - top2
        assert not missing, (
            f"Sales roles missing from top 2: {[titles[i] for i in missing]}. "
            f"Scores: {[(titles[r['index']], round(r['relevance_score'], 3)) for r in results]}"
        )
        return f"top2=[{titles[ranked[0]]}, {titles[ranked[1]]}]"

    eval_n_times("jina_rerank_sales_top2", trial)


@needs_jina
def test_jina_rerank_all_six():
    """Jina: Driver and Warehouse must both land in bottom 3 of 6, 4/5 trials."""
    keys = ["ae_saas", "bdr_remote", "ae_fintech_toronto", "sales_mgr_manufacturing", "driver", "warehouse"]
    jobs = [JOBS[k] for k in keys]
    titles = [j["title"] + " @ " + j["company"] for j in jobs]
    bad_indices = {4, 5}
    documents = [_doc(j) for j in jobs]

    def trial():
        results = _jina_rerank(CANDIDATE, documents)
        ranked = [r["index"] for r in results]
        bottom3 = set(ranked[3:])
        missing = bad_indices - bottom3
        assert not missing, (
            f"Non-sales roles not in bottom 3: {[titles[i] for i in missing]}. "
            f"Order: {[titles[i] for i in ranked]}"
        )
        scores_str = ", ".join(f"{titles[r['index']]}={r['relevance_score']:.3f}" for r in results)
        return f"bottom3=[{', '.join(titles[i] for i in ranked[3:])}] | {scores_str}"

    eval_n_times("jina_rerank_all_six", trial)


@needs_jina
def test_jina_rerank_french_penalised():
    """Jina: French Montréal job must rank 4th or 5th out of 5 for English Toronto candidate, 4/5."""
    keys = ["ae_saas", "bdr_remote", "ae_fintech_toronto", "driver", "french_sales"]
    jobs = [JOBS[k] for k in keys]
    titles = [j["title"] + " @ " + j["company"] for j in jobs]
    french_idx = 4
    documents = [_doc(j) for j in jobs]

    def trial():
        results = _jina_rerank(CANDIDATE, documents)
        ranked = [r["index"] for r in results]
        pos = ranked.index(french_idx)
        assert pos >= 3, (
            f"French job ranked {pos + 1}/5 — expected rank 4 or 5. "
            f"Order: {[titles[i] for i in ranked]}"
        )
        return f"French rank={pos + 1}/5"

    eval_n_times("jina_rerank_french_penalised", trial)

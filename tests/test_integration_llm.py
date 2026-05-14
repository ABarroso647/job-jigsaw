"""
Integration / eval tests — hit real APIs with obvious correct answers.

These are NOT run in normal unit test passes. They skip automatically
when API keys aren't set. Run them manually to validate behaviour:

    OPENROUTER_API_KEY=... pytest tests/test_integration_llm.py -v
    OPENROUTER_API_KEY=... JINA_API_KEY=... pytest tests/test_integration_llm.py -v

All assertions are sanity-level: "the delivery driver should NOT rank above
the sales role for a Toronto sales professional." If these fail it means the
model/reranker is producing nonsense for our use case.

Each test is labelled with estimated token cost at DeepSeek V4 Flash prices.
"""
from __future__ import annotations
import json
import os
import sys

import pytest
import requests

# ── Skip markers ──────────────────────────────────────────────────────────────

OR_KEY   = os.environ.get("OPENROUTER_API_KEY", "")
JINA_KEY = os.environ.get("JINA_API_KEY", "")

needs_openrouter = pytest.mark.skipif(not OR_KEY, reason="OPENROUTER_API_KEY not set")
needs_jina       = pytest.mark.skipif(not JINA_KEY, reason="JINA_API_KEY not set")

OR_MODEL         = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
OR_URL           = "https://openrouter.ai/api/v1/chat/completions"
JINA_RERANK_URL  = "https://api.jina.ai/v1/rerank"
JINA_MODEL       = "jina-reranker-v3"

# ── Shared fixtures ───────────────────────────────────────────────────────────

SALES_RESUME = (
    "Serina Zheng — Toronto-based B2B sales professional. 4 years closing SaaS deals "
    "at growth-stage tech companies. Quota-carrying AE, proficient in HubSpot and "
    "Salesforce. Seeking Account Executive or BDR roles, hybrid or remote."
)

JOBS = [
    {
        "title": "Account Executive",
        "company": "CloudCo",
        "location": "Toronto, ON (Hybrid)",
        "description": "Drive B2B SaaS revenue, manage HubSpot CRM, close 6-figure enterprise deals. 3+ yrs AE experience required.",
    },
    {
        "title": "BDR",
        "company": "SaaSCorp",
        "location": "Toronto, ON (Remote)",
        "description": "Outbound prospecting via cold call and email. Qualify leads for AE handoff. Salesforce required.",
    },
    {
        "title": "Delivery Driver",
        "company": "LogisticsFast",
        "location": "Brampton, ON",
        "description": "Drive cargo vans across the GTA. Valid G licence required. Forklift certification an asset.",
    },
    {
        "title": "Gérant·e des ventes",
        "company": "Acme Québec",
        "location": "Montréal, QC",
        "description": "Diriger une équipe de vente. Expérience en gestion requise. Bilinguisme essentiel.",
    },
]


def _or_complete(prompt: str, temperature: float = 0.3) -> str:
    resp = requests.post(
        OR_URL,
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


# ── Scoring evals (OpenRouter) ────────────────────────────────────────────────

@needs_openrouter
def test_score_ae_above_50():
    """Account Executive should score well above 50 for this sales profile. (~200 tokens)"""
    job = JOBS[0]
    prompt = f"""\
You are evaluating a job listing for a specific candidate.

CANDIDATE:
{SALES_RESUME}

JOB:
Title: {job['title']}
Employer: {job['company']}
Location: {job['location']}
Description: {job['description']}

Score how well this job fits the candidate 0-100:
- 90-100: exceptional fit on all dimensions
- 70-89: strong fit, minor gaps
- 50-69: partial fit, notable gaps
- below 50: poor fit

Use the full range. Avoid round numbers.
Return ONLY valid JSON: {{"score": <integer 0-100>, "reason": "<1-2 sentences>"}}"""

    result = _parse_json(_or_complete(prompt))
    score = result["score"]
    print(f"\nAE score: {score} — {result['reason']}")
    assert score >= 60, f"Expected AE score >= 60, got {score}"


@needs_openrouter
def test_score_driver_below_ae():
    """Delivery Driver should score significantly lower than Account Executive. (~400 tokens)"""
    scores = {}
    for job in [JOBS[0], JOBS[2]]:  # AE and Driver
        prompt = f"""\
You are evaluating a job listing for a specific candidate.

CANDIDATE:
{SALES_RESUME}

JOB:
Title: {job['title']}
Employer: {job['company']}
Location: {job['location']}
Description: {job['description']}

Score 0-100. Avoid round numbers.
Return ONLY valid JSON: {{"score": <integer>, "reason": "<1-2 sentences>"}}"""

        result = _parse_json(_or_complete(prompt))
        scores[job["title"]] = result["score"]
        print(f"\n{job['title']}: {result['score']} — {result['reason']}")

    assert scores["Account Executive"] > scores["Delivery Driver"], (
        f"AE ({scores['Account Executive']}) should outscore Driver ({scores['Delivery Driver']})"
    )
    assert scores["Delivery Driver"] < 40, (
        f"Driver score should be < 40 for a sales profile, got {scores['Delivery Driver']}"
    )


@needs_openrouter
def test_llm_rerank_sales_order():
    """LLM re-ranker should put AE/BDR above Delivery Driver for a sales profile. (~500 tokens)"""
    job_lines = "\n".join(
        f'{i}. {j["title"]} at {j["company"]} ({j["location"]}): {j["description"][:200]}'
        for i, j in enumerate(JOBS[:3])  # AE, BDR, Driver
    )
    prompt = (
        "Rank these job postings by relevance to this candidate. "
        "Return ONLY valid JSON with no explanation.\n\n"
        f"CANDIDATE:\n{SALES_RESUME}\n\n"
        f"JOBS:\n{job_lines}\n\n"
        'Return: {"ranked": [indices best→worst], "scores": [relevance 0-1 per ranked job]}'
    )
    result = _parse_json(_or_complete(prompt, temperature=0.1))
    ranked = result["ranked"]
    print(f"\nLLM ranked order: {ranked} (0=AE, 1=BDR, 2=Driver)")

    driver_pos = ranked.index(2)
    assert driver_pos > 0, f"Delivery Driver should NOT be ranked #1, got position {driver_pos}"
    assert ranked[0] in (0, 1), f"AE or BDR should be #1, got index {ranked[0]}"


@needs_openrouter
def test_llm_rerank_filters_french_job():
    """French job should rank last or be scored very low for an English-profile candidate. (~600 tokens)"""
    job_lines = "\n".join(
        f'{i}. {j["title"]} at {j["company"]} ({j["location"]}): {j["description"][:200]}'
        for i, j in enumerate(JOBS)  # all 4 including French one
    )
    prompt = (
        "Rank these job postings by relevance to this candidate. "
        "Return ONLY valid JSON with no explanation.\n\n"
        f"CANDIDATE:\n{SALES_RESUME}\n\n"
        f"JOBS:\n{job_lines}\n\n"
        'Return: {"ranked": [indices best→worst], "scores": [relevance 0-1 per ranked job]}'
    )
    result = _parse_json(_or_complete(prompt, temperature=0.1))
    ranked = result["ranked"]
    scores = result.get("scores", [])
    french_pos = ranked.index(3)
    french_score = scores[french_pos] if french_pos < len(scores) else None
    print(f"\nAll ranked: {ranked} (3=French job), French score: {french_score}")

    assert french_pos >= 2, (
        f"French/Montréal job should rank 3rd or 4th for a Toronto English-profile candidate, "
        f"got position {french_pos}"
    )


# ── Jina re-ranker evals ──────────────────────────────────────────────────────

@needs_jina
def test_jina_rerank_sales_order():
    """Jina should put AE/BDR above Delivery Driver for a sales resume. (~300 tokens)"""
    jobs = JOBS[:3]  # AE, BDR, Driver
    documents = [
        f"{j['title']} at {j['company']} ({j['location']})\n{j['description']}"
        for j in jobs
    ]
    resp = requests.post(
        JINA_RERANK_URL,
        headers={"Authorization": f"Bearer {JINA_KEY}", "Content-Type": "application/json"},
        json={
            "model": JINA_MODEL,
            "query": SALES_RESUME,
            "documents": documents,
            "top_n": len(jobs),
        },
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json()["results"]
    ranked_indices = [r["index"] for r in results]
    print(f"\nJina ranked: {ranked_indices} (0=AE, 1=BDR, 2=Driver)")
    print("Scores:", [(r["index"], round(r["relevance_score"], 3)) for r in results])

    driver_pos = ranked_indices.index(2)
    assert driver_pos > 0, f"Delivery Driver should NOT be #1, got position {driver_pos}"
    assert ranked_indices[0] in (0, 1), f"AE or BDR should be #1, got {ranked_indices[0]}"


@needs_jina
def test_jina_rerank_all_four():
    """Jina should put French/Brampton job last for a Toronto sales profile. (~400 tokens)"""
    documents = [
        f"{j['title']} at {j['company']} ({j['location']})\n{j['description']}"
        for j in JOBS
    ]
    resp = requests.post(
        JINA_RERANK_URL,
        headers={"Authorization": f"Bearer {JINA_KEY}", "Content-Type": "application/json"},
        json={
            "model": JINA_MODEL,
            "query": SALES_RESUME,
            "documents": documents,
            "top_n": len(JOBS),
        },
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json()["results"]
    ranked_indices = [r["index"] for r in results]
    print(f"\nJina ranked all 4: {ranked_indices} (0=AE, 1=BDR, 2=Driver, 3=French)")
    print("Scores:", [(r["index"], round(r["relevance_score"], 3)) for r in results])

    last = ranked_indices[-1]
    assert last in (2, 3), (
        f"Last place should be Driver or French job, got index {last} ({JOBS[last]['title']})"
    )
    assert ranked_indices[0] in (0, 1), (
        f"AE or BDR should be #1, got {ranked_indices[0]} ({JOBS[ranked_indices[0]]['title']})"
    )

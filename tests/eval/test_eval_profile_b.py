"""
DeepEval-style LLM quality tests for Branch B — candidate wiki + resume health.

Uses the eval_n_times pattern (3 trials, 2/3 pass threshold = ~67%).

Run:
    OPENROUTER_API_KEY=... pytest tests/eval/test_eval_profile_b.py -v -s

Cost: ~$0.005 per full run at DeepSeek V4 Flash prices.
"""
from __future__ import annotations

import json
import os

import pytest
import requests

# ── Config ────────────────────────────────────────────────────────────────────

OR_KEY   = os.environ.get("OPENROUTER_API_KEY", "")
OR_MODEL = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")

needs_openrouter = pytest.mark.skipif(not OR_KEY, reason="OPENROUTER_API_KEY not set")

N_TRIALS       = 3
PASS_THRESHOLD = 2   # 2/3 ≈ 67% — allows 1 stochastic failure per eval

# ── Eval runner ───────────────────────────────────────────────────────────────


def eval_n_times(label: str, fn, n: int = N_TRIALS, threshold: int = PASS_THRESHOLD):
    """Run fn() n times, assert at least threshold passes."""
    passes, lines = 0, []
    for i in range(n):
        try:
            detail = fn()
            passes += 1
            lines.append(f"  PASS [{i+1}/{n}] {detail}")
        except AssertionError as e:
            lines.append(f"  FAIL [{i+1}/{n}] {e}")
    pct = passes / n
    print(f"\n{'='*60}")
    print(f"[{label}]  {passes}/{n} passed  ({pct:.0%})")
    for ln in lines:
        print(ln)
    assert passes >= threshold, (
        f"[{label}] passed {passes}/{n} ({pct:.0%}) — need {threshold}/{n}"
    )


def _or_call(prompt: str, temperature: float = 0.3) -> str:
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OR_KEY}", "Content-Type": "application/json"},
        json={
            "model": OR_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
        },
        timeout=90,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


# ── Fixtures ──────────────────────────────────────────────────────────────────

SAMPLE_PROFILE = {
    "experience": [
        {
            "title": "Account Executive",
            "company": "HubSpot",
            "start": "2022-03",
            "end": "2024-06",
            "description": "Closed $800K ARR mid-market SaaS. Used Salesforce daily.",
            "skills": ["Salesforce", "MEDDIC", "SaaS", "mid-market"],
        },
        {
            "title": "BDR",
            "company": "Acme Corp",
            "start": "2020-06",
            "end": "2022-02",
            "description": "Generated 200 SQLs per quarter via cold outreach.",
            "skills": ["outbound", "cold calling", "LinkedIn Sales Navigator"],
        },
    ],
    "projects": [],
    "structured_skills": [
        {"name": "Salesforce", "category": "crm", "evidence_level": "experience"},
        {"name": "MEDDIC", "category": "methodology", "evidence_level": "experience"},
        {"name": "LinkedIn Sales Navigator", "category": "general", "evidence_level": "experience"},
    ],
    "description": (
        "Looking for AE/BDR roles at SaaS companies. Comfortable with enterprise and mid-market. "
        "Strong outbound background, quota-carrying experience."
    ),
}

SAMPLE_JDS = [
    "Account Executive - SaaS company, MEDDIC required, 3+ years experience, Salesforce CRM",
    "BDR - Tech startup, cold calling, LinkedIn outreach, fast-paced environment",
    "Sales Manager - must have 5+ years leading teams of 10+, P&L responsibility",
    "Account Manager - HubSpot preferred, renewal experience, customer success background",
    "Enterprise AE - MEDDPICC required, $1M+ quota, Fortune 500 selling experience",
]


# ── Test: wiki contains candidate specifics ────────────────────────────────────

@needs_openrouter
def test_wiki_contains_candidate_specifics():
    """3 trials: given profile with HubSpot at Acme Corp, wiki must mention both."""

    exp_text = "\n".join(
        f"- {e['title']} at {e['company']} ({e['start']}–{e['end']}): {e['description']}"
        for e in SAMPLE_PROFILE["experience"]
    )

    def trial():
        prompt = (
            "Create a structured candidate wiki in markdown for an LLM to use as context.\n\n"
            f"Candidate info:\n{exp_text}\n\n"
            f"Projects: {SAMPLE_PROFILE['projects']}\n"
            f"Skills: {SAMPLE_PROFILE['structured_skills']}\n"
            f"Description: {SAMPLE_PROFILE['description']}\n\n"
            "Generate a wiki with exactly these 8 sections:\n"
            "# Candidate Wiki\n"
            "## Identity & Target Role\n"
            "## Quota & Revenue Performance\n"
            "## CRM & Tools\n"
            "## Work Experience (chronological)\n"
            "## Sales Skills & Methodologies\n"
            "## Client & Deal Types\n"
            "## Industry Knowledge\n"
            "## What This Candidate Is NOT\n\n"
            "Be specific. Include real numbers, real company names, real tools.\n"
            "Keep total length under 800 tokens."
        )
        wiki = _or_call(prompt)

        assert "HubSpot" in wiki, f"Wiki missing 'HubSpot'. Got:\n{wiki[:300]}"
        assert "Acme" in wiki or "Acme Corp" in wiki, f"Wiki missing 'Acme Corp'. Got:\n{wiki[:300]}"
        assert "## What This Candidate Is NOT" in wiki, "Wiki missing required section"
        return f"wiki length={len(wiki)}, mentions HubSpot and Acme"

    eval_n_times("wiki_contains_candidate_specifics", trial)


# ── Test: resume health suggestions are specific ──────────────────────────────

@needs_openrouter
def test_resume_health_suggestions_are_specific():
    """3 trials: suggestions must contain at least one specific tool name or number."""

    jd_sample = "\n---\n".join(SAMPLE_JDS)
    candidate_wiki = (
        "# Candidate Wiki\n"
        "## Identity & Target Role\nAE/BDR targeting SaaS roles.\n"
        "## CRM & Tools\nSalesforce daily user.\n"
        "## Work Experience\n- AE at HubSpot 2022-2024, $800K ARR\n- BDR at Acme Corp 2020-2022"
    )

    def trial():
        prompt = (
            "Analyze this candidate's profile against recent job postings and provide:\n"
            "1. A resume health score (0-100)\n"
            "2. 3-5 specific, actionable suggestions to improve their match rate\n"
            "3. Scores (0-100) for 4 dimensions: keyword_coverage, specificity, recency_signal, seniority_alignment\n\n"
            f"Recent job postings sample:\n{jd_sample}\n\n"
            f"Candidate profile:\n{candidate_wiki}\n\n"
            'Return JSON:\n{"score": 72, "suggestions": ["..."], '
            '"dimensions": {"keyword_coverage": 68, "specificity": 65, "recency_signal": 80, "seniority_alignment": 75}}'
        )
        raw = _or_call(prompt)

        # Strip code fences if present
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise AssertionError(f"Response is not valid JSON: {e}\nRaw: {raw[:300]}")

        suggestions = data.get("suggestions", [])
        assert suggestions, "No suggestions returned"

        # At least one suggestion must mention a specific tool name or number
        SPECIFIC_SIGNALS = [
            "MEDDIC", "MEDDPICC", "Salesforce", "HubSpot", "LinkedIn",
            "ARR", "quota", "$", "%", "year", "month",
        ]
        found_specific = any(
            any(signal.lower() in s.lower() for signal in SPECIFIC_SIGNALS)
            for s in suggestions
        )
        assert found_specific, (
            f"No specific tool/number in suggestions: {suggestions}"
        )

        assert "score" in data and isinstance(data["score"], (int, float))
        return f"score={data['score']}, {len(suggestions)} suggestions, first: {suggestions[0][:60]}"

    eval_n_times("resume_health_suggestions_are_specific", trial)

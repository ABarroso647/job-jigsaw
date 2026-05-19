"""
DeepEval-style LLM quality tests for Branch A (Boolean query generation).

Uses the eval_n_times() harness from test_integration_llm.py.
3 trials, 3/3 pass threshold (100% — query gen is deterministic enough).

Run:
    OPENROUTER_API_KEY=... pytest tests/eval/test_eval_scraper_a.py -v -s
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
import re

import pytest
import requests

# ── Config ────────────────────────────────────────────────────────────────────

OR_KEY   = os.environ.get("OPENROUTER_API_KEY", "")
OR_MODEL = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")

needs_openrouter = pytest.mark.skipif(not OR_KEY, reason="OPENROUTER_API_KEY not set")

N_TRIALS       = 3
PASS_THRESHOLD = 2   # 67% — allows 1 stochastic failure


def eval_n_times(label: str, fn, n: int = N_TRIALS, threshold: int = PASS_THRESHOLD):
    """Run fn() n times. fn() returns detail string on pass, raises AssertionError on fail."""
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


def _or_complete(prompt: str) -> str:
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OR_KEY}", "Content-Type": "application/json"},
        json={
            "model": OR_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


# ── Test profiles ─────────────────────────────────────────────────────────────

SALES_CANDIDATE = {
    "description": (
        "Results-driven sales professional with 3+ years in B2B SaaS. "
        "Experienced as Account Executive, BDR, and SDR. "
        "Strong track record closing mid-market deals, CRM proficiency, "
        "consultative selling. Looking for AE or senior BDR roles."
    ),
    "search": {
        "keywords": ["Account Executive", "BDR", "SDR", "Business Development"]
    },
}

BOOLEAN_QUERY_PROMPT = """\
Generate a LinkedIn/Indeed Boolean job search string for this candidate.
Candidate description: {description}
Current keywords: {keywords}
Rules: use OR between related titles, quote multi-word phrases, include seniority variants.
Return ONLY the search string, nothing else.
Example: "Account Executive" OR "AE" OR "Sales Executive" OR "BDR" OR "SDR"
"""

BOOLEAN_OPERATORS = re.compile(r'\b(OR|AND|NOT)\b')
ROLE_TERMS = re.compile(r'(Account Executive|AE|BDR|SDR|Sales|Business Development)', re.IGNORECASE)


# ── Eval tests ────────────────────────────────────────────────────────────────

@needs_openrouter
def test_boolean_query_contains_role_terms():
    """3 trials: generated query must contain 'Account Executive' or 'AE' and a Boolean operator."""

    def trial():
        prompt = BOOLEAN_QUERY_PROMPT.format(
            description=SALES_CANDIDATE["description"],
            keywords=", ".join(SALES_CANDIDATE["search"]["keywords"]),
        )
        query = _or_complete(prompt)

        has_role_term = bool(ROLE_TERMS.search(query))
        has_boolean_op = bool(BOOLEAN_OPERATORS.search(query))

        assert has_role_term, (
            f"Generated query missing role terms (AE/BDR/SDR/Account Executive). "
            f"Query: {query[:200]}"
        )
        assert has_boolean_op, (
            f"Generated query has no Boolean operators (OR/AND). Query: {query[:200]}"
        )
        return f"query={query[:100]}"

    eval_n_times("boolean_query_contains_role_terms", trial)

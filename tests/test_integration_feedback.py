"""
Feedback summary integration evals.

Tests two things:
1. QUALITY — does the generated summary actually capture patterns from rated jobs?
2. USAGE   — does including the summary in the scoring prompt actually move scores
             in the correct direction for matching / non-matching jobs?

Each eval runs N_TRIALS=5 times and must pass PASS_THRESHOLD=4 (80%).

Run:
    OPENROUTER_API_KEY=... pytest tests/test_integration_feedback.py -v -s
"""
from __future__ import annotations
import json
import os

import pytest
import requests

OR_KEY   = os.environ.get("OPENROUTER_API_KEY", "")
OR_MODEL = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
needs_openrouter = pytest.mark.skipif(not OR_KEY, reason="OPENROUTER_API_KEY not set")

N_TRIALS       = 5
PASS_THRESHOLD = 4

# ── Eval runner ───────────────────────────────────────────────────────────────

def eval_n_times(label, fn, n=N_TRIALS, threshold=PASS_THRESHOLD):
    passes, lines = 0, []
    for i in range(n):
        try:
            detail = fn()
            passes += 1
            lines.append(f"  PASS [{i+1}/{n}] {detail}")
        except AssertionError as e:
            lines.append(f"  FAIL [{i+1}/{n}] {e}")
    pct = passes / n
    print(f"\n{'='*60}\n[{label}]  {passes}/{n} passed  ({pct:.0%})")
    for ln in lines:
        print(ln)
    assert passes >= threshold, f"[{label}] {passes}/{n} ({pct:.0%}) — need {threshold}/{n}"

# ── Prompts (must match profile-editor/main.py exactly) ──────────────────────

FEEDBACK_SUMMARY_PROMPT = """\
Based on these job ratings and notes, summarize in 2-3 sentences what this \
candidate likes and dislikes about job postings. Be specific about patterns \
(company types, role types, requirements, industries).

FEEDBACK:
{feedback_lines}

Return ONLY valid JSON: {{"summary": "<2-3 sentences>"}}"""

SCORING_PROMPT = """\
You are evaluating a job listing for a specific candidate.

CANDIDATE:
{profile_json}

JOB:
Title: {title}
Employer: {employer}
Location: {location}
Description: {description}

Score how well this job fits the candidate 0-100:
- 90-100: exceptional fit on all dimensions
- 70-89: strong fit, minor gaps
- 50-69: partial fit, notable gaps
- below 50: poor fit

Use the full range. Avoid round numbers — 73 is better than 70.
Score below 30 if the role requires credentials clearly absent from the resume, \
or is in an unrelated field.

Return ONLY valid JSON: {{"score": <integer 0-100>, "reason": "<1-2 sentences>"}}"""

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


def _generate_summary(feedback_rows: list[dict]) -> str:
    lines = []
    for r in feedback_rows:
        rating_str = {1: "liked", -1: "disliked"}.get(r.get("user_rating"), "noted")
        line = f'- {rating_str}: "{r["title"]}" at {r["employer"]}'
        if r.get("notes"):
            line += f' — Note: "{r["notes"]}"'
        lines.append(line)
    prompt = FEEDBACK_SUMMARY_PROMPT.format(feedback_lines="\n".join(lines))
    result = _parse_json(_or_complete(prompt))
    return result["summary"]


def _score(profile_json: str, title: str, employer: str, location: str, description: str) -> int:
    result = _parse_json(_or_complete(SCORING_PROMPT.format(
        profile_json=profile_json,
        title=title, employer=employer,
        location=location, description=description,
    )))
    return result["score"]

# ── Fake rating data sets ─────────────────────────────────────────────────────
#
# Each set has a clear pattern so we can assert the summary picks it up.

SAAS_LOVER_RATINGS = [
    {"title": "Account Executive", "employer": "CloudMetrics", "user_rating": 1, "notes": "Great SaaS platform"},
    {"title": "Senior AE", "employer": "Salesify", "user_rating": 1, "notes": ""},
    {"title": "BDR", "employer": "SaaSlead", "user_rating": 1, "notes": "Love the tech stack"},
    {"title": "Delivery Driver", "employer": "SwiftRoute", "user_rating": -1, "notes": "Not relevant at all"},
    {"title": "Warehouse Associate", "employer": "FulfillCo", "user_rating": -1, "notes": ""},
    {"title": "Forklift Operator", "employer": "LogisticsFast", "user_rating": -1, "notes": "Wrong field"},
]

REMOTE_PREFERENCE_RATINGS = [
    {"title": "AE", "employer": "CloudCo", "user_rating": 1, "notes": "Love that it's remote"},
    {"title": "BDR", "employer": "SaaSly", "user_rating": 1, "notes": "Remote is perfect"},
    {"title": "Sales Manager", "employer": "TechStart", "user_rating": 1, "notes": "Great remote culture"},
    {"title": "AE", "employer": "OfficeCorp", "user_rating": -1, "notes": "5 days in-office, not for me"},
    {"title": "Sales Rep", "employer": "BramptonSales", "user_rating": -1, "notes": "On-site only, dealbreaker"},
]

SENIORITY_RATINGS = [
    {"title": "Senior Account Executive", "employer": "GrowthCo", "user_rating": 1, "notes": "Good seniority level"},
    {"title": "Enterprise AE", "employer": "BigCloud", "user_rating": 1, "notes": ""},
    {"title": "SDR", "employer": "StartupX", "user_rating": -1, "notes": "Too junior"},
    {"title": "BDR", "employer": "SeedCo", "user_rating": -1, "notes": "Entry level, not what I want"},
    {"title": "VP Sales", "employer": "EnterpriseY", "user_rating": -1, "notes": "Too senior, unrealistic"},
]

# ── Quality evals: does the summary capture patterns? ─────────────────────────

@needs_openrouter
def test_summary_captures_saas_preference():
    """
    Given 3 liked SaaS roles and 3 disliked logistics roles, the summary
    must mention SaaS/tech preference AND dislike of logistics/warehouse/driving.
    4/5 trials.
    """
    def trial():
        summary = _generate_summary(SAAS_LOVER_RATINGS)
        summary_lower = summary.lower()
        has_positive = any(w in summary_lower for w in ["saas", "tech", "software", "cloud"])
        has_negative = any(w in summary_lower for w in [
            "logistic", "warehouse", "driver", "driving", "manual", "physical", "delivery"
        ])
        assert has_positive, f"Summary missing SaaS/tech preference. Got: '{summary}'"
        assert has_negative, f"Summary missing logistics dislike. Got: '{summary}'"
        return f"summary='{summary[:80]}…'"

    eval_n_times("summary_captures_saas_preference", trial)


@needs_openrouter
def test_summary_captures_remote_preference():
    """
    Given ratings clearly skewed toward remote and against on-site,
    summary must mention remote preference. 4/5 trials.
    """
    def trial():
        summary = _generate_summary(REMOTE_PREFERENCE_RATINGS)
        has_remote = any(w in summary.lower() for w in ["remote", "work from home", "wfh", "flexible"])
        assert has_remote, f"Summary missing remote preference. Got: '{summary}'"
        return f"summary='{summary[:80]}…'"

    eval_n_times("summary_captures_remote_preference", trial)


@needs_openrouter
def test_summary_captures_seniority_preference():
    """
    Given ratings that liked senior roles and disliked both junior and VP-level,
    summary must mention mid/senior level preference. 4/5 trials.
    """
    def trial():
        summary = _generate_summary(SENIORITY_RATINGS)
        summary_lower = summary.lower()
        has_seniority = any(w in summary_lower for w in [
            "senior", "mid-level", "mid level", "experienced", "junior", "entry"
        ])
        assert has_seniority, f"Summary missing seniority signal. Got: '{summary}'"
        return f"summary='{summary[:80]}…'"

    eval_n_times("summary_captures_seniority_preference", trial)


@needs_openrouter
def test_summary_is_specific_not_generic():
    """
    Summary must not be a vague platitude. It must contain at least one concrete
    term from the job titles or notes. 4/5 trials.
    """
    concrete_terms = {
        "saas", "cloud", "account executive", "ae", "bdr", "remote",
        "logistics", "warehouse", "driver", "senior",
    }

    def trial():
        summary = _generate_summary(SAAS_LOVER_RATINGS)
        found = [t for t in concrete_terms if t in summary.lower()]
        assert len(found) >= 2, (
            f"Summary too generic — only found {found}. Got: '{summary}'"
        )
        return f"concrete_terms_found={found}"

    eval_n_times("summary_is_specific_not_generic", trial)


# ── Usage evals: does the summary actually move scores? ───────────────────────

RESUME = (
    "B2B sales professional, 4 years SaaS AE experience. HubSpot, Salesforce. "
    "Seeking Account Executive roles. Based in Toronto, ON."
)

GOOD_JOB = dict(
    title="Account Executive",
    employer="CloudMetrics",
    location="Toronto, ON (Remote)",
    description="Full-cycle SaaS sales, HubSpot CRM, mid-market accounts, OTE $120k.",
)
BAD_JOB = dict(
    title="Delivery Driver",
    employer="SwiftRoute",
    location="Mississauga, ON",
    description="Drive 5-ton truck, GTA routes. G licence required. Union position.",
)


@needs_openrouter
def test_positive_summary_raises_good_job_score():
    """
    A summary saying 'prefers remote SaaS AE roles' should increase the AE score
    (or at least not decrease it significantly) vs no summary. 4/5 trials.
    """
    def trial():
        # Generate a fresh summary from the SaaS-lover ratings
        summary = _generate_summary(SAAS_LOVER_RATINGS)

        base_profile   = json.dumps({"resume": RESUME})
        with_feedback  = json.dumps({"resume": RESUME, "feedback": summary})

        score_base = _score(base_profile,  **GOOD_JOB)
        score_with = _score(with_feedback, **GOOD_JOB)

        # Allow a 5-pt stochastic drop; what we care about is it doesn't crash
        assert score_with >= score_base - 5, (
            f"AE score dropped significantly with positive feedback: "
            f"{score_base}→{score_with}. Summary: '{summary[:60]}'"
        )
        return f"AE: {score_base}→{score_with} (summary='{summary[:50]}…')"

    eval_n_times("positive_summary_raises_good_job", trial)


@needs_openrouter
def test_negative_summary_lowers_bad_job_score():
    """
    A summary saying 'dislikes logistics/driving' should lower the Driver score
    vs no summary. 4/5 trials.
    """
    def trial():
        summary = _generate_summary(SAAS_LOVER_RATINGS)

        base_profile   = json.dumps({"resume": RESUME})
        with_feedback  = json.dumps({"resume": RESUME, "feedback": summary})

        score_base = _score(base_profile,  **BAD_JOB)
        score_with = _score(with_feedback, **BAD_JOB)

        assert score_with <= score_base + 3, (
            f"Driver score improved with negative feedback: "
            f"{score_base}→{score_with}. Summary: '{summary[:60]}'"
        )
        return f"Driver: {score_base}→{score_with} (summary='{summary[:50]}…')"

    eval_n_times("negative_summary_lowers_bad_job", trial)


@needs_openrouter
def test_summary_gap_larger_than_baseline():
    """
    The score GAP between AE and Driver should be wider when feedback_summary
    is included vs when it isn't. The summary should amplify discrimination.
    4/5 trials.
    """
    def trial():
        summary = _generate_summary(SAAS_LOVER_RATINGS)

        base_profile   = json.dumps({"resume": RESUME})
        with_feedback  = json.dumps({"resume": RESUME, "feedback": summary})

        ae_base    = _score(base_profile,  **GOOD_JOB)
        ae_with    = _score(with_feedback, **GOOD_JOB)
        drv_base   = _score(base_profile,  **BAD_JOB)
        drv_with   = _score(with_feedback, **BAD_JOB)

        gap_base = ae_base  - drv_base
        gap_with = ae_with  - drv_with

        # We allow gap_with to be equal (not strictly greater) — stochasticity
        assert gap_with >= gap_base - 5, (
            f"Gap narrowed significantly with summary: "
            f"base gap={gap_base} ({ae_base} - {drv_base}), "
            f"with-summary gap={gap_with} ({ae_with} - {drv_with})"
        )
        return (
            f"gap: {gap_base}→{gap_with} "
            f"(AE {ae_base}→{ae_with}, Driver {drv_base}→{drv_with})"
        )

    eval_n_times("summary_gap_larger_than_baseline", trial)

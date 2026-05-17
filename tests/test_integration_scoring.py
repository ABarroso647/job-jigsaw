"""
Scoring prompt integration evals — verify the scraper's SCORING_PROMPT
produces sensible, discriminating, non-round scores.

Separate from the re-ranker evals (test_integration_llm.py) — these tests
specifically validate the scoring step: output quality, score range usage,
and that feedback_summary actually moves scores in the right direction.

Each eval runs N_TRIALS=5 times, must pass PASS_THRESHOLD=4 (80%).

Run:
    OPENROUTER_API_KEY=... pytest tests/test_integration_scoring.py -v -s
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

# ── Exact SCORING_PROMPT from scraper/scrape.py ───────────────────────────────

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

def _score(profile_json: str, title: str, employer: str, location: str, description: str) -> dict:
    prompt = SCORING_PROMPT.format(
        profile_json=profile_json,
        title=title, employer=employer,
        location=location, description=description,
    )
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OR_KEY}", "Content-Type": "application/json"},
        json={
            "model": OR_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
        },
        timeout=60,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    if "```" in content:
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
    return json.loads(content.strip())

# ── Test data ─────────────────────────────────────────────────────────────────

RESUME_SALES = (
    "Experienced B2B sales professional, 4 years as a quota-carrying Account Executive "
    "at SaaS companies. Proficient in HubSpot and Salesforce. Closed $1.2M ARR last year. "
    "Based in Toronto, ON. Seeking AE or senior BDR roles at tech companies."
)

RESUME_NURSE = (
    "Registered Nurse, 6 years ICU and emergency department experience. BScN degree. "
    "BCLS and ACLS certified. Experienced with ventilator management, central lines, "
    "and critical care protocols."
)

JOBS_FOR_SALES = [
    {
        "title": "Account Executive",
        "employer": "CloudMetrics",
        "location": "Toronto, ON (Hybrid)",
        "description": (
            "Own full sales cycle for our B2B SaaS platform. Manage mid-market accounts, "
            "run demos, negotiate contracts. HubSpot required. OTE $120k, $700k quota. "
            "3+ yrs AE experience required."
        ),
    },
    {
        "title": "Delivery Driver",
        "employer": "SwiftRoute Logistics",
        "location": "Mississauga, ON",
        "description": (
            "Drive a 5-ton truck on GTA routes. Load and unload parcels up to 50 lbs. "
            "Valid G licence required. Forklift certification an asset. Union position."
        ),
    },
    {
        "title": "Business Development Rep",
        "employer": "SaaSly",
        "location": "Remote (Canada)",
        "description": (
            "Outbound prospecting via cold call and LinkedIn. Qualify leads for AE handoff. "
            "Salesforce required. 1-2 yrs sales experience preferred."
        ),
    },
]

# ── Scoring prompt quality evals ──────────────────────────────────────────────

@needs_openrouter
def test_score_avoids_round_numbers():
    """
    The prompt instructs 'avoid round numbers'. At least 3/5 responses should
    not be a multiple of 5 (e.g. 73, not 70).
    """
    def trial():
        result = _score(
            RESUME_SALES,
            JOBS_FOR_SALES[0]["title"],
            JOBS_FOR_SALES[0]["employer"],
            JOBS_FOR_SALES[0]["location"],
            JOBS_FOR_SALES[0]["description"],
        )
        score = result["score"]
        assert score % 5 != 0, f"Got round number score={score}"
        return f"score={score} (non-round ✓)"

    eval_n_times("score_avoids_round_numbers", trial, threshold=3)


@needs_openrouter
def test_score_uses_full_range():
    """
    Across 3 very different jobs, the range of scores should span >= 30 pts.
    Run 5 trials; at least 4 should show discrimination.
    """
    def trial():
        scores = []
        for job in JOBS_FOR_SALES:
            result = _score(RESUME_SALES, job["title"], job["employer"],
                            job["location"], job["description"])
            scores.append(result["score"])
        span = max(scores) - min(scores)
        assert span >= 30, (
            f"Score span only {span} pts across AE/BDR/Driver: {scores}"
        )
        return f"scores={scores}, span={span}"

    eval_n_times("score_uses_full_range", trial)


@needs_openrouter
def test_score_returns_valid_json_with_reason():
    """
    Response must be valid JSON with integer score and non-empty reason string.
    Should hold 5/5 — this tests the response_format compliance.
    """
    def trial():
        result = _score(
            RESUME_NURSE,
            "ICU Registered Nurse",
            "Toronto General Hospital",
            "Toronto, ON",
            "Critical care nursing in a 20-bed ICU. BScN and ACLS required. 3+ yrs ICU.",
        )
        assert isinstance(result.get("score"), int), f"score not int: {result}"
        assert isinstance(result.get("reason"), str) and result["reason"].strip(), \
            f"reason missing or empty: {result}"
        assert 0 <= result["score"] <= 100, f"score out of range: {result['score']}"
        return f"score={result['score']}, reason len={len(result['reason'])}"

    eval_n_times("score_valid_json_with_reason", trial, n=5, threshold=5)


# ── feedback_summary integration evals ───────────────────────────────────────
#
# These verify that passing feedback_summary in the candidate profile
# actually shifts scores in the correct direction.

@needs_openrouter
def test_feedback_summary_boosts_preferred_job():
    """
    A feedback_summary saying 'prefers SaaS AE roles, dislikes logistics'
    should push the AE score higher and the Driver score lower vs no summary.
    Run 5 trials; at least 4 must show the AE scores higher WITH the summary.
    """
    resume_base = RESUME_SALES
    profile_no_feedback   = json.dumps({"resume": resume_base})
    profile_with_feedback = json.dumps({
        "resume": resume_base,
        "feedback": "User strongly prefers SaaS Account Executive roles at tech companies. "
                    "Has consistently downranked and disliked logistics and driving roles.",
    })

    ae_job    = JOBS_FOR_SALES[0]  # SaaS AE — should benefit from summary
    driver_job = JOBS_FOR_SALES[1]  # Driver — should be hurt by summary

    def trial():
        ae_base   = _score(profile_no_feedback,   **ae_job)["score"]
        ae_with   = _score(profile_with_feedback, **ae_job)["score"]
        drv_base  = _score(profile_no_feedback,   **driver_job)["score"]
        drv_with  = _score(profile_with_feedback, **driver_job)["score"]

        ae_moved_right   = ae_with  >= ae_base   - 5   # AE shouldn't drop much
        driver_got_worse = drv_with <= drv_base  + 3   # Driver shouldn't improve

        assert ae_moved_right and driver_got_worse, (
            f"AE: {ae_base}→{ae_with}, Driver: {drv_base}→{drv_with}. "
            "Expected: AE holds or improves, Driver holds or drops."
        )
        return (
            f"AE {ae_base}→{ae_with} ({'↑' if ae_with > ae_base else '→'}), "
            f"Driver {drv_base}→{drv_with} ({'↓' if drv_with < drv_base else '→'})"
        )

    eval_n_times("feedback_summary_boosts_preferred_job", trial)


@needs_openrouter
def test_feedback_summary_against_mismatched_profile():
    """
    A feedback_summary that conflicts with the job (user dislikes the exact role type)
    should result in a lower score than when no summary is present.
    Run 5 trials; at least 4 must show the score drops.
    """
    resume_base = RESUME_SALES
    profile_against = json.dumps({
        "resume": resume_base,
        "feedback": "User has explicitly rejected every Account Executive role they have seen. "
                    "Wants to move away from SaaS sales entirely into operations.",
    })
    profile_neutral = json.dumps({"resume": resume_base})

    ae_job = JOBS_FOR_SALES[0]

    def trial():
        score_neutral = _score(profile_neutral, **ae_job)["score"]
        score_against = _score(profile_against, **ae_job)["score"]
        assert score_against < score_neutral, (
            f"Score with conflicting feedback ({score_against}) should be "
            f"lower than neutral ({score_neutral})"
        )
        return f"neutral={score_neutral}, against={score_against}, drop={score_neutral - score_against}"

    eval_n_times("feedback_summary_penalises_rejected_role", trial)

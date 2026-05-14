"""
LLM/Reranker eval tests — generic, parameterized, trial-based.

Inspired by deepeval's approach: each scenario runs N_TRIALS=5 times and
must pass PASS_THRESHOLD=4 (80%). This surfaces stochastic model failures
that a single-run test hides.

Tests are domain-agnostic — they use 4 completely different professional
profiles so they prove model capability, not app-specific knowledge.

Run:
    OPENROUTER_API_KEY=... pytest tests/test_integration_llm.py -v -s
    OPENROUTER_API_KEY=... JINA_API_KEY=... pytest tests/test_integration_llm.py -v -s

Cost: ~$0.01 per full run at DeepSeek V4 Flash prices.
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

N_TRIALS       = 5
PASS_THRESHOLD = 4   # 80% — allows 1 stochastic failure per eval

# Score thresholds — conservative enough for any decent model
GOOD_FIT_MIN   = 60   # clearly relevant job must score at least this
BAD_FIT_MAX    = 40   # clearly irrelevant job must score at most this
MIN_SCORE_GAP  = 20   # relevant must outscore irrelevant by at least this

# ── Eval runner ───────────────────────────────────────────────────────────────

def eval_n_times(label: str, fn, n: int = N_TRIALS, threshold: int = PASS_THRESHOLD):
    """
    Run fn() n times. fn() returns a detail string on pass, raises AssertionError on fail.
    Asserts at least `threshold` passes and prints a per-trial report.
    """
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

# ── Scoring prompt ────────────────────────────────────────────────────────────

SCORING_PROMPT = """\
You are evaluating a job listing for a specific candidate.

CANDIDATE PROFILE:
{resume}

JOB:
Title: {title}
Description: {description}

Score how well this job fits the candidate 0-100:
- 90-100: exceptional fit on all dimensions
- 70-89: strong fit, minor gaps
- 50-69: partial fit, notable gaps
- 30-49: poor fit, major mismatches
- 0-29: irrelevant — unrelated field or missing core credentials

Use the full range. Avoid round numbers.

Return ONLY valid JSON: {{"score": <integer 0-100>, "reason": "<1-2 sentences>"}}"""

RERANK_PROMPT = """\
Rank these job postings by relevance to this candidate.
Return ONLY valid JSON with no explanation.

CANDIDATE:
{resume}

JOBS:
{job_lines}

Return: {{"ranked": [indices best to worst], "scores": [relevance 0.0-1.0 per ranked job]}}"""

# ── Test cases — 4 unrelated professional domains ─────────────────────────────
#
# Each case has:
#   resume      — clear professional identity
#   strong      — obviously relevant (same role, matching credentials)
#   weak        — obviously irrelevant (completely different field)
#   mid         — plausible but imperfect match (for re-ranking variation)
#   extra_weak  — second obviously irrelevant job (for re-ranking bottom-2 tests)
#
# Contrast between strong and weak is intentionally extreme so any competent
# model should get it right. The mid jobs add realistic noise.

CASES = [
    {
        "id": "nursing",
        "resume": (
            "Registered Nurse, 6 years ICU and emergency department experience. "
            "BScN degree. BCLS and ACLS certified. Skilled in ventilator management, "
            "central line care, critical care protocols, and hemodynamic monitoring."
        ),
        "strong": {
            "title": "ICU Registered Nurse",
            "description": (
                "Provide critical care nursing to ventilated patients in a 20-bed ICU. "
                "Manage vasopressors, central lines, and continuous renal replacement therapy. "
                "BScN required. BCLS required, ACLS preferred. 3+ years ICU experience required."
            ),
        },
        "mid": {
            "title": "Medical Office Administrator",
            "description": (
                "Manage patient scheduling, billing, and EMR records for a busy family medicine clinic. "
                "Healthcare background an asset but not required. Strong organizational skills needed."
            ),
        },
        "weak": {
            "title": "Forklift Operator",
            "description": (
                "Operate electric forklifts in a distribution warehouse. Move pallets, scan inventory, "
                "maintain logs. Forklift licence required. No other experience necessary."
            ),
        },
        "extra_weak": {
            "title": "Landscaper",
            "description": (
                "Mow lawns, plant shrubs, lay sod, and maintain gardens for residential clients. "
                "Valid driver's licence required. Physically demanding outdoor work."
            ),
        },
    },
    {
        "id": "software_engineering",
        "resume": (
            "Senior software engineer, 8 years Python and distributed systems. "
            "Built ML training pipelines at scale using PyTorch and Apache Spark. "
            "Deep expertise in Kubernetes, microservices, and cloud infrastructure (AWS). "
            "BSc Computer Science."
        ),
        "strong": {
            "title": "Senior ML Infrastructure Engineer",
            "description": (
                "Design and operate ML training and serving infrastructure. "
                "Python, PyTorch, Kubernetes, and Spark required. AWS experience preferred. "
                "5+ years distributed systems experience. BSc in CS or equivalent."
            ),
        },
        "mid": {
            "title": "IT Project Manager",
            "description": (
                "Coordinate software delivery across engineering teams. "
                "Maintain roadmaps, run sprints, manage stakeholder expectations. "
                "PMP preferred. Technical background a strong asset. 5+ years project management."
            ),
        },
        "weak": {
            "title": "Licensed Plumber",
            "description": (
                "Install and repair plumbing systems in residential and commercial buildings. "
                "Red Seal certification or 306A licence required. "
                "3+ years journeyman experience. Must have own tools."
            ),
        },
        "extra_weak": {
            "title": "Pastry Chef",
            "description": (
                "Produce baked goods, desserts, and breads for a high-volume hotel kitchen. "
                "Red Seal in Baking and Pastry Arts preferred. 5am start. Weekend availability required."
            ),
        },
    },
    {
        "id": "culinary",
        "resume": (
            "Executive Chef with 14 years fine dining experience. "
            "Led kitchens of 25+ staff at two Michelin-recognized restaurants. "
            "Deep expertise in French classical technique, seasonal tasting menu development, "
            "and food cost management. Certified through George Brown culinary program."
        ),
        "strong": {
            "title": "Executive Chef — Fine Dining",
            "description": (
                "Lead the culinary program at an upscale 80-seat restaurant. "
                "Design seasonal tasting menus, mentor junior chefs, manage 20% food cost target. "
                "Formal culinary training required. 8+ years kitchen leadership experience."
            ),
        },
        "mid": {
            "title": "Food and Beverage Manager",
            "description": (
                "Oversee restaurant operations including front-of-house and back-of-house coordination. "
                "P&L accountability, staff scheduling, vendor negotiations. "
                "Hospitality management degree or equivalent experience. Culinary background an asset."
            ),
        },
        "weak": {
            "title": "Tax Accountant",
            "description": (
                "Prepare corporate and personal tax returns, T2 filings, and HST reconciliations. "
                "CPA designation required. Public accounting experience preferred. "
                "3+ years in a professional services firm."
            ),
        },
        "extra_weak": {
            "title": "Civil Engineering Technician",
            "description": (
                "Assist in site inspections, soil testing, and preparation of technical drawings. "
                "Diploma in Civil Engineering Technology required. AutoCAD experience an asset."
            ),
        },
    },
    {
        "id": "civil_engineering",
        "resume": (
            "Professional Engineer (P.Eng) specializing in structural bridge design, 10 years experience. "
            "Led design on 12 highway bridge replacement projects across Ontario. "
            "Proficient in AutoCAD, STAAD Pro, MTO bridge design standards, and load rating analysis."
        ),
        "strong": {
            "title": "Structural Bridge Designer",
            "description": (
                "Design and rehabilitate highway bridge superstructures and substructures. "
                "Perform load analysis, prepare stamped design drawings, liaise with MTO. "
                "P.Eng required. STAAD Pro and AutoCAD required. 7+ years bridge design experience."
            ),
        },
        "mid": {
            "title": "Construction Project Manager",
            "description": (
                "Manage construction of commercial and infrastructure projects from tender to closeout. "
                "Coordinate trades, manage RFIs and submittals, track schedule and budget. "
                "P.Eng or PMP preferred. 5+ years construction management experience."
            ),
        },
        "weak": {
            "title": "Registered Massage Therapist",
            "description": (
                "Provide therapeutic massage to clients in a clinical setting. "
                "RMT registration with CMTO required. Experience with deep tissue and sports therapy preferred. "
                "Flexible hours including evenings and weekends."
            ),
        },
        "extra_weak": {
            "title": "Hair Stylist",
            "description": (
                "Cut, colour, and style hair in a busy downtown salon. "
                "Cosmetology licence required. Experience with balayage and highlights preferred. "
                "Commission-based, flexible schedule."
            ),
        },
    },
]

CASE_IDS = [c["id"] for c in CASES]

# ── Scoring evals ─────────────────────────────────────────────────────────────

@needs_openrouter
@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_score_strong_fit_above_threshold(case):
    """Clearly relevant job must score >= 60 for a matched candidate profile, 4/5 trials."""
    def trial():
        result = _parse_json(_or_complete(SCORING_PROMPT.format(
            resume=case["resume"], **case["strong"]
        )))
        score = result["score"]
        assert score >= GOOD_FIT_MIN, (
            f"score={score} < {GOOD_FIT_MIN}. reason: {result['reason']}"
        )
        return f"score={score}"

    eval_n_times(f"score_strong_fit[{case['id']}]", trial)


@needs_openrouter
@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_score_weak_fit_below_threshold(case):
    """Completely unrelated job must score <= 40 for a matched candidate profile, 4/5 trials."""
    def trial():
        result = _parse_json(_or_complete(SCORING_PROMPT.format(
            resume=case["resume"], **case["weak"]
        )))
        score = result["score"]
        assert score <= BAD_FIT_MAX, (
            f"score={score} > {BAD_FIT_MAX}. reason: {result['reason']}"
        )
        return f"score={score}"

    eval_n_times(f"score_weak_fit[{case['id']}]", trial)


@needs_openrouter
@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_score_gap_strong_vs_weak(case):
    """Strong job must outscore clearly unrelated job by >= 20 pts, 4/5 trials."""
    def trial():
        strong = _parse_json(_or_complete(SCORING_PROMPT.format(
            resume=case["resume"], **case["strong"]
        )))
        weak = _parse_json(_or_complete(SCORING_PROMPT.format(
            resume=case["resume"], **case["weak"]
        )))
        gap = strong["score"] - weak["score"]
        assert gap >= MIN_SCORE_GAP, (
            f"gap={gap} < {MIN_SCORE_GAP}. "
            f"strong={strong['score']}, weak={weak['score']}"
        )
        return f"strong={strong['score']}, weak={weak['score']}, gap={gap}"

    eval_n_times(f"score_gap[{case['id']}]", trial)


@needs_openrouter
@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_score_strong_beats_mid(case):
    """Clearly relevant job must outscore the partial-match job, 4/5 trials."""
    def trial():
        strong = _parse_json(_or_complete(SCORING_PROMPT.format(
            resume=case["resume"], **case["strong"]
        )))
        mid = _parse_json(_or_complete(SCORING_PROMPT.format(
            resume=case["resume"], **case["mid"]
        )))
        assert strong["score"] > mid["score"], (
            f"strong ({strong['score']}) did not beat mid ({mid['score']})"
        )
        return f"strong={strong['score']}, mid={mid['score']}"

    eval_n_times(f"score_strong_beats_mid[{case['id']}]", trial)


# ── LLM re-ranking evals ──────────────────────────────────────────────────────

@needs_openrouter
@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_llm_rerank_strong_is_top2(case):
    """Strong-fit job must be in top 2 of 4 (strong, mid, weak, extra_weak), 4/5 trials."""
    jobs = [case["strong"], case["mid"], case["weak"], case["extra_weak"]]
    titles = [j["title"] for j in jobs]
    job_lines = "\n".join(
        f"{i}. {j['title']}: {j['description'][:200]}" for i, j in enumerate(jobs)
    )

    def trial():
        result = _parse_json(_or_complete(
            RERANK_PROMPT.format(resume=case["resume"], job_lines=job_lines),
            temperature=0.1,
        ))
        ranked = result["ranked"]
        pos = ranked.index(0)  # index 0 = strong job
        assert pos <= 1, (
            f"Strong job '{titles[0]}' ranked {pos + 1}/4. "
            f"Order: {[titles[i] for i in ranked]}"
        )
        return f"strong ranked {pos + 1}/4, order=[{', '.join(titles[i] for i in ranked)}]"

    eval_n_times(f"llm_rerank_strong_top2[{case['id']}]", trial)


@needs_openrouter
@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_llm_rerank_weak_jobs_in_bottom2(case):
    """Both unrelated jobs must be in bottom 2 of 4 (strong, mid, weak, extra_weak), 4/5 trials."""
    jobs = [case["strong"], case["mid"], case["weak"], case["extra_weak"]]
    titles = [j["title"] for j in jobs]
    job_lines = "\n".join(
        f"{i}. {j['title']}: {j['description'][:200]}" for i, j in enumerate(jobs)
    )
    bad_indices = {2, 3}

    def trial():
        result = _parse_json(_or_complete(
            RERANK_PROMPT.format(resume=case["resume"], job_lines=job_lines),
            temperature=0.1,
        ))
        ranked = result["ranked"]
        bottom2 = set(ranked[2:])
        missing = bad_indices - bottom2
        assert not missing, (
            f"Irrelevant jobs not in bottom 2: {[titles[i] for i in missing]}. "
            f"Order: {[titles[i] for i in ranked]}"
        )
        return f"bottom2=[{', '.join(titles[i] for i in ranked[2:])}]"

    eval_n_times(f"llm_rerank_weak_bottom2[{case['id']}]", trial)


# ── Jina re-ranking evals ─────────────────────────────────────────────────────

@needs_jina
@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_jina_rerank_strong_is_top2(case):
    """Jina: strong-fit job must be in top 2 of 4, 4/5 trials."""
    jobs = [case["strong"], case["mid"], case["weak"], case["extra_weak"]]
    titles = [j["title"] for j in jobs]
    documents = [f"{j['title']}\n{j['description']}" for j in jobs]

    def trial():
        results = _jina_rerank(case["resume"], documents)
        ranked = [r["index"] for r in results]
        pos = ranked.index(0)
        scores_str = ", ".join(f"{titles[r['index']]}={r['relevance_score']:.3f}" for r in results)
        assert pos <= 1, (
            f"Strong job '{titles[0]}' ranked {pos + 1}/4. "
            f"Scores: {scores_str}"
        )
        return f"strong rank={pos + 1}/4 | {scores_str}"

    eval_n_times(f"jina_rerank_strong_top2[{case['id']}]", trial)


@needs_jina
@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_jina_rerank_weak_jobs_in_bottom2(case):
    """Jina: both unrelated jobs must be in bottom 2 of 4, 4/5 trials."""
    jobs = [case["strong"], case["mid"], case["weak"], case["extra_weak"]]
    titles = [j["title"] for j in jobs]
    documents = [f"{j['title']}\n{j['description']}" for j in jobs]
    bad_indices = {2, 3}

    def trial():
        results = _jina_rerank(case["resume"], documents)
        ranked = [r["index"] for r in results]
        bottom2 = set(ranked[2:])
        missing = bad_indices - bottom2
        scores_str = ", ".join(f"{titles[r['index']]}={r['relevance_score']:.3f}" for r in results)
        assert not missing, (
            f"Irrelevant jobs not in bottom 2: {[titles[i] for i in missing]}. "
            f"Scores: {scores_str}"
        )
        return f"bottom2=[{', '.join(titles[i] for i in ranked[2:])}] | {scores_str}"

    eval_n_times(f"jina_rerank_weak_bottom2[{case['id']}]", trial)

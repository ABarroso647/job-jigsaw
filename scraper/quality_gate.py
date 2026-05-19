"""Pre-score quality gate for Job Jigsaw scraper.

Three-layer gate:
  Layer 0 — structural (missing URL / title)
  Layer 1 — hard content rejects (commission-only, MLM, wrong field, thin description)
  Layer 2 — soft penalties (accumulated score; reject when score < -20)
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

HARD_REJECT_PHRASES = [
    "commission only",
    "commission-only",
    "100% commission",
    "no base salary",
    "unpaid",
    "equity only",
    "no guaranteed pay",
    "mlm",
    "multi-level marketing",
    "network marketing",
    "amway",
    "herbalife",
    "primerica",
    "world financial group",
    "door to door",
    "door-to-door",
]

WRONG_FIELD_TITLES = [
    "delivery driver",
    "warehouse",
    "forklift",
    "cashier",
    "cook",
    "cleaner",
    "security guard",
    "truck driver",
]

SOFT_PENALTIES: dict[str, float] = {
    "1099": -10,
    "independent contractor": -8,
    "unlimited earning potential": -12,
    "be your own boss": -15,
    "work from home guaranteed": -8,
}


def evaluate_gate(job: dict) -> tuple[bool, str, float]:
    """Return (passes, reason, gate_score). passes=False means skip this job."""
    title = (job.get("title") or "").lower()
    description = (job.get("description") or "")
    desc_lower = description.lower()

    # Layer 0: structural
    if not job.get("job_url"):
        return False, "missing job_url", -100.0
    if not job.get("title"):
        return False, "missing title", -100.0
    if len(description) < 50:
        return False, "description too short", -100.0

    # Layer 1: hard reject
    for phrase in HARD_REJECT_PHRASES:
        if phrase in desc_lower or phrase in title:
            return False, f"hard reject: {phrase}", -100.0
    for term in WRONG_FIELD_TITLES:
        if term in title:
            return False, f"wrong field: {term}", -100.0

    # Layer 2: soft penalties
    score = 0.0
    for phrase, penalty in SOFT_PENALTIES.items():
        if phrase in desc_lower:
            score += penalty

    passes = score >= -20
    reason = "passed" if passes else f"soft penalties: {score}"
    return passes, reason, score

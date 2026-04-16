"""Shared helpers for AI insights proposals, history, and fuzzy intent parsing."""

import json
from datetime import datetime, timezone
from pathlib import Path

PENDING_PROPOSAL = Path("/data/pending_proposal.json")
INSIGHTS_HISTORY = Path("/data/insights_history.json")


# ── Pending proposal ──────────────────────────────────────────────────────────

def load_proposal() -> dict | None:
    """Return the active pending proposal, or None if none exists."""
    if not PENDING_PROPOSAL.exists():
        return None
    try:
        p = json.loads(PENDING_PROPOSAL.read_text())
        if p.get("status") == "pending":
            return p
    except Exception:
        pass
    return None


def save_proposal(proposal: dict) -> None:
    PENDING_PROPOSAL.write_text(json.dumps(proposal, indent=2))


def clear_proposal() -> None:
    if PENDING_PROPOSAL.exists():
        PENDING_PROPOSAL.unlink()


# ── History ───────────────────────────────────────────────────────────────────

def load_history() -> list[dict]:
    if not INSIGHTS_HISTORY.exists():
        return []
    try:
        return json.loads(INSIGHTS_HISTORY.read_text())
    except Exception:
        return []


def append_history(entry: dict) -> None:
    history = load_history()
    history.insert(0, entry)  # newest first
    INSIGHTS_HISTORY.write_text(json.dumps(history, indent=2))


# ── Apply / revert diff ───────────────────────────────────────────────────────

def apply_diff(profile: dict, proposal: dict) -> dict:
    """Apply proposal's boost/penalize/terms additions to profile. Returns change counts."""
    if not profile.get("scoring"):
        profile["scoring"] = {}

    existing_boost    = {k["keyword"].lower() for k in profile["scoring"].get("boost", [])}
    existing_penalize = {k["keyword"].lower() for k in profile["scoring"].get("penalize", [])}
    existing_terms    = {t.lower() for t in profile.get("search", {}).get("terms", [])}

    added_boost, added_penalize, added_terms = [], [], []

    for item in proposal.get("boost_add", []):
        if item.get("keyword", "").lower() not in existing_boost:
            profile["scoring"].setdefault("boost", []).append(item)
            added_boost.append(item)

    for item in proposal.get("penalize_add", []):
        if item.get("keyword", "").lower() not in existing_penalize:
            profile["scoring"].setdefault("penalize", []).append(item)
            added_penalize.append(item)

    for term in proposal.get("terms_add", []):
        if term.lower() not in existing_terms:
            profile.setdefault("search", {}).setdefault("terms", []).append(term)
            added_terms.append(term)

    return {"boost": added_boost, "penalize": added_penalize, "terms": added_terms}


def revert_entry(profile: dict, entry: dict) -> int:
    """Remove items added by a history entry from profile. Returns count of items removed."""
    removed = 0

    boost_remove = {k["keyword"].lower() for k in entry.get("boost_added", [])}
    if boost_remove and profile.get("scoring", {}).get("boost"):
        before = len(profile["scoring"]["boost"])
        profile["scoring"]["boost"] = [
            k for k in profile["scoring"]["boost"]
            if k["keyword"].lower() not in boost_remove
        ]
        removed += before - len(profile["scoring"]["boost"])

    penalize_remove = {k["keyword"].lower() for k in entry.get("penalize_added", [])}
    if penalize_remove and profile.get("scoring", {}).get("penalize"):
        before = len(profile["scoring"]["penalize"])
        profile["scoring"]["penalize"] = [
            k for k in profile["scoring"]["penalize"]
            if k["keyword"].lower() not in penalize_remove
        ]
        removed += before - len(profile["scoring"]["penalize"])

    terms_remove = {t.lower() for t in entry.get("terms_added", [])}
    if terms_remove and profile.get("search", {}).get("terms"):
        before = len(profile["search"]["terms"])
        profile["search"]["terms"] = [
            t for t in profile["search"]["terms"]
            if t.lower() not in terms_remove
        ]
        removed += before - len(profile["search"]["terms"])

    return removed


# ── Fuzzy intent parsing ──────────────────────────────────────────────────────

_APPROVE_WORDS = {"approve", "yes", "ok", "okay", "yep", "yup", "sure", "go", "apply", "accepted", "accept", "agreed", "agree", "confirmed", "confirm", "lgtm", "good", "great", "perfect", "done"}
_REJECT_WORDS  = {"reject", "no", "nope", "nah", "cancel", "stop", "denied", "deny", "decline", "skip", "abort", "discard", "ignore", "nevermind", "never"}


def _levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(prev[j] + (ca != cb), curr[-1] + 1, prev[j + 1] + 1))
        prev = curr
    return prev[-1]


def parse_intent(text: str) -> str:
    """Return 'approve', 'reject', or 'freeform'."""
    # Take the first non-empty word, lowercased, stripped of punctuation
    first = text.strip().split()[0].lower().strip(".,!?;:'\"") if text.strip() else ""

    if first in _APPROVE_WORDS:
        return "approve"
    if first in _REJECT_WORDS:
        return "reject"

    # Fuzzy match — allow edit distance ≤ 2 for short words, ≤ 3 for longer
    for word in _APPROVE_WORDS:
        threshold = 2 if len(word) <= 5 else 3
        if _levenshtein(first, word) <= threshold:
            return "approve"
    for word in _REJECT_WORDS:
        threshold = 2 if len(word) <= 5 else 3
        if _levenshtein(first, word) <= threshold:
            return "reject"

    return "freeform"


def build_history_entry(proposal: dict, applied: dict) -> dict:
    return {
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "summary": proposal.get("summary", ""),
        "boost_added":    applied["boost"],
        "penalize_added": applied["penalize"],
        "terms_added":    applied["terms"],
        "reverted": False,
    }

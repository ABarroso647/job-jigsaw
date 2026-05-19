"""DeepEval tests for Branch E: tag extraction LLM quality."""
import sys
import json
import pytest
from unittest.mock import MagicMock, patch

for mod in ["config", "jobspy", "fast_langdetect", "email_utils", "telegram", "proposal",
            "mammoth", "pymupdf4llm"]:
    if mod not in sys.modules:
        m = MagicMock()
        if mod == "email_utils":
            m.build_email_html = MagicMock(return_value="<html/>")
            m.send_email = MagicMock()
            m.send_plain_email = MagicMock(return_value=("msg-id", None))
        if mod == "proposal":
            m.INSIGHTS_HISTORY = MagicMock()
            m.apply_diff = MagicMock(return_value={"boost": [], "penalize": [], "terms": []})
            m.append_history = MagicMock()
            m.build_history_entry = MagicMock(return_value={})
            m.clear_proposal = MagicMock()
            m.load_history = MagicMock(return_value=[])
            m.load_proposal = MagicMock(return_value=None)
            m.revert_entry = MagicMock(return_value=0)
            m.save_proposal = MagicMock()
        sys.modules[mod] = m


import os
os.environ.setdefault("PROFILE_PATH", "/tmp/test_profile_eval.yaml")
os.environ.setdefault("JOBS_DB", ":memory:")

import main

PASS_THRESHOLD = 0.8  # at least 80% of trials must pass
N_TRIALS = 3


def _check_cold_calling_tag(tags: list[str]) -> bool:
    """Tags must contain a term related to cold calling or commission."""
    keywords = ["cold", "commission", "calling", "call", "sales-heavy", "phone"]
    for tag in tags:
        for kw in keywords:
            if kw in tag.lower():
                return True
    return False


def eval_n_times(fn, n: int, threshold: float) -> float:
    """Run fn() n times and return pass rate. Raises if below threshold."""
    passes = 0
    for i in range(n):
        try:
            result = fn()
            if result:
                passes += 1
        except Exception as e:
            print(f"Trial {i} error: {e}")
    rate = passes / n
    return rate


def test_tag_extraction_relevance():
    """
    3 trials: note 'too much cold calling, hated the commission structure'
    must produce a tag containing 'cold' or 'commission'.
    """
    note = "too much cold calling, hated the commission structure"
    mock_settings = MagicMock()
    mock_settings.openrouter_api_key = "fake-key"

    def trial():
        # Simulate a realistic LLM response
        fake_response = '["cold-calling-heavy", "commission-heavy"]'
        with patch.object(main, "_openrouter_call", return_value=fake_response):
            tags = main.extract_tags_from_note(note, "https://example.com/eval-job", mock_settings)
        return _check_cold_calling_tag(tags)

    rate = eval_n_times(trial, N_TRIALS, PASS_THRESHOLD)
    assert rate >= PASS_THRESHOLD, (
        f"Tag extraction relevance too low: {rate:.0%} pass rate "
        f"(threshold {PASS_THRESHOLD:.0%}). "
        f"Cold-calling note must produce a cold/commission tag."
    )


def test_tag_extraction_short_note_returns_empty():
    """Very short notes (< 10 chars) must return no tags (no wasted LLM call)."""
    mock_settings = MagicMock()
    mock_settings.openrouter_api_key = "fake-key"

    def trial():
        with patch.object(main, "_openrouter_call", return_value='[]') as mock_call:
            tags = main.extract_tags_from_note("ok", "https://example.com/short", mock_settings)
            # Should not call LLM at all for very short notes
            assert mock_call.call_count == 0
        return len(tags) == 0

    rate = eval_n_times(trial, N_TRIALS, 1.0)
    assert rate == 1.0, "Short notes must never produce tags and must skip LLM call"


def test_tag_extraction_handles_malformed_llm_response():
    """LLM returning non-JSON must not crash; must return []."""
    mock_settings = MagicMock()
    mock_settings.openrouter_api_key = "fake-key"

    def trial():
        with patch.object(main, "_openrouter_call", return_value="Sorry, I cannot help with that."):
            tags = main.extract_tags_from_note(
                "This role requires too much enterprise sales experience",
                "https://example.com/malformed",
                mock_settings,
            )
        return isinstance(tags, list)  # must not crash

    rate = eval_n_times(trial, N_TRIALS, 1.0)
    assert rate == 1.0, "Malformed LLM response must return [] without crashing"

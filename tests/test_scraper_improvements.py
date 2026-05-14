"""Tests for scraper pre-filters, entity boost, and scoring prompt."""
import sys
import os
# conftest.py mocks jobspy before this import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scraper'))

from scrape import location_allowed, language_ok, apply_entity_boost, SCORING_PROMPT


# ── Location filter ───────────────────────────────────────────────────────────

def test_location_pass_ontario():
    assert location_allowed("Mississauga, Ontario, Canada", False, ["Ontario", "Toronto"]) is True

def test_location_pass_toronto():
    assert location_allowed("Toronto, ON, CA", False, ["Toronto", "Ontario"]) is True

def test_location_pass_remote():
    assert location_allowed("Vancouver, BC", True, ["Ontario", "Toronto"]) is True

def test_location_fail_bc():
    assert location_allowed("Vancouver, British Columbia, Canada", False, ["Ontario", "Toronto"]) is False

def test_location_fail_quebec():
    assert location_allowed("Témiscouata-sur-le-Lac, QC, CA", False, ["Ontario", "Toronto"]) is False

def test_location_disabled_none():
    assert location_allowed("Vancouver, BC", False, None) is True

def test_location_disabled_empty():
    assert location_allowed("Vancouver, BC", False, []) is True

def test_location_case_insensitive():
    assert location_allowed("toronto, on, ca", False, ["Toronto"]) is True


# ── Language filter ───────────────────────────────────────────────────────────

def test_language_disabled_none():
    assert language_ok("Gérant des ventes au détail", None) is True

def test_language_disabled_empty_string():
    assert language_ok("Gérant·e des ventes", "") is True

def test_language_english_passes():
    import sys
    sys.modules["fast_langdetect"].detect.return_value = {"lang": "en"}
    text = "Account Manager - Business Development Representative Toronto Ontario"
    assert language_ok(text, "en") is True

def test_language_french_filtered():
    import sys
    sys.modules["fast_langdetect"].detect.return_value = {"lang": "fr"}
    text = "Gestionnaire des comptes commerciaux Ventes et développement des affaires"
    assert language_ok(text, "en") is False

def test_language_detection_error_allows_through():
    import sys
    sys.modules["fast_langdetect"].detect.side_effect = Exception("model not found")
    assert language_ok("some text", "en") is True
    sys.modules["fast_langdetect"].detect.side_effect = None


# ── Entity boost ──────────────────────────────────────────────────────────────

SCORING_CONFIG = {
    "boost": [
        {"keyword": "HubSpot", "weight": 16},
        {"keyword": "CRM", "weight": 11},
        {"keyword": "business development", "weight": 20},
    ],
    "penalize": [
        {"keyword": "entry level", "weight": -50},
        {"keyword": "healthcare", "weight": -35},
        {"keyword": "warehouse", "weight": -25},
    ],
}

def test_entity_boost_positive_single():
    desc = "You will use HubSpot to manage client relationships."
    result = apply_entity_boost(60.0, desc, SCORING_CONFIG)
    assert result > 60.0
    assert result <= 80.0  # capped at +20

def test_entity_boost_positive_cap():
    desc = "HubSpot, CRM, business development role with client focus."
    result = apply_entity_boost(60.0, desc, SCORING_CONFIG)
    assert result == 80.0  # 60 + 20 (capped)

def test_entity_boost_negative():
    desc = "Entry level position in our healthcare division."
    result = apply_entity_boost(70.0, desc, SCORING_CONFIG)
    assert result == 50.0  # 70 + (-20 cap)

def test_entity_boost_negative_cap():
    desc = "Entry level warehouse position in healthcare."
    result = apply_entity_boost(70.0, desc, SCORING_CONFIG)
    assert result == 50.0  # 70 - 20 (capped at -20)

def test_entity_boost_clamp_upper():
    desc = "HubSpot CRM business development focus."
    result = apply_entity_boost(95.0, desc, SCORING_CONFIG)
    assert result == 100.0

def test_entity_boost_clamp_lower():
    desc = "Entry level warehouse healthcare."
    result = apply_entity_boost(5.0, desc, SCORING_CONFIG)
    assert result == 0.0

def test_entity_boost_no_match():
    desc = "Software engineering role requiring Python and Kubernetes."
    result = apply_entity_boost(45.0, desc, SCORING_CONFIG)
    assert result == 45.0

def test_entity_boost_case_insensitive():
    desc = "HUBSPOT and crm experience required."
    result = apply_entity_boost(60.0, desc, SCORING_CONFIG)
    assert result > 60.0

def test_entity_boost_empty_scoring():
    result = apply_entity_boost(72.0, "Some job description", {})
    assert result == 72.0

def test_entity_boost_empty_description():
    result = apply_entity_boost(60.0, "", SCORING_CONFIG)
    assert result == 60.0


# ── Scoring prompt ────────────────────────────────────────────────────────────

def test_scoring_prompt_no_boost_keyword():
    assert "boost" not in SCORING_PROMPT.lower()

def test_scoring_prompt_no_penalize_keyword():
    assert "penalize" not in SCORING_PROMPT.lower()

def test_scoring_prompt_has_score_anchors():
    assert "90-100" in SCORING_PROMPT
    assert "70-89" in SCORING_PROMPT
    assert "50-69" in SCORING_PROMPT

def test_scoring_prompt_discourages_round_numbers():
    assert "round" in SCORING_PROMPT.lower()

def test_scoring_prompt_has_format_placeholders():
    assert "{profile_json}" in SCORING_PROMPT
    assert "{title}" in SCORING_PROMPT
    assert "{description}" in SCORING_PROMPT

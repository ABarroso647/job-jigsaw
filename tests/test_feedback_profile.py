"""Tests for feedback summary generation (profile-editor)."""
from __future__ import annotations
import sys
import os
import json
from unittest.mock import MagicMock, patch

# ── Stub heavy deps before importing the module ───────────────────────────────
for mod in [
    "config", "email_utils", "telegram", "proposal",
    "mammoth", "pymupdf4llm",
    "fastapi", "fastapi.responses", "fastapi.staticfiles",
    "fastapi.templating", "pydantic", "pydantic_settings",
    "requests", "aiofiles",
]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

# Stub sub-modules fastapi needs
sys.modules.setdefault("fastapi.exceptions", MagicMock())

# Make pydantic.BaseModel usable as a real base class
import types
real_base = type("BaseModel", (), {"__init_subclass__": classmethod(lambda cls, **kw: None)})
sys.modules["pydantic"].BaseModel = real_base

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'profile-editor'))

# Patch FastAPI / StaticFiles / Jinja2Templates so module import doesn't crash
with patch.dict(sys.modules, {
    "fastapi": MagicMock(),
    "fastapi.responses": MagicMock(),
    "fastapi.staticfiles": MagicMock(),
    "fastapi.templating": MagicMock(),
}):
    import importlib
    import main as pe

# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_row(title, employer, user_rating=None, notes=None):
    """Simulate a sqlite3.Row-like dict."""
    return {"title": title, "employer": employer, "user_rating": user_rating, "notes": notes}


MOCK_SETTINGS = MagicMock()
MOCK_SETTINGS.openrouter_api_key = "test-key"
MOCK_SETTINGS.openrouter_model = "deepseek/deepseek-v4-flash"


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_refresh_builds_summary():
    rows = [
        _make_row("Account Manager", "Acme", user_rating=1),
        _make_row("Delivery Driver", "Logistics", user_rating=-1, notes="Not relevant"),
    ]
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": '{"summary": "Prefers sales roles, dislikes logistics."}'}}]
    }

    with patch.object(pe, "_openrouter_call", return_value='{"summary": "Prefers sales roles, dislikes logistics."}'):
        summary = pe._generate_feedback_summary(rows, MOCK_SETTINGS)

    assert "sales" in summary.lower() or "prefer" in summary.lower()


def test_refresh_with_no_feedback():
    summary = pe._generate_feedback_summary([], MOCK_SETTINGS)
    assert summary == ""


def test_generate_feedback_includes_liked_and_disliked():
    rows = [
        _make_row("SaaS AE", "CloudCo", user_rating=1),
        _make_row("Warehouse Lead", "ShipFast", user_rating=-1),
    ]
    captured_prompt = {}

    def fake_call(settings, prompt, timeout=60):
        captured_prompt["value"] = prompt
        return '{"summary": "Likes SaaS, dislikes warehouse."}'

    with patch.object(pe, "_openrouter_call", side_effect=fake_call):
        pe._generate_feedback_summary(rows, MOCK_SETTINGS)

    prompt = captured_prompt["value"]
    assert "liked" in prompt
    assert "disliked" in prompt
    assert "SaaS AE" in prompt
    assert "Warehouse Lead" in prompt


def test_generate_feedback_includes_notes():
    rows = [_make_row("BDR", "Corp", user_rating=1, notes="Great HubSpot role")]
    captured = {}

    def fake_call(settings, prompt, timeout=60):
        captured["prompt"] = prompt
        return '{"summary": "Likes HubSpot roles."}'

    with patch.object(pe, "_openrouter_call", side_effect=fake_call):
        pe._generate_feedback_summary(rows, MOCK_SETTINGS)

    assert "Great HubSpot role" in captured["prompt"]


def test_generate_feedback_handles_bad_json():
    rows = [_make_row("Account Exec", "Corp", user_rating=1)]

    with patch.object(pe, "_openrouter_call", return_value="not json at all"):
        summary = pe._generate_feedback_summary(rows, MOCK_SETTINGS)

    assert summary == "not json at all"

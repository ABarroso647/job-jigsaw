"""Unit tests for Branch B — candidate profile overhaul (structured fields + wiki)."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# Mock only modules that genuinely aren't available outside Docker.
# We keep real fastapi but mock StaticFiles to avoid the directory issue.
# ---------------------------------------------------------------------------

_DOCKER_ONLY = [
    "jobspy", "fast_langdetect",
    "email_utils", "telegram", "proposal",
    "mammoth", "pymupdf4llm",
    "aiofiles",
]
for mod in _DOCKER_ONLY:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

# Stub pydantic_settings (not installed in conda env)
if "pydantic_settings" not in sys.modules:
    sys.modules["pydantic_settings"] = MagicMock()

# Mock StaticFiles so app.mount("/static", ...) doesn't raise about missing dir
import fastapi.staticfiles  # noqa: E402
fastapi.staticfiles.StaticFiles = MagicMock(return_value=MagicMock())

# config stub — must happen before main.py is loaded
config_mock = MagicMock()
config_mock.get_settings.return_value = MagicMock(
    openrouter_api_key="test-key",
    openrouter_model="deepseek/deepseek-v4-flash",
)
sys.modules["config"] = config_mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "profile-editor"))

import main as pe  # noqa: E402

DEFAULT_PROFILE = pe.DEFAULT_PROFILE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_profile(path: Path, data: dict) -> None:
    with open(path, "w") as f:
        yaml.dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)


def _read_profile(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _make_profile_file(tmp_path: Path, extra: dict | None = None) -> Path:
    p = tmp_path / "profile.yaml"
    data = {
        **DEFAULT_PROFILE,
        "resume": "Sample resume text with HubSpot at Acme Corp.",
    }
    if extra:
        data.update(extra)
    _write_profile(p, data)
    return p


# ---------------------------------------------------------------------------
# B1 — DEFAULT_PROFILE has new keys
# ---------------------------------------------------------------------------

def test_default_profile_has_experience_key():
    assert "experience" in DEFAULT_PROFILE
    assert isinstance(DEFAULT_PROFILE["experience"], list)


def test_default_profile_has_projects_key():
    assert "projects" in DEFAULT_PROFILE
    assert isinstance(DEFAULT_PROFILE["projects"], list)


def test_default_profile_has_structured_skills_key():
    assert "structured_skills" in DEFAULT_PROFILE
    assert isinstance(DEFAULT_PROFILE["structured_skills"], list)


def test_default_profile_has_wiki_key():
    assert "wiki" in DEFAULT_PROFILE
    assert DEFAULT_PROFILE["wiki"] == ""


def test_default_profile_has_wiki_updated_at():
    assert "wiki_updated_at" in DEFAULT_PROFILE
    assert DEFAULT_PROFILE["wiki_updated_at"] is None


def test_default_profile_has_resume_health():
    assert "resume_health" in DEFAULT_PROFILE
    assert DEFAULT_PROFILE["resume_health"] is None


# ---------------------------------------------------------------------------
# B2 — Wiki save and get (via TestClient)
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient

    profile_path = _make_profile_file(tmp_path)
    with patch.object(pe, "PROFILE_PATH", profile_path):
        yield TestClient(pe.app)


def test_wiki_save_and_get(client):
    """PUT /api/profile/wiki then GET returns same content."""
    wiki_text = "# Candidate Wiki\n## Identity & Target Role\nTest candidate."

    put_resp = client.put(
        "/api/profile/wiki",
        json={"wiki": wiki_text},
    )
    assert put_resp.status_code == 200
    assert put_resp.json().get("ok") is True

    get_resp = client.get("/api/profile/wiki")
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert data["wiki"] == wiki_text
    assert data["updated_at"] is not None


# ---------------------------------------------------------------------------
# B2 — Regenerate wiki endpoint (mock LLM)
# ---------------------------------------------------------------------------

def test_regenerate_wiki_endpoint_exists(client):
    """POST /api/profile/regenerate-wiki returns 200 with mocked LLM."""
    mock_wiki = "# Candidate Wiki\n## Identity & Target Role\nMocked wiki content."
    with patch.object(pe, "_openrouter_call", return_value=mock_wiki):
        resp = client.post("/api/profile/regenerate-wiki")
    assert resp.status_code == 200
    data = resp.json()
    assert "wiki" in data
    assert data["wiki"] == mock_wiki
    assert "updated_at" in data


# ---------------------------------------------------------------------------
# B1 — Migrate from resume endpoint (mock LLM)
# ---------------------------------------------------------------------------

def test_migrate_from_resume_endpoint_exists(client):
    """POST /api/profile/migrate-from-resume returns 200 with mocked LLM."""
    mock_payload = json.dumps({
        "experience": [
            {
                "title": "Account Executive",
                "company": "Acme Corp",
                "start": "2022-01",
                "end": "2024-06",
                "description": "Closed $800K ARR",
                "skills": ["HubSpot", "SaaS"],
            }
        ],
        "projects": [],
        "structured_skills": [
            {"name": "HubSpot", "category": "crm", "evidence_level": "experience"}
        ],
    })
    with patch.object(pe, "_openrouter_call", return_value=mock_payload):
        resp = client.post("/api/profile/migrate-from-resume")
    assert resp.status_code == 200
    data = resp.json()
    assert data["migrated"] is True
    assert data["experience_count"] == 1
    assert data["skills_count"] == 1


# ---------------------------------------------------------------------------
# B4 — Analyze resume returns health object (mock LLM + mock DB)
# ---------------------------------------------------------------------------

def test_analyze_resume_returns_health_object(client, tmp_path):
    """POST /api/profile/analyze-resume returns score and suggestions."""
    db_path = tmp_path / "jobs.db"
    con = sqlite3.connect(db_path)
    con.execute("""
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY, title TEXT, employer TEXT, location TEXT,
            job_url TEXT UNIQUE, suitability_score INTEGER,
            suitability_reason TEXT, date_posted TEXT, is_remote INTEGER,
            job_type TEXT, discovered_at TEXT, user_rating INTEGER,
            notes TEXT, hidden INTEGER DEFAULT 0, is_applied INTEGER DEFAULT 0,
            description TEXT
        )
    """)
    con.execute(
        "INSERT INTO jobs "
        "(id, title, employer, location, job_url, suitability_score, discovered_at, description) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            "1", "Account Executive", "Acme", "Toronto", "http://ex.com/1",
            80, "2026-05-01T00:00:00",
            "Looking for AE with HubSpot experience and MEDDIC methodology.",
        ),
    )
    con.commit()
    con.close()

    mock_health = json.dumps({
        "score": 72,
        "suggestions": ["Add MEDDIC to skills (appears in 8/10 recent jobs)"],
        "dimensions": {
            "keyword_coverage": 68,
            "specificity": 65,
            "recency_signal": 80,
            "seniority_alignment": 75,
        },
    })

    with (
        patch.object(pe, "JOBS_DB", db_path),
        patch.object(pe, "_openrouter_call", return_value=mock_health),
    ):
        resp = client.post("/api/profile/analyze-resume")

    assert resp.status_code == 200
    data = resp.json()
    assert "score" in data
    assert isinstance(data["score"], (int, float))
    assert "suggestions" in data
    assert len(data["suggestions"]) > 0
    assert "dimensions" in data

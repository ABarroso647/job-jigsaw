"""Shared test fixtures and import mocking for modules that need Docker deps."""
import sys
import sqlite3
import pytest
from unittest.mock import MagicMock

# Mock dependencies not available outside Docker
for mod in ["jobspy", "fast_langdetect", "config"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()


def pytest_configure(config):
    """Register custom markers so frontend tests don't emit PytestUnknownMarkWarning."""
    config.addinivalue_line("markers", "playwright: browser tests driven via Playwright")


SAMPLE_PROFILE = {
    "resume": "Account Executive with 3 years SaaS sales experience...",
    "description": "Experienced AE seeking SaaS roles in Ontario",
    "scoring": {"boost": [], "penalize": []},
    "search": {"keywords": ["Account Executive"], "locations": ["Toronto, ON", "Canada"]},
    "notification": {"score_threshold": 60, "max_jobs_per_email": 5},
}


@pytest.fixture
def sample_profile():
    return SAMPLE_PROFILE.copy()


@pytest.fixture
def in_memory_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE jobs (
        job_url TEXT PRIMARY KEY, title TEXT, employer TEXT, location TEXT,
        suitability_score REAL, suitability_reason TEXT, date_posted TEXT,
        is_remote INTEGER, job_type TEXT, user_rating INTEGER, notes TEXT,
        hidden INTEGER DEFAULT 0, discovered_at TEXT DEFAULT CURRENT_TIMESTAMP,
        description TEXT, language TEXT
    )""")
    con.commit()
    yield con
    con.close()

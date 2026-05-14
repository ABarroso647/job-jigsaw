"""Tests for notifier staleness filter and Jina re-ranker."""
from __future__ import annotations
import sys
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

# conftest mocks config, email_utils, telegram
for mod in ["config", "email_utils", "telegram"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'notifier'))
from notify import fetch_unsent_jobs, rerank_with_jina, STALE_DATE_POSTED_DAYS


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_jobs_db(jobs: list[dict]) -> sqlite3.Connection:
    """Create in-memory jobs.db with given rows."""
    con = sqlite3.connect(":memory:")
    con.execute("""
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY, title TEXT, employer TEXT, location TEXT,
            job_url TEXT UNIQUE, suitability_score REAL, suitability_reason TEXT,
            date_posted TEXT, is_remote INTEGER, job_type TEXT,
            discovered_at TEXT, user_rating INTEGER, hidden INTEGER,
            description TEXT, language TEXT
        )
    """)
    for j in jobs:
        con.execute("""
            INSERT INTO jobs (id, title, employer, location, job_url,
                suitability_score, suitability_reason, date_posted, is_remote,
                job_type, discovered_at, user_rating, hidden, description, language)
            VALUES (:id, :title, :employer, :location, :job_url,
                :suitability_score, :suitability_reason, :date_posted, :is_remote,
                :job_type, :discovered_at, :user_rating, :hidden, :description, :language)
        """, j)
    con.commit()
    return con


def make_sent_db(sent_urls: list[str] | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE sent_jobs (job_url TEXT PRIMARY KEY, sent_at TEXT)")
    if sent_urls:
        con.executemany("INSERT INTO sent_jobs VALUES (?, ?)",
                        [(u, "2026-01-01") for u in sent_urls])
    con.commit()
    return con


def days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


def date_days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


BASE_JOB = {
    "id": "1", "title": "Account Manager", "employer": "Acme", "location": "Toronto, ON",
    "job_url": "https://example.com/job/1", "suitability_score": 80.0,
    "suitability_reason": "Good fit", "date_posted": None, "is_remote": 0,
    "job_type": "full_time", "discovered_at": days_ago(1), "user_rating": None,
    "hidden": 0, "description": "Great role with HubSpot", "language": "en",
}

BASE_PROFILE = {
    "resume": "Sales professional in Toronto",
    "notification": {
        "score_threshold": 60,
        "max_job_age_days": 7,
        "max_jobs_per_email": 5,
        "rerank_candidates": 30,
        "rerank_min_score": 0.3,
        "timezone": "America/Toronto",
        "email_subject": "{count} jobs",
        "telegram_message": "{count} jobs",
    }
}


# ── Staleness filter — date_posted primary signal ─────────────────────────────

def test_staleness_known_date_recent(monkeypatch):
    job = {**BASE_JOB, "date_posted": date_days_ago(5)}
    jobs_con = make_jobs_db([job])
    sent_con = make_sent_db()
    monkeypatch.setattr("notify.JOBS_DB", ":memory:")

    with patch("sqlite3.connect") as mock_connect:
        mock_connect.return_value = jobs_con
        results = fetch_unsent_jobs(BASE_PROFILE, sent_con)
    assert len(results) == 1


def test_staleness_known_date_old(monkeypatch):
    job = {**BASE_JOB, "date_posted": date_days_ago(20)}  # 20 days > 14
    jobs_con = make_jobs_db([job])
    sent_con = make_sent_db()

    with patch("sqlite3.connect") as mock_connect:
        mock_connect.return_value = jobs_con
        results = fetch_unsent_jobs(BASE_PROFILE, sent_con)
    assert len(results) == 0


def test_staleness_nan_date_recent_discovered(monkeypatch):
    job = {**BASE_JOB, "date_posted": "nan", "discovered_at": days_ago(2)}
    jobs_con = make_jobs_db([job])
    sent_con = make_sent_db()

    with patch("sqlite3.connect") as mock_connect:
        mock_connect.return_value = jobs_con
        results = fetch_unsent_jobs(BASE_PROFILE, sent_con)
    assert len(results) == 1


def test_staleness_nan_date_old_discovered(monkeypatch):
    job = {**BASE_JOB, "date_posted": "nan", "discovered_at": days_ago(10)}  # > 7 day fallback
    jobs_con = make_jobs_db([job])
    sent_con = make_sent_db()

    with patch("sqlite3.connect") as mock_connect:
        mock_connect.return_value = jobs_con
        results = fetch_unsent_jobs(BASE_PROFILE, sent_con)
    assert len(results) == 0


def test_staleness_empty_date_recent_discovered(monkeypatch):
    job = {**BASE_JOB, "date_posted": "", "discovered_at": days_ago(1)}
    jobs_con = make_jobs_db([job])
    sent_con = make_sent_db()

    with patch("sqlite3.connect") as mock_connect:
        mock_connect.return_value = jobs_con
        results = fetch_unsent_jobs(BASE_PROFILE, sent_con)
    assert len(results) == 1


def test_disliked_job_excluded(monkeypatch):
    job = {**BASE_JOB, "user_rating": -1, "date_posted": date_days_ago(1)}
    jobs_con = make_jobs_db([job])
    sent_con = make_sent_db()

    with patch("sqlite3.connect") as mock_connect:
        mock_connect.return_value = jobs_con
        results = fetch_unsent_jobs(BASE_PROFILE, sent_con)
    assert len(results) == 0


def test_already_sent_excluded(monkeypatch):
    job = {**BASE_JOB, "date_posted": date_days_ago(1)}
    jobs_con = make_jobs_db([job])
    sent_con = make_sent_db(sent_urls=[job["job_url"]])

    with patch("sqlite3.connect") as mock_connect:
        mock_connect.return_value = jobs_con
        results = fetch_unsent_jobs(BASE_PROFILE, sent_con)
    assert len(results) == 0


# ── Jina re-ranker ────────────────────────────────────────────────────────────

MOCK_JOBS = [
    {"title": "Account Manager", "company": "Acme", "location": "Toronto",
     "job_url": "https://example.com/1", "score": 78, "reason": "Good", "description": "CRM role"},
    {"title": "BDR", "company": "Corp", "location": "Toronto",
     "job_url": "https://example.com/2", "score": 72, "reason": "OK", "description": "Sales role"},
    {"title": "Delivery Driver", "company": "Logistics", "location": "Brampton",
     "job_url": "https://example.com/3", "score": 65, "reason": "Poor", "description": "Drive truck"},
]

MOCK_SETTINGS = MagicMock()
MOCK_SETTINGS.jina_api_key = "test-key"


def test_jina_rerank_reorders():
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "results": [
            {"index": 2, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.8},
            {"index": 1, "relevance_score": 0.5},
        ]
    }
    mock_response.raise_for_status = MagicMock()

    with patch("notify.requests.post", return_value=mock_response):
        result = rerank_with_jina(MOCK_JOBS, BASE_PROFILE, MOCK_SETTINGS)

    assert result[0]["title"] == "Delivery Driver"
    assert result[1]["title"] == "Account Manager"
    assert result[2]["title"] == "BDR"


def test_jina_rerank_filters_low_score():
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "results": [
            {"index": 0, "relevance_score": 0.8},
            {"index": 1, "relevance_score": 0.4},
            {"index": 2, "relevance_score": 0.1},  # below min_score=0.3
        ]
    }
    mock_response.raise_for_status = MagicMock()

    with patch("notify.requests.post", return_value=mock_response):
        result = rerank_with_jina(MOCK_JOBS, BASE_PROFILE, MOCK_SETTINGS)

    assert len(result) == 2
    assert all(j["title"] != "Delivery Driver" for j in result)


def test_jina_rerank_fallback_on_error():
    with patch("notify.requests.post", side_effect=Exception("network error")):
        result = rerank_with_jina(MOCK_JOBS, BASE_PROFILE, MOCK_SETTINGS)
    assert result == MOCK_JOBS


def test_jina_disabled_when_no_key():
    settings = MagicMock()
    settings.jina_api_key = ""
    result = rerank_with_jina(MOCK_JOBS, BASE_PROFILE, settings)
    assert result == MOCK_JOBS


def test_jina_uses_description_when_available():
    mock_response = MagicMock()
    mock_response.json.return_value = {"results": [{"index": 0, "relevance_score": 0.8}]}
    mock_response.raise_for_status = MagicMock()

    with patch("notify.requests.post", return_value=mock_response) as mock_post:
        rerank_with_jina([MOCK_JOBS[0]], BASE_PROFILE, MOCK_SETTINGS)
        call_args = mock_post.call_args
        documents = call_args[1]["json"]["documents"]
        assert "CRM role" in documents[0]


def test_jina_falls_back_to_reason_when_no_description():
    job_no_desc = {**MOCK_JOBS[0], "description": "", "reason": "Excellent sales background match"}
    mock_response = MagicMock()
    mock_response.json.return_value = {"results": [{"index": 0, "relevance_score": 0.8}]}
    mock_response.raise_for_status = MagicMock()

    with patch("notify.requests.post", return_value=mock_response) as mock_post:
        rerank_with_jina([job_no_desc], BASE_PROFILE, MOCK_SETTINGS)
        call_args = mock_post.call_args
        documents = call_args[1]["json"]["documents"]
        assert "Excellent sales background match" in documents[0]


def test_jina_includes_feedback_summary_in_query():
    profile_with_feedback = {
        **BASE_PROFILE,
        "feedback_summary": "User prefers SaaS companies and remote work."
    }
    mock_response = MagicMock()
    mock_response.json.return_value = {"results": [{"index": 0, "relevance_score": 0.8}]}
    mock_response.raise_for_status = MagicMock()

    with patch("notify.requests.post", return_value=mock_response) as mock_post:
        rerank_with_jina([MOCK_JOBS[0]], profile_with_feedback, MOCK_SETTINGS)
        call_args = mock_post.call_args
        query = call_args[1]["json"]["query"]
        assert "SaaS" in query

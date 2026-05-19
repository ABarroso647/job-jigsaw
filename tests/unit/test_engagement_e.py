"""Unit tests for Branch E: user engagement features (pipeline + dynamic tags)."""
import json
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

# Mock Docker-only deps before importing main
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


def _make_db(path: Path) -> None:
    """Create a minimal jobs.db with E1/E2 columns."""
    con = sqlite3.connect(path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            title TEXT,
            employer TEXT,
            location TEXT,
            job_url TEXT UNIQUE,
            suitability_score INTEGER,
            suitability_reason TEXT,
            date_posted TEXT,
            is_remote INTEGER,
            job_type TEXT,
            discovered_at TEXT DEFAULT CURRENT_TIMESTAMP,
            user_rating INTEGER,
            notes TEXT,
            hidden INTEGER DEFAULT 0,
            is_applied INTEGER DEFAULT 0,
            status TEXT,
            status_updated_at TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            sentiment REAL DEFAULT 0,
            count INTEGER DEFAULT 0
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS job_tags (
            job_url TEXT NOT NULL,
            tag_id INTEGER NOT NULL,
            PRIMARY KEY (job_url, tag_id),
            FOREIGN KEY (tag_id) REFERENCES tags(id)
        )
    """)
    con.commit()
    con.close()


@pytest.fixture
def tmp_db(tmp_path):
    db = tmp_path / "jobs.db"
    _make_db(db)
    return db


@pytest.fixture
def app_with_db(tmp_db, tmp_path):
    """Import main with patched DB paths."""
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text("notification:\n  score_threshold: 60\n  max_jobs_per_email: 10\n  max_job_age_days: 7\n")

    import importlib
    if "main" in sys.modules:
        del sys.modules["main"]

    with patch.dict("os.environ", {
        "PROFILE_PATH": str(profile_path),
        "JOBS_DB": str(tmp_db),
    }):
        import main as m
        m.JOBS_DB = tmp_db
        m.PROFILE_PATH = profile_path
        yield m


# ── Helpers ────────────────────────────────────────────────────────────────────

def _insert_job(db: Path, url: str, score: int = 75, status: str = None,
                days_old: int = 0, notes: str = None) -> None:
    con = sqlite3.connect(db)
    discovered = f"datetime('now', '-{days_old} days')"
    con.execute(
        f"INSERT OR IGNORE INTO jobs (id, title, employer, job_url, suitability_score, "
        f"discovered_at, status, notes) "
        f"VALUES (?, ?, ?, ?, ?, {discovered}, ?, ?)",
        (url, f"Job {url}", "Acme", url, score, status, notes),
    )
    con.commit()
    con.close()


# ── E1 tests ───────────────────────────────────────────────────────────────────

def test_status_update_persists(tmp_db, app_with_db):
    """PUT /api/jobs/{url}/status with 'applied' → job has status='applied' in DB."""
    _insert_job(tmp_db, "https://example.com/job/1")
    m = app_with_db
    m.update_job_status("https://example.com/job/1", m.StatusUpdate(status="applied"))
    con = sqlite3.connect(tmp_db)
    row = con.execute("SELECT status FROM jobs WHERE job_url=?", ("https://example.com/job/1",)).fetchone()
    con.close()
    assert row and row[0] == "applied"


def test_interested_bypasses_staleness(tmp_db, app_with_db):
    """Job with status='interested' appears in query_jobs even if very old."""
    _insert_job(tmp_db, "https://example.com/job/old", score=75, status="interested", days_old=30)
    m = app_with_db
    results = m.query_jobs(threshold=60, max_jobs=10, max_job_age_days=7)
    urls = [j["job_url"] for j in results]
    assert "https://example.com/job/old" in urls, "interested job should bypass staleness"


def test_applied_excluded_from_query(tmp_db, app_with_db):
    """Job with status='applied' is NOT returned by query_jobs."""
    _insert_job(tmp_db, "https://example.com/job/applied", score=80, status="applied", days_old=1)
    m = app_with_db
    results = m.query_jobs(threshold=60, max_jobs=10, max_job_age_days=7)
    urls = [j["job_url"] for j in results]
    assert "https://example.com/job/applied" not in urls, "applied job should be excluded"


def test_invalid_status_rejected(tmp_db, app_with_db):
    """status='nonsense' → 400 HTTPException."""
    from fastapi import HTTPException
    _insert_job(tmp_db, "https://example.com/job/2")
    m = app_with_db
    with pytest.raises(HTTPException) as exc_info:
        m.update_job_status("https://example.com/job/2", m.StatusUpdate(status="nonsense"))
    assert exc_info.value.status_code == 400


def test_interviewing_excluded_from_query(tmp_db, app_with_db):
    """Job with status='interviewing' is NOT returned by query_jobs."""
    _insert_job(tmp_db, "https://example.com/job/int", score=80, status="interviewing", days_old=1)
    m = app_with_db
    results = m.query_jobs(threshold=60, max_jobs=10, max_job_age_days=7)
    urls = [j["job_url"] for j in results]
    assert "https://example.com/job/int" not in urls


def test_null_status_included_in_query(tmp_db, app_with_db):
    """Job with no status appears in query_jobs normally."""
    _insert_job(tmp_db, "https://example.com/job/nostat", score=80, status=None, days_old=1)
    m = app_with_db
    results = m.query_jobs(threshold=60, max_jobs=10, max_job_age_days=7)
    urls = [j["job_url"] for j in results]
    assert "https://example.com/job/nostat" in urls


# ── E2 tests ───────────────────────────────────────────────────────────────────

def test_tags_table_created(tmp_db, app_with_db):
    """After _init_jobs_db, tags table exists in DB."""
    m = app_with_db
    m._init_jobs_db()
    con = sqlite3.connect(tmp_db)
    row = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tags'").fetchone()
    con.close()
    assert row is not None, "tags table should exist"


def test_seed_tags_inserted(tmp_db, app_with_db):
    """'cold-calling-heavy' exists in tags after _init_jobs_db."""
    m = app_with_db
    m._init_jobs_db()
    con = sqlite3.connect(tmp_db)
    row = con.execute("SELECT name FROM tags WHERE name=?", ("cold-calling-heavy",)).fetchone()
    con.close()
    assert row is not None, "seed tag cold-calling-heavy should be present"


def test_add_tag_to_job(tmp_db, app_with_db):
    """POST /api/jobs/{url}/tags → tag linked to job."""
    _insert_job(tmp_db, "https://example.com/job/3")
    m = app_with_db
    m.add_job_tag("https://example.com/job/3", {"name": "remote-friendly"})
    con = sqlite3.connect(tmp_db)
    row = con.execute("""
        SELECT t.name FROM job_tags jt JOIN tags t ON t.id = jt.tag_id
        WHERE jt.job_url = ?
    """, ("https://example.com/job/3",)).fetchone()
    con.close()
    assert row and row[0] == "remote-friendly"


def test_remove_tag_from_job(tmp_db, app_with_db):
    """DELETE /api/jobs/{url}/tags/{name} → tag unlinked."""
    _insert_job(tmp_db, "https://example.com/job/4")
    m = app_with_db
    m.add_job_tag("https://example.com/job/4", {"name": "too-junior"})
    m.remove_job_tag("https://example.com/job/4", "too-junior")
    con = sqlite3.connect(tmp_db)
    rows = con.execute("""
        SELECT t.name FROM job_tags jt JOIN tags t ON t.id = jt.tag_id
        WHERE jt.job_url = ?
    """, ("https://example.com/job/4",)).fetchall()
    con.close()
    assert len(rows) == 0, "tag should have been removed"


def test_tag_extraction_returns_list(tmp_db, app_with_db):
    """Mock LLM returns ['cold-calling-heavy'] → stored in DB."""
    _insert_job(tmp_db, "https://example.com/job/5", notes="too much cold calling")
    m = app_with_db

    mock_settings = MagicMock()
    mock_settings.openrouter_api_key = "fake-key"

    with patch.object(m, "_openrouter_call", return_value='["cold-calling-heavy"]'):
        tags = m.extract_tags_from_note(
            "too much cold calling required in this role",
            "https://example.com/job/5",
            mock_settings,
        )

    assert isinstance(tags, list)
    assert "cold-calling-heavy" in tags


def test_tag_count_increments(tmp_db, app_with_db):
    """Adding a tag increments its count."""
    _insert_job(tmp_db, "https://example.com/job/6")
    m = app_with_db
    m.add_job_tag("https://example.com/job/6", {"name": "new-tag-xyz"})
    con = sqlite3.connect(tmp_db)
    row = con.execute("SELECT count FROM tags WHERE name=?", ("new-tag-xyz",)).fetchone()
    con.close()
    assert row and row[0] == 1


def test_tag_count_not_double_incremented(tmp_db, app_with_db):
    """Adding the same tag to the same job twice doesn't double-count."""
    _insert_job(tmp_db, "https://example.com/job/7")
    m = app_with_db
    m.add_job_tag("https://example.com/job/7", {"name": "remote-friendly"})
    m.add_job_tag("https://example.com/job/7", {"name": "remote-friendly"})
    con = sqlite3.connect(tmp_db)
    row = con.execute("SELECT count FROM tags WHERE name=?", ("remote-friendly",)).fetchone()
    con.close()
    assert row and row[0] == 1, "count should not exceed 1 for same job+tag"


def test_tag_delta_adjusts_score(tmp_db, app_with_db):
    """A job with a negative tag gets its score lowered by tag delta."""
    _insert_job(tmp_db, "https://example.com/job/8", score=80, days_old=1)
    m = app_with_db
    # Add a negative-sentiment tag manually
    con = sqlite3.connect(tmp_db)
    con.execute("INSERT OR IGNORE INTO tags (name, sentiment, count) VALUES ('cold-calling-heavy', -0.8, 1)")
    tag_id = con.execute("SELECT id FROM tags WHERE name='cold-calling-heavy'").fetchone()[0]
    con.execute("INSERT OR IGNORE INTO job_tags (job_url, tag_id) VALUES (?,?)",
                ("https://example.com/job/8", tag_id))
    con.commit()
    con.close()

    results = m.query_jobs(threshold=60, max_jobs=10, max_job_age_days=7)
    job = next((j for j in results if j["job_url"] == "https://example.com/job/8"), None)
    assert job is not None, "job should still appear in results"
    assert job["score"] < 80, f"score should be reduced by tag delta, got {job['score']}"

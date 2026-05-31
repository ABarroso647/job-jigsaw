"""Auto-loaded conftest for frontend tests.

pytest only auto-discovers files named ``conftest.py``. The Playwright fixtures
live in ``tests/conftest_frontend.py`` (a plain module, shared verbatim across
feature branches), so we re-export them here to register ``live_app`` and
``mock_profile`` for every test under ``tests/frontend/``.
"""
import os
import sqlite3
import subprocess
from datetime import date, timedelta

import pytest
import yaml

# Re-export the session-scoped fixtures so pytest discovers them. We also reuse
# conftest_frontend's launch helpers (find_free_port / _wait_for_server) and repo
# path constants below so the seeded-app fixture boots uvicorn the SAME way the
# baseline live_app does, rather than re-implementing port/poll/launch logic.
from tests.conftest_frontend import (  # noqa: F401
    live_app,
    mock_profile,
    find_free_port,
    _wait_for_server,
    PROFILE_EDITOR_DIR,
    REPO_ROOT,
)
from tests.conftest import SAMPLE_PROFILE, seed_jobs


def _seed_sent_db(data_dir, sent_rows):
    """Create the sent_jobs.db that main.py's _sent_map()/query_jobs() reads.

    main.py never CREATEs this DB (the notifier owns it externally); it only ever
    runs ``SELECT job_url, sent_at FROM sent_jobs`` (main.py:180) to build the set
    of URLs that preview/query_jobs excludes (main.py:216-220). So we create a
    table with exactly the columns that query is read against and insert the rows
    we want treated as "already sent". ``sent_rows`` is a list of (job_url, sent_at).
    """
    con = sqlite3.connect(data_dir / "sent_jobs.db")
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS sent_jobs (job_url TEXT PRIMARY KEY, sent_at TEXT)"
        )
        con.executemany(
            "INSERT OR REPLACE INTO sent_jobs (job_url, sent_at) VALUES (?, ?)",
            sent_rows,
        )
        con.commit()
    finally:
        con.close()


@pytest.fixture
def seeded_app(tmp_path):
    """Boot the editor against a temp DATA_DIR pre-seeded with known jobs.

    Function-scoped so it never disturbs the session-scoped ``live_app`` baseline.
    Seeds a realistic profile.yaml (threshold/max so our jobs qualify for preview),
    a jobs.db via the editor's REAL schema (tests.conftest.seed_jobs), and a
    sent_jobs.db built through the same read path main.py uses for exclusion.

    Yields a dict with the base ``url`` plus the seeded jobs (``unsent`` / ``sent``
    / ``rateable``) so tests can assert against known titles/urls.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # Profile: explicit notification thresholds so the seeded jobs land in preview.
    # score_threshold 50 keeps both seeded jobs (scores 80/90) above threshold.
    profile = {
        **SAMPLE_PROFILE,
        "notification": {
            "score_threshold": 50,
            "max_jobs_per_email": 25,
            "max_job_age_days": 7,
        },
    }
    (data_dir / "profile.yaml").write_text(yaml.safe_dump(profile))

    # A recent date_posted so query_jobs' freshness window keeps these rows.
    # Computed relative to today (yesterday) so it never ages out of the 14-day
    # stale window; a hardcoded date would silently rot the behavioral tests.
    recent = (date.today() - timedelta(days=1)).isoformat()
    unsent = {
        "id": "job-unsent-1",
        "title": "Senior Account Executive",
        "employer": "Acme SaaS",
        "location": "Toronto, ON",
        "job_url": "https://jobs.example.com/unsent-1",
        "suitability_score": 90,
        "date_posted": recent,
        "is_remote": 1,
        "job_type": "fulltime",
    }
    sent = {
        "id": "job-sent-1",
        "title": "Already Emailed Rep",
        "employer": "OldCo",
        "location": "Toronto, ON",
        "job_url": "https://jobs.example.com/sent-1",
        "suitability_score": 80,
        "date_posted": recent,
        "is_remote": 0,
        "job_type": "fulltime",
    }
    seed_jobs(data_dir, [unsent, sent])
    # Mark the second job as sent via the same table main.py reads for exclusion.
    _seed_sent_db(data_dir, [(sent["job_url"], "2026-05-28T09:00:00")])

    port = find_free_port()
    lib_dir = REPO_ROOT / "lib"
    pythonpath = os.pathsep.join(
        p for p in (str(lib_dir), os.environ.get("PYTHONPATH", "")) if p
    )
    proc = subprocess.Popen(
        ["python", "-m", "uvicorn", "main:app", "--host", "127.0.0.1", f"--port={port}"],
        cwd=str(PROFILE_EDITOR_DIR),
        env={**os.environ, "DATA_DIR": str(data_dir), "PYTHONPATH": pythonpath},
    )
    url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_server(url, proc)
        yield {"url": url, "unsent": unsent, "sent": sent, "rateable": unsent}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

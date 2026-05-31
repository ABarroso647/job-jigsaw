"""Shared test fixtures and import mocking for modules that need Docker deps."""
import os
import sys
import sqlite3
import pathlib
import uuid
import pytest
from unittest.mock import MagicMock

# `jobspy` and `fast_langdetect` are heavy/native deps that are only installed
# inside the Docker image — they're genuinely unavailable in the local test env,
# so we stub them with MagicMock to let modules that import them load.
for mod in ["jobspy", "fast_langdetect"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

# `config` (lib/config.py) is deliberately NOT mocked. It's a pydantic
# BaseSettings with three required fields; a blanket MagicMock would make every
# `settings.<attr>` a truthy MagicMock (garbage in URLs/payloads, `if` always
# true), hiding real bugs. Instead we make the real module importable and feed
# it dummy values so `get_settings()` returns one real, reusable Settings object
# with concrete string attributes. In Docker, PYTHONPATH=/app/lib puts config on
# the path; locally we add the repo's lib/ dir ourselves.
_LIB_DIR = pathlib.Path(__file__).resolve().parent.parent / "lib"
if _LIB_DIR.is_dir() and str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

# Dummy values for the three required settings (mirrors .env.example) so the real
# pydantic Settings validates. setdefault avoids clobbering a real env if present.
os.environ.setdefault("GMAIL_FROM", "sender@gmail.com")
os.environ.setdefault("GMAIL_TO", "recipient@gmail.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "test")


def pytest_configure(config):
    """Register custom markers so frontend tests don't emit PytestUnknownMarkWarning."""
    config.addinivalue_line("markers", "playwright: browser tests driven via Playwright")


SAMPLE_PROFILE = {
    "resume": "Account Executive with 3 years SaaS sales experience...",
    "description": "Experienced AE seeking SaaS roles in Ontario",
    "scoring": {"boost": [], "penalize": []},
    # Keys here MUST match DEFAULT_PROFILE["search"] in profile-editor/main.py
    # (the canonical schema). The app reads search...get("terms"); "keywords" is a
    # different concept (scoring boost/penalize lists) and is never read here.
    "search": {"terms": ["Account Executive"], "locations": ["Toronto, ON", "Canada"]},
    "notification": {"score_threshold": 60, "max_jobs_per_email": 5},
}


@pytest.fixture
def sample_profile():
    return SAMPLE_PROFILE.copy()


# profile-editor's `main` module is the single source of truth for the jobs DB
# schema (its `_init_jobs_db()` runs the canonical CREATE TABLE). Tests import it
# the same way test_profile_editor.py does — by putting `profile-editor` on
# sys.path — so the fixture/seed helper below can derive the schema from the real
# init path instead of hand-copying a CREATE TABLE (which previously drifted: the
# old fixture made job_url the PK, dropped `id`/`is_applied`, and used REAL).
_PROFILE_EDITOR_DIR = pathlib.Path(__file__).resolve().parent.parent / "profile-editor"
if _PROFILE_EDITOR_DIR.is_dir() and str(_PROFILE_EDITOR_DIR) not in sys.path:
    sys.path.insert(0, str(_PROFILE_EDITOR_DIR))


def _import_main():
    """Import profile-editor's `main` module (cached after first call).

    main.py mounts StaticFiles/Jinja2Templates with cwd-relative paths ("static",
    "templates") at import time, so we import with cwd temporarily set to
    profile-editor/ — matching how the app is actually launched (uvicorn runs with
    cwd=profile-editor in the live_app fixture).
    """
    if "main" in sys.modules:
        return sys.modules["main"]
    cwd = os.getcwd()
    try:
        os.chdir(_PROFILE_EDITOR_DIR)
        import main  # noqa: F401
    finally:
        os.chdir(cwd)
    return sys.modules["main"]


# Columns the editor's real `_init_jobs_db()` creates, in schema order. Kept here
# only so `seed_jobs` knows which keys are insertable; the table itself is always
# built by main._init_jobs_db(), never from this list.
_JOBS_COLUMNS = (
    "id", "title", "employer", "location", "job_url", "suitability_score",
    "suitability_reason", "date_posted", "is_remote", "job_type",
    "discovered_at", "user_rating", "notes", "hidden", "is_applied",
)


@pytest.fixture
def in_memory_db(tmp_path, monkeypatch):
    """A sqlite3 connection to a jobs DB built by the editor's REAL init path.

    Despite the historical name, this is a temp *file* DB (not :memory:) because
    main._init_jobs_db() opens/closes its own connection to main.JOBS_DB; sharing
    an in-memory DB across connections isn't possible. We point main.JOBS_DB at a
    temp file, run the real init, then open a fresh row-factory connection to it.
    Name kept as `in_memory_db` since nothing references it yet (lowest risk).
    """
    main = _import_main()

    db_path = tmp_path / "jobs.db"
    monkeypatch.setattr(main, "JOBS_DB", db_path)
    main._init_jobs_db()

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    yield con
    con.close()


def seed_jobs(data_dir, jobs):
    """Build a jobs DB via the editor's real init and insert `jobs` into it.

    For the behavioral frontend tests (rating / preview-exclusion) that need a
    populated jobs.db inside a temp DATA_DIR. Schema knowledge lives in ONE place:
    this points main.JOBS_DB at `data_dir/jobs.db`, calls main._init_jobs_db() to
    create the table, then INSERT OR IGNOREs each job using only columns that
    exist in the real editor schema. Omitted keys get sensible defaults; a UUID
    `id` is generated when absent.

    `data_dir` is a Path (a temp DATA_DIR like the live_app fixture uses) and
    `jobs` is a list of dicts keyed by editor-schema column names.
    """
    main = _import_main()

    db_path = pathlib.Path(data_dir) / "jobs.db"
    main.JOBS_DB = db_path
    main._init_jobs_db()

    con = sqlite3.connect(db_path)
    try:
        for job in jobs:
            row = {
                "id": job.get("id") or str(uuid.uuid4()),
                "title": job.get("title"),
                "employer": job.get("employer"),
                "location": job.get("location"),
                "job_url": job.get("job_url"),
                "suitability_score": job.get("suitability_score"),
                "suitability_reason": job.get("suitability_reason"),
                "date_posted": job.get("date_posted"),
                "is_remote": job.get("is_remote"),
                "job_type": job.get("job_type"),
                "discovered_at": job.get("discovered_at"),
                "user_rating": job.get("user_rating"),
                "notes": job.get("notes"),
                "hidden": job.get("hidden", 0),
                "is_applied": job.get("is_applied", 0),
            }
            cols = ", ".join(row.keys())
            placeholders = ", ".join(f":{k}" for k in row.keys())
            con.execute(
                f"INSERT OR IGNORE INTO jobs ({cols}) VALUES ({placeholders})", row
            )
        con.commit()
    finally:
        con.close()

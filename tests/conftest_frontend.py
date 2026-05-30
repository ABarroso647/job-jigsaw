"""Playwright fixtures for frontend tests."""
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

from tests.conftest import SAMPLE_PROFILE

# Repo paths derived relative to this file so tests run anywhere (CI, any checkout),
# not just one developer's absolute home directory.
REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILE_EDITOR_DIR = REPO_ROOT / "profile-editor"


def find_free_port():
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _wait_for_server(url: str, proc: subprocess.Popen, timeout: float = 20.0) -> None:
    """Poll the server until it answers or the timeout elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"uvicorn exited early with code {proc.returncode}")
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except urllib.error.HTTPError:
            return  # server is up (any HTTP response means it's listening)
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.2)
    raise RuntimeError(f"server at {url} did not start within {timeout}s")


@pytest.fixture
def mock_profile(tmp_path):
    """Write a temp profile.yaml seeded from SAMPLE_PROFILE; yield its path.

    Function-scoped so each test gets a fresh, isolated profile it can mutate.
    """
    path = tmp_path / "profile.yaml"
    with open(path, "w") as f:
        yaml.safe_dump(SAMPLE_PROFILE, f)
    yield path


@pytest.fixture(scope="session")
def live_app(tmp_path_factory):
    """Spin up the FastAPI profile editor on a random port against a temp DATA_DIR.

    Setting DATA_DIR (consumed by main.py) points profile.yaml, jobs.db, sent_jobs.db,
    and insights_meta.json at a throwaway dir so tests never touch real /data. The app
    auto-creates a default profile.yaml and initializes the DB on startup, but we seed a
    realistic profile.yaml up front so the UI renders meaningful values.

    Yields the base URL as a string (callers depend on this being a plain str).
    """
    port = find_free_port()
    data_dir = tmp_path_factory.mktemp("data")

    # Seed a realistic profile so Settings/Resume tabs render real values.
    src_profile = REPO_ROOT / "data" / "profile.yaml"
    if src_profile.exists():
        (data_dir / "profile.yaml").write_text(src_profile.read_text())
    else:
        with open(data_dir / "profile.yaml", "w") as f:
            yaml.safe_dump(SAMPLE_PROFILE, f)

    # The app imports config/email_utils/telegram/proposal from lib/, which Docker
    # exposes via PYTHONPATH=/app/lib. Replicate that so uvicorn can import the app
    # when launched outside the container.
    lib_dir = REPO_ROOT / "lib"
    pythonpath = os.pathsep.join(p for p in (str(lib_dir), os.environ.get("PYTHONPATH", "")) if p)
    proc = subprocess.Popen(
        ["python", "-m", "uvicorn", "main:app", "--host", "127.0.0.1", f"--port={port}"],
        cwd=str(PROFILE_EDITOR_DIR),
        env={**os.environ, "DATA_DIR": str(data_dir), "PYTHONPATH": pythonpath},
    )
    url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_server(url, proc)
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

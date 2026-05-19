"""Playwright fixtures for frontend tests."""
import subprocess
import time
import socket
import shutil
import os
import pytest
from pathlib import Path


def find_free_port():
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def live_app(tmp_path_factory):
    """Spin up the FastAPI profile editor on a random port."""
    port = find_free_port()
    # Use a temp copy of profile.yaml so tests don't mutate real data
    tmp = tmp_path_factory.mktemp("data")
    shutil.copy(
        "/home/roton/git_git/job-hunt/data/profile.yaml",
        tmp / "profile.yaml",
    )
    proc = subprocess.Popen(
        [
            "python",
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            "127.0.0.1",
            f"--port={port}",
        ],
        cwd="/home/roton/git_git/job-hunt/profile-editor",
        env={
            **os.environ,
            "PROFILE_PATH": str(tmp / "profile.yaml"),
            "JOBS_DB": ":memory:",
        },
    )
    time.sleep(2)  # wait for startup
    yield f"http://127.0.0.1:{port}"
    proc.terminate()

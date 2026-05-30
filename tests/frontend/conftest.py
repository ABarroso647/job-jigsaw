"""Auto-loaded conftest for frontend tests.

pytest only auto-discovers files named ``conftest.py``. The Playwright fixtures
live in ``tests/conftest_frontend.py`` (a plain module, shared verbatim across
feature branches), so we re-export them here to register ``live_app`` and
``mock_profile`` for every test under ``tests/frontend/``.
"""
from tests.conftest_frontend import live_app, mock_profile  # noqa: F401

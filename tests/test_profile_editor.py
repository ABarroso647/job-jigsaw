"""Tests for profile-editor DEFAULT_PROFILE and settings roundtrip."""
from __future__ import annotations
import sys
import os
from unittest.mock import MagicMock

for mod in [
    "config", "email_utils", "telegram", "proposal",
    "mammoth", "pymupdf4llm",
    "fastapi", "fastapi.responses", "fastapi.staticfiles",
    "fastapi.templating", "pydantic", "pydantic_settings",
    "requests", "aiofiles",
]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

sys.modules.setdefault("fastapi.exceptions", MagicMock())
real_base = type("BaseModel", (), {"__init_subclass__": classmethod(lambda cls, **kw: None)})
sys.modules["pydantic"].BaseModel = real_base

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'profile-editor'))

import yaml
import main as pe


def test_default_profile_has_new_search_keys():
    search = pe.DEFAULT_PROFILE["search"]
    assert "require_language" in search
    assert search["require_language"] is None
    assert "allowed_regions" in search
    assert search["allowed_regions"] is None


def test_default_profile_has_new_notification_keys():
    notif = pe.DEFAULT_PROFILE["notification"]
    assert "max_job_age_days" in notif
    assert "rerank_candidates" in notif
    assert "rerank_min_score" in notif
    assert notif["max_job_age_days"] == 7
    assert notif["rerank_candidates"] == 30
    assert notif["rerank_min_score"] == 0.3


def test_null_allowed_regions_roundtrips():
    """None must survive a yaml.dump/yaml.safe_load roundtrip (not become [])."""
    data = {"search": {"allowed_regions": None, "require_language": None}}
    dumped = yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    loaded = yaml.safe_load(dumped)
    assert loaded["search"]["allowed_regions"] is None
    assert loaded["search"]["require_language"] is None


def test_allowed_regions_list_roundtrips():
    data = {"search": {"allowed_regions": ["Ontario", "Toronto", "Remote"]}}
    dumped = yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    loaded = yaml.safe_load(dumped)
    assert loaded["search"]["allowed_regions"] == ["Ontario", "Toronto", "Remote"]


def test_rerank_min_score_float_roundtrips():
    data = {"notification": {"rerank_min_score": 0.35}}
    dumped = yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    loaded = yaml.safe_load(dumped)
    assert abs(loaded["notification"]["rerank_min_score"] - 0.35) < 1e-9

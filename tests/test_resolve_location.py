"""Tests for location -> timezone resolution on profile save.

Covers `_resolve_location` (geopy + timezonefinder) and the `save_profile`
enrichment path that snaps the user's free-text location to a canonical name,
stores lat/lon, and writes `notification.timezone` — so the notifier can later
read the correct IANA zone (notify.py does `ZoneInfo(notif["timezone"])`).

Geo singletons are monkeypatched: the tests are deterministic and network-free
(and numpy never loads, since the lazy getters are overridden before use).
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
import yaml
import os
import sys
import pathlib

# Importing main for real triggers `app.mount("/static", StaticFiles(directory="static"))`,
# which resolves "static" relative to CWD — so chdir into profile-editor for the import
# (mirrors conftest._import_main), then restore CWD.
#
# Other test modules (e.g. test_profile_editor) globally stub fastapi/pydantic with
# MagicMock and import `main` under those mocks, leaving a poisoned cached `main` in
# sys.modules. We need REAL pydantic so ResolvedLocation stores fields and the real
# save_profile runs — so drop the mocked modules and import fresh.
for _m in list(sys.modules):
    if _m == "main" or _m.startswith("fastapi") or _m in ("pydantic", "pydantic_settings"):
        del sys.modules[_m]
_PE_DIR = pathlib.Path(__file__).resolve().parent.parent / "profile-editor"
sys.path.insert(0, str(_PE_DIR))
_cwd = os.getcwd()
os.chdir(_PE_DIR)
try:
    import main as pe
finally:
    os.chdir(_cwd)


@pytest.fixture
def isolated_profile(tmp_path, monkeypatch):
    """Point the editor at a throwaway profile.yaml so tests never touch prod data."""
    monkeypatch.setattr(pe, "PROFILE_PATH", tmp_path / "profile.yaml")
    return tmp_path / "profile.yaml"


class _FakeLoc:
    def __init__(self, address: str, lat: float, lon: float):
        self.address = address
        self.latitude = lat
        self.longitude = lon


def _patch_geo(monkeypatch, geocode_ret, tz_ret):
    fake_geo = MagicMock()
    fake_geo.geocode.return_value = geocode_ret
    fake_tzf = MagicMock()
    fake_tzf.timezone_at.return_value = tz_ret
    monkeypatch.setattr(pe, "_get_geocoder", lambda: fake_geo)
    monkeypatch.setattr(pe, "_get_tzf", lambda: fake_tzf)


# ── _resolve_location ────────────────────────────────────────────────────────

def test_resolve_location_success(monkeypatch):
    _patch_geo(monkeypatch, _FakeLoc("Toronto, Ontario, Canada", 43.65, -79.38), "America/Toronto")
    r = pe._resolve_location("Toronto, ON")
    assert r is not None
    assert r.name == "Toronto, Ontario, Canada"
    assert r.lat == pytest.approx(43.65)
    assert r.lon == pytest.approx(-79.38)
    assert r.timezone == "America/Toronto"


def test_resolve_location_unresolvable_returns_none(monkeypatch):
    _patch_geo(monkeypatch, None, "America/Toronto")
    assert pe._resolve_location("zzz nonsense 999") is None


def test_resolve_location_geocode_raises_returns_none(monkeypatch):
    fake_geo = MagicMock()
    fake_geo.geocode.side_effect = TimeoutError("nominatim down")
    monkeypatch.setattr(pe, "_get_geocoder", lambda: fake_geo)
    monkeypatch.setattr(pe, "_get_tzf", lambda: MagicMock())
    # Failures must degrade to None, never raise into save_profile.
    assert pe._resolve_location("anything") is None


def test_resolve_location_no_timezone_over_ocean(monkeypatch):
    # timezone_at legitimately returns None over water / unmapped points.
    _patch_geo(monkeypatch, _FakeLoc("Mid Atlantic", 35.0, -45.0), None)
    r = pe._resolve_location("Mid Atlantic")
    assert r is not None
    assert r.timezone is None


# ── save_profile enrichment ────────────────────────────────────────────────────

class _FakeRequest:
    def __init__(self, payload: dict):
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


def test_save_profile_writes_resolved_timezone_and_coords(isolated_profile, monkeypatch):
    _patch_geo(monkeypatch, _FakeLoc("Toronto, Ontario, Canada", 43.6534, -79.3839), "America/Toronto")
    payload = {"search": {"locations": ["Toronto, ON", "Canada"]}, "notification": {}}
    asyncio.run(pe.save_profile(_FakeRequest(payload)))

    saved = yaml.safe_load(isolated_profile.read_text())
    # canonical name snaps the free text; coords + IANA tz land in the profile
    assert saved["search"]["locations"][0] == "Toronto, Ontario, Canada"
    assert saved["search"]["lat"] == pytest.approx(43.6534)
    assert saved["search"]["lon"] == pytest.approx(-79.3839)
    assert saved["notification"]["timezone"] == "America/Toronto"


def test_save_profile_preserves_timezone_when_unresolvable(isolated_profile, monkeypatch):
    """A location that can't be geocoded must not wipe the existing tz the UI sent."""
    _patch_geo(monkeypatch, None, "America/Toronto")
    # Real UI flow: the existing timezone is always round-tripped back in the payload.
    payload = {
        "search": {"locations": ["zzz nonsense", "Canada"]},
        "notification": {"timezone": "America/Toronto", "score_threshold": 60},
    }
    asyncio.run(pe.save_profile(_FakeRequest(payload)))

    saved = yaml.safe_load(isolated_profile.read_text())
    # location untouched, no coords invented, tz preserved for the notifier
    assert saved["search"]["locations"][0] == "zzz nonsense"
    assert "lat" not in saved["search"]
    assert "lon" not in saved["search"]
    assert saved["notification"]["timezone"] == "America/Toronto"


def test_save_profile_skips_enrichment_when_no_locations(isolated_profile, monkeypatch):
    # No location field at all -> resolver never consulted, profile written as-is.
    fake_geo = MagicMock()
    monkeypatch.setattr(pe, "_get_geocoder", lambda: fake_geo)
    payload = {"search": {}, "notification": {"timezone": "America/Vancouver"}}
    asyncio.run(pe.save_profile(_FakeRequest(payload)))

    saved = yaml.safe_load(isolated_profile.read_text())
    assert saved["notification"]["timezone"] == "America/Vancouver"
    fake_geo.geocode.assert_not_called()
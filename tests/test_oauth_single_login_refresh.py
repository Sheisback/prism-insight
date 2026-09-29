"""One OAuth login shared by reports and the BUY/SELL Codex CLI (via the 18742 proxy).

The persistent proxy never refreshes, so (a) the healthcheck cron must rotate the
token before it can expire between 30-minute runs, and (b) a process holding a
stale cached token must adopt a newer file instead of refreshing with a rotated
refresh token. No network: refresh is stubbed.
"""
import asyncio
import importlib.util
import json
import time
from pathlib import Path

import pytest

from cores.chatgpt_proxy import token_manager as tm_mod

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def auth_file(tmp_path, monkeypatch):
    path = tmp_path / "chatgpt_auth.json"
    monkeypatch.setattr(tm_mod, "AUTH_FILE", path)
    monkeypatch.setattr(tm_mod, "AUTH_DIR", tmp_path)
    return path


def _write(path, *, access, refresh, expires_in):
    path.write_text(json.dumps({"access_token": access, "refresh_token": refresh, "account_id": "a",
                                "expires_at": int(time.time()) + expires_in}))


def test_stale_cache_adopts_a_newer_file_instead_of_refreshing(auth_file, monkeypatch):
    _write(auth_file, access="old", refresh="r1", expires_in=60)  # inside the 5-minute buffer
    manager = tm_mod.TokenManager()
    manager._auth_data = json.loads(auth_file.read_text())
    _write(auth_file, access="new", refresh="r2", expires_in=86400)  # rotated elsewhere
    calls = []

    async def no_refresh(data):
        calls.append(data)
        raise AssertionError("must not refresh with a rotated refresh token")

    monkeypatch.setattr(manager, "_refresh_token", no_refresh)
    assert asyncio.run(manager.get_token()) == "new" and calls == []


def test_expired_everywhere_refreshes_from_the_newest_file(auth_file, monkeypatch):
    _write(auth_file, access="old", refresh="r1", expires_in=60)
    manager = tm_mod.TokenManager()
    manager._auth_data = {"access_token": "older", "refresh_token": "r0", "expires_at": 0}
    seen = []

    async def refresh(data):
        seen.append(data["refresh_token"])
        return {**data, "access_token": "fresh", "expires_at": int(time.time()) + 86400}

    monkeypatch.setattr(manager, "_refresh_token", refresh)
    assert asyncio.run(manager.get_token()) == "fresh" and seen == ["r1"]


def _healthcheck():
    spec = importlib.util.spec_from_file_location("oauth_healthcheck_t", ROOT / "tools/oauth_healthcheck.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("expires_in,expect_refresh", [(3600, True), (6 * 3600, False)])
def test_healthcheck_rotates_before_the_cron_gap(auth_file, monkeypatch, expires_in, expect_refresh):
    hc = _healthcheck()
    monkeypatch.setattr("cores.chatgpt_proxy.constants.AUTH_FILE", auth_file)
    _write(auth_file, access="a1", refresh="r1", expires_in=expires_in)
    calls = []

    async def refresh(self, data):
        calls.append(data["refresh_token"])
        return {**data, "access_token": "a2", "expires_at": int(time.time()) + 10 * 86400}

    monkeypatch.setattr(tm_mod.TokenManager, "_refresh_token", refresh)
    healthy, detail = asyncio.run(hc._check_token())
    assert healthy is True
    assert calls == (["r1"] if expect_refresh else [])
    assert hc.PROACTIVE_REFRESH_HOURS * 60 > 30  # longer than the cron interval

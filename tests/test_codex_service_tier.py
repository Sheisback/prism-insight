import base64
import json

import pytest

from cores.llm import codex_oauth_fast_backend as backend
from cores.llm.codex_oauth_fast_backend import _command, codex_service_tier


def _write_session(home, email):
    claims = {"https://api.openai.com/profile": {"email": email}}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    auth = home / ".config" / "prism-insight" / "chatgpt_auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps({"access_token": f"h.{payload}.s"}))


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(backend.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("PRISM_CODEX_SERVICE_TIER", raising=False)
    monkeypatch.delenv("PRISM_CODEX_STANDARD_TIER_ACCOUNTS", raising=False)
    return tmp_path


def test_fast_command_is_unchanged():
    cmd = _command("codex", "gpt-6-astra", "kr_trading", "medium")
    assert 'service_tier="fast"' in cmd and "features.fast_mode=true" in cmd
    assert "features.fast_mode=false" not in cmd


def test_standard_command_disables_fast_mode():
    cmd = _command("codex", "gpt-6-astra", "kr_trading", "medium", fast_tier=False)
    assert "features.fast_mode=false" in cmd
    assert not any("service_tier" in arg for arg in cmd)
    assert cmd[-2:] == ["--json", "-"]


def test_auto_defaults_to_fast(home):
    _write_session(home, "a@example.com")
    assert codex_service_tier() == "fast"


def test_auto_listed_account_runs_standard(home, monkeypatch):
    _write_session(home, "Slow@Example.com")
    monkeypatch.setenv("PRISM_CODEX_STANDARD_TIER_ACCOUNTS", "other@example.com, slow@example.com")
    assert codex_service_tier() == "standard"


def test_auto_unlisted_or_unreadable_account_stays_fast(home, monkeypatch):
    monkeypatch.setenv("PRISM_CODEX_STANDARD_TIER_ACCOUNTS", "slow@example.com")
    assert codex_service_tier() == "fast"  # no session file
    _write_session(home, "fast@example.com")
    assert codex_service_tier() == "fast"


@pytest.mark.parametrize("forced", ["fast", "standard", " STANDARD "])
def test_forced_tier_overrides_account(home, monkeypatch, forced):
    _write_session(home, "slow@example.com")
    monkeypatch.setenv("PRISM_CODEX_STANDARD_TIER_ACCOUNTS", "slow@example.com")
    monkeypatch.setenv("PRISM_CODEX_SERVICE_TIER", forced)
    assert codex_service_tier() == forced.strip().lower()


def test_invalid_forced_value_falls_back_to_auto(home, monkeypatch):
    _write_session(home, "slow@example.com")
    monkeypatch.setenv("PRISM_CODEX_STANDARD_TIER_ACCOUNTS", "slow@example.com")
    monkeypatch.setenv("PRISM_CODEX_SERVICE_TIER", "turbo")
    assert codex_service_tier() == "standard"

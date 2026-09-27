from copy import deepcopy
import json

import pytest

from prism_core.oneil_adaptive_policy import _hash
from prism_core.oneil_config import ConfigurationError, capture_enabled, defaults, implementation_hash, load, validate


def config():
    return dict(mode="SHADOW", accounts=["primary"], capture_since="2026-09-27T00:00:00Z")


def test_missing_is_off_and_shadow_has_no_live_authority(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("ONEIL_EXECUTION_CONFIG", str(path))
    assert load()["mode"] == "OFF" and not capture_enabled()
    path.write_text(json.dumps(config()))
    assert capture_enabled() and load()["live_approval"] is None


def test_live_requires_explicit_policy_bound_unexpired_approval():
    value = dict(config(), mode="LIVE")
    with pytest.raises(ConfigurationError, match="APPROVAL"):
        validate(value)
    approval = dict(policy="oneil-adaptive-v1", initial_arm="INITIAL_POLICY_50",
                    scope="NEW_CAMPAIGNS_ONLY", accounts=["primary"], approved_by="test-operator",
                    approved_at="2026-09-27T00:00:00Z", expires_at="2026-09-28T00:00:00Z",
                    max_unit_budget_usd="1000", implementation_hash=implementation_hash())
    approval["approval_hash"] = _hash(approval)
    value["live_approval"] = approval
    assert validate(value, now="2026-09-27T10:00:00Z")["mode"] == "LIVE"
    for key, altered in (("accounts", ["other"]), ("initial_arm", "COMPATIBILITY_SCOUT_10")):
        wrong = deepcopy(value)
        wrong["live_approval"][key] = altered
        with pytest.raises(ConfigurationError):
            validate(wrong, now="2026-09-27T10:00:00Z")
    with pytest.raises(ConfigurationError):
        validate(value, now="2026-09-29T00:00:00Z")


def test_no_alias_databases_or_unknown_credentials():
    value = config()
    value["live_db"] = defaults()["holdings_db"]
    with pytest.raises(ConfigurationError, match="distinct"):
        validate(value)
    with pytest.raises(ConfigurationError, match="unknown"):
        validate(dict(config(), api_key="not-allowed"))


def test_malformed_cannot_silently_become_off(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("bad")
    with pytest.raises(ConfigurationError):
        load(path)


def test_expired_approval_does_not_hide_existing_protection_paths(tmp_path):
    value = dict(config(), mode="LIVE", live_approval=None)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ConfigurationError):
        load(path)
    assert load(path, protection_only=True)["live_db"] == defaults()["live_db"]


def test_shadow_configuration_enables_all_existing_capture_inputs(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config()))
    monkeypatch.setenv("ONEIL_EXECUTION_CONFIG", str(path))
    monkeypatch.delenv("SCENARIO_SHADOW_CAPTURE_ENABLED", raising=False)
    monkeypatch.delenv("ONEIL_AUTO_REVIEW_CAPTURE_ENABLED", raising=False)
    monkeypatch.delenv("ONEIL_TAPE_CAPTURE_ENABLED", raising=False)
    from observability.scenario_shadow import capture_enabled as initial_enabled
    from observability.oneil_capture import enabled as tape_enabled
    from prism_core.oneil_batch_setup import enabled as report_enabled
    assert initial_enabled() and tape_enabled() and report_enabled()

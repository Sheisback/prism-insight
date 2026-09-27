from copy import deepcopy

import pytest

from prism_core.oneil_current_capture import capture_current_record
from prism_core.oneil_live_boundary import assert_mode, project_live_candidate, readiness
from test_oneil_runtime import fixture


def inputs(tmp_path):
    runtime, cid, args, _ = fixture(tmp_path)
    result = runtime.advance(cid, capture_current_record(**args), expected_revision=0)
    intent = result["intent"]
    identity = {k: intent[k] for k in ("campaign_id", "position_id", "symbol", "source_decision_ref", "plan_hash")}
    receipt = dict(identity, contract_version="oneil-owned-account-receipt-v1", account_id="account:A",
                   source_ref="reconciled:1", execution_profile_ref="profile:A", currency="USD",
                   mode="LIVE", basis="CONFIRMED_BROKER_FILLS", ownership="ONEIL_INITIAL_SCOUT",
                   initial_target_pct=10, adopted=False, execution_status="CONFIRMED", settlement_status="SETTLED",
                   unknown_execution=False, observed_at=args["now"], reserved_buy_notional="0",
                   account_unit_budget="10000", confirmed_buy_notional="1000", previously_submitted_target_pct=10)
    quote = dict(identity, account_id="account:A", currency="USD", source_ref="quote:1",
                 price="104", observed_at=args["now"])
    return runtime, intent["intent_id"], dict(plan_hash=intent["plan_hash"], receipt=receipt, quote=quote, now=args["now"])


def test_environment_cannot_replace_explicit_live_approval(monkeypatch):
    monkeypatch.setenv("ONEIL_MODE", "LIVE")
    monkeypatch.setenv("ONEIL_LIVE_APPROVED", "true")
    assert assert_mode("SHADOW") == "SHADOW"
    assert assert_mode("OFF") == "OFF"
    assert not readiness()["live_ready"]
    assert readiness()["technical_switch_ready"]
    assert not any("NOT_IMPLEMENTED" in code for code in readiness()["blockers"])
    with pytest.raises(ValueError, match="LIVE_UNAVAILABLE"):
        assert_mode("LIVE")
    with pytest.raises(ValueError, match="UNSUPPORTED"):
        assert_mode("live")


def test_projection_whole_shares_only_never_execution(tmp_path):
    runtime, intent_id, kwargs = inputs(tmp_path)
    before = deepcopy(kwargs)
    result = project_live_candidate(runtime, intent_id, **kwargs)
    assert result["status"] == "PLANNED" and result["quantity"] == 86
    assert result["projected_notional"] == "8944"
    assert result["no_order"] and not result["execution_authorized"] and not result["live_ready"]
    assert kwargs == before


@pytest.mark.parametrize("field,value", [
    ("mode", "SHADOW"), ("basis", "STRATEGY_LEDGER"), ("ownership", "OTHER"),
    ("initial_target_pct", 100), ("adopted", True), ("position_id", "other"),
    ("execution_status", "UNKNOWN"), ("execution_status", "PARTIAL"),
    ("settlement_status", "PENDING"), ("reserved_buy_notional", "1"),
    ("unknown_execution", True), ("observed_at", "2026-09-25T13:30:00Z"),
    ("confirmed_buy_notional", "10001"), ("currency", "KRW"),
])
def test_invalid_unsettled_or_foreign_receipt_is_blocked(tmp_path, field, value):
    runtime, intent_id, kwargs = inputs(tmp_path)
    kwargs["receipt"][field] = value
    result = project_live_candidate(runtime, intent_id, **kwargs)
    assert result["status"] == "BLOCKED" and result["quantity"] == 0


@pytest.mark.parametrize("field,value", [("observed_at", "2026-09-25T13:30:00Z"),
                                        ("account_id", "other"), ("price", "105"),
                                        ("source_ref", ""), ("price", float("nan"))])
def test_quote_binding_freshness_and_price_recheck(tmp_path, field, value):
    runtime, intent_id, kwargs = inputs(tmp_path)
    kwargs["quote"][field] = value
    assert project_live_candidate(runtime, intent_id, **kwargs)["status"] == "BLOCKED"


def test_submitted_target_retry_cannot_double_order(tmp_path):
    runtime, intent_id, kwargs = inputs(tmp_path)
    kwargs["receipt"]["previously_submitted_target_pct"] = 100
    result = project_live_candidate(runtime, intent_id, **kwargs)
    assert result["reason"] == "TARGET_ALREADY_SUBMITTED"
    assert result["quantity"] == 0


def test_fabricated_intent_id_planhash_and_old_intent_blocked(tmp_path):
    runtime, intent_id, kwargs = inputs(tmp_path)
    assert project_live_candidate(runtime, "invented", **kwargs)["status"] == "BLOCKED"
    wrong = dict(kwargs, plan_hash="invented")
    assert project_live_candidate(runtime, intent_id, **wrong)["status"] == "BLOCKED"
    later = dict(kwargs, now="2026-09-25T13:45:00Z")
    assert project_live_candidate(runtime, intent_id, **later)["reason"] == "STALE_INTENT"


def test_initial_fifty_requires_matching_owned_receipt(tmp_path):
    runtime, cid, args, _ = fixture(tmp_path, "INITIAL_POLICY_50")
    result = runtime.advance(cid, capture_current_record(**args), expected_revision=0)
    intent = result["intent"]
    identity = {k: intent[k] for k in ("campaign_id", "position_id", "symbol", "source_decision_ref", "plan_hash")}
    receipt = dict(identity, contract_version="oneil-owned-account-receipt-v1", account_id="account:A",
                   source_ref="reconciled:1", execution_profile_ref="profile:A", currency="USD",
                   mode="LIVE", basis="CONFIRMED_BROKER_FILLS", ownership="ONEIL_INITIAL_POLICY_50",
                   initial_target_pct=50, initial_max_pct=50, adopted=False, execution_status="CONFIRMED",
                   settlement_status="SETTLED", unknown_execution=False, observed_at=args["now"],
                   reserved_buy_notional="0", account_unit_budget="10000", confirmed_buy_notional="5000",
                   previously_submitted_target_pct=50)
    quote = dict(identity, account_id="account:A", currency="USD", source_ref="quote:1",
                 price="104", observed_at=args["now"])
    kwargs = dict(plan_hash=intent["plan_hash"], receipt=receipt, quote=quote, now=args["now"])
    result = project_live_candidate(runtime, intent["intent_id"], **kwargs)
    assert result["reason"] == "TARGET_ALREADY_SUBMITTED"
    receipt["initial_max_pct"] = 100
    assert project_live_candidate(runtime, intent["intent_id"], **kwargs)["reason"] == "OWNED_INITIAL_SCOUT_RECEIPT_REQUIRED"

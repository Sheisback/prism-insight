"""v2 review fixes: config protection, best-effort volume, ATR age, v1 integration paths."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
import json
from zoneinfo import ZoneInfo

import pytest

from prism_core.oneil_adaptive_policy import V1_VERSION, VERSION, _hash, _time, create_plan, evaluate_target
from prism_core.oneil_config import ConfigurationError, load, require_live_approval
from prism_core.oneil_dispatcher import _clock, _validator
from prism_core.oneil_execution import OneilExecution
from prism_core.oneil_input_bridge import assemble_evidence
from prism_core.oneil_intraday_inputs import build_intraday_inputs
from prism_core.oneil_shadow_runner import ShadowRunner
from test_oneil_adaptive_policy import evidence as v1_evidence
from test_oneil_adaptive_v2 import INITIAL, _approval, _config, _v1_arguments, plan, setup
from test_oneil_execution import reserve
from test_oneil_shadow_runner import setup as runner_setup

NY = ZoneInfo("America/New_York")


def _legacy(frozen):
    return create_plan(policy_version=V1_VERSION, symbol=frozen["symbol"],
                       entry_reference=frozen["entry_reference"], initial_stop=frozen["initial_stop"],
                       source_decision_ref=frozen["source_decision_ref"], created_at=frozen["created_at"],
                       setup={k: v for k, v in frozen["setup"].items() if not k.startswith("atr14")},
                       entry_eligible=True)


# --- L5: create_plan bounds the ATR age -------------------------------------

@pytest.mark.parametrize("change,ok", [
    ({"atr14_as_of": "2026-09-24T13:00:00Z", "atr14_last_trade_date": "2026-09-23"}, False),  # >1 day old
    ({"atr14_as_of": "2026-09-24T13:30:00Z", "atr14_last_trade_date": "2026-09-23"}, True),   # exactly 1 day
    ({"atr14_last_trade_date": "2026-09-19"}, False),                                          # 6 days before
    ({"atr14_last_trade_date": "2026-09-20"}, True),                                           # 5 days before
])
def test_create_plan_bounds_atr_age(change, ok):
    if ok:
        assert plan(frozen_setup=dict(setup(), **change))["policy_version"] == VERSION
    else:
        with pytest.raises(ValueError, match="stale ATR"):
            plan(frozen_setup=dict(setup(), **change))


# --- M1: protection-only load of a LIVE file that still names v1 -------------

def test_protection_only_load_of_live_v1_file_keeps_working(tmp_path):
    now = "2026-09-27T10:00:00Z"
    path = tmp_path / "oneil-execution.json"
    value = _config(mode="LIVE", policy=V1_VERSION, live_approval=_approval(V1_VERSION))
    path.write_text(json.dumps(value))
    loaded = load(path, protection_only=True, now=now)
    assert loaded["mode"] == "LIVE" and loaded["policy"] == V1_VERSION
    with pytest.raises(ConfigurationError):
        load(path, now=now)
    with pytest.raises(ConfigurationError, match="LIVE_APPROVAL_INVALID_OR_EXPIRED"):
        require_live_approval(loaded, now=now)
    # A v2 approval cannot authorize new risk while the file still names v1.
    path.write_text(json.dumps(dict(value, live_approval=_approval(VERSION))))
    with pytest.raises(ConfigurationError):
        load(path, now=now)
    assert load(path, protection_only=True, now=now)["mode"] == "LIVE"


# --- M2: volume availability never gates v2 at the builder ------------------

def _long_fixture(half_day_index=None, zero_prior_volume=False, elapsed=225):
    """Bars up to `elapsed` minutes into each session, plus each prior session's final bar."""
    dates = v1_evidence()["volume"]["expected_prior_trade_dates"] + ["2026-09-25"]
    sessions, bars = [], []
    for index, day in enumerate(dates):
        opened = datetime.fromisoformat(day + "T09:30:00").replace(tzinfo=NY)
        length = 210 if index == half_day_index else 390
        sessions.append({"trade_date": day, "open_at": opened.isoformat(),
                         "close_at": (opened + timedelta(minutes=length)).isoformat()})
        today = index == len(dates) - 1
        minutes = list(range(0, elapsed if today else min(elapsed, length), 5))
        if not today and length - 5 not in minutes:
            minutes.append(length - 5)
        for minute in minutes:
            close = 81 + index if not today and minute == length - 5 else 104
            volume = 75 if today else (0 if zero_prior_volume else 50)
            bars.append({"provider_timestamp": (opened + timedelta(minutes=minute)).isoformat(),
                         "open": close, "high": close, "low": close, "close": close,
                         "volume": volume, "dividends": 0, "stock_splits": 0})
    as_of = datetime(2026, 9, 25, 9, 30, tzinfo=NY) + timedelta(minutes=elapsed)
    return dict(symbol="TEST", bars=bars, calendar={"calendar_ref": "exchange-fixture", "sessions": sessions},
                as_of=as_of.isoformat(), retrieved_at=(as_of + timedelta(seconds=30)).isoformat(),
                retrieval_started_at=as_of.isoformat(), price_basis_ref="unadjusted-v1",
                source_ref="provider-fixture", kind="LIVE_CAPTURE")


def _evaluate_from_builder(frozen, intraday, deployed):
    at = _time(intraday["as_of"])
    observed, now = (at + timedelta(seconds=30)).isoformat(), (at + timedelta(seconds=60)).isoformat()
    base = v1_evidence()
    assembled = assemble_evidence(plan=frozen, setup_input=dict(status="OK", setup=frozen["setup"]),
                                  intraday_input=intraday, quote=dict(base["quote"], observed_at=observed),
                                  gates=dict(base["gates"], observed_at=observed), now=now)
    if assembled["status"] != "OK":
        return assembled
    state = (dict(cumulative_allocation=".5", remaining_allocation=".5", normalized_units=".005",
                  remaining_entry_cost=".0005", current_stop="100") if deployed else INITIAL)
    return evaluate_target(frozen, assembled["evidence"], now=now, **state)


@pytest.mark.parametrize("kind,strict_reason", [
    ("half_day", "PRIOR_SESSION_TOO_SHORT"), ("zero_volume", "MISSING_VOLUME_DENOMINATOR")])
def test_builder_volume_is_best_effort_for_v2_and_strict_for_v1(kind, strict_reason):
    data = _long_fixture(half_day_index=10) if kind == "half_day" else _long_fixture(zero_prior_volume=True)
    strict = build_intraday_inputs(**data)
    assert (strict["status"], strict["reason_codes"]) == ("MISSING", [strict_reason])
    relaxed = build_intraday_inputs(**data, volume_required=False)
    assert relaxed["status"] == "OK" and relaxed["usable_for_prospective"] is True
    assert relaxed["volume"] is None and relaxed["reason_codes"] == ["MATCHED_VOLUME_UNAVAILABLE"]
    assert len(relaxed["bars"]) == 2 and relaxed["trend"]["closes"] == [str(81 + i) for i in range(20)]
    first = _evaluate_from_builder(plan(), relaxed, deployed=False)
    assert first["action"] == "ADD" and Decimal(first["target_allocation"]) == Decimal(".5")
    add = _evaluate_from_builder(plan(), relaxed, deployed=True)
    assert add["action"] == "ADD" and Decimal(add["target_allocation"]) == 1
    # A frozen v1 plan still refuses input without matched volume.
    refused = _evaluate_from_builder(_legacy(plan()), relaxed, deployed=False)
    assert refused["reason_codes"] == ["MATCHED_VOLUME_REQUIRED_FOR_V1"] and refused["evidence"] is None


def test_builder_best_effort_keeps_matched_volume_and_current_prefix_strict():
    data = _long_fixture()
    strict, relaxed = build_intraday_inputs(**data), build_intraday_inputs(**data, volume_required=False)
    assert strict == relaxed and strict["status"] == "OK" and strict["volume"] is not None
    first = _evaluate_from_builder(_legacy(plan()), strict, deployed=False)
    assert first["action"] == "ADD"
    gap = _long_fixture()
    gap["bars"] = [b for b in gap["bars"] if b["provider_timestamp"] != "2026-09-25T10:00:00-04:00"]
    assert build_intraday_inputs(**gap, volume_required=False)["reason_codes"] == ["REGULAR_PREFIX_GAP"]


# --- M3: v1 campaigns through execution, dispatcher and the shadow runner ---

def _v1_start(tmp_path, mode="SHADOW"):
    args = _v1_arguments()
    core = OneilExecution(tmp_path / "execution.sqlite", mode=mode)
    account = dict(status="OK", account_id="account", symbol="TEST", quantity=0, observed_at=args["now"],
                   source_ref="account-source", open_orders_status="OK", open_orders_count=0)
    state = core.claim_campaign(account_id="account", position_id=args["position_id"], plan=args["plan"],
                                unit_budget=10000, account_snapshot=account, now=args["now"])
    return core, state["campaign_id"], args, account


def test_v1_execution_keeps_volume_gate_and_v1_intent_source(tmp_path):
    core, cid, args, account = _v1_start(tmp_path)
    assert core.snapshot(cid)["plan"]["policy_version"] == V1_VERSION
    weak = deepcopy(args)
    weak["intraday_input"]["volume"]["cumulative_volume"] = "140"
    waiting = reserve(core, cid, weak, account)
    assert waiting["status"] == "WAIT" and waiting["decision"]["reason"] == "VOLUME_NOT_CONFIRMED"
    reserved = reserve(core, cid, args, account)
    assert reserved["status"] == "RESERVED"
    assert reserved["intent"].source == "oneil-adaptive-v1" and reserved["intent"].quantity == 48


@pytest.mark.parametrize("target,quote,error", [
    ("1", 104, None), ("1", 101, "TARGET_PRICE_CONFIRMATION_LOST"),
    (".8", 102, None), (".8", 101, "TARGET_PRICE_CONFIRMATION_LOST"),
    (".5", 100.5, None),
    ("1", 105.5, "RESERVED_PRICE_OUTSIDE_POLICY"),   # pivot*1.05 cap still applies to v1 adds
])
def test_dispatch_validator_keeps_v1_band(tmp_path, monkeypatch, target, quote, error):
    core, cid, args, account = _v1_start(tmp_path, "LIVE")
    reserved = reserve(core, cid, args, account)
    reserved = dict(reserved, intent=replace(reserved["intent"], limit_price="111"))
    snapshot = core.snapshot(cid)
    snapshot["confirmed_quantity"] = 48
    snapshot["orders"][0]["target_allocation"] = target
    monkeypatch.setattr(core, "snapshot", lambda _: snapshot)
    validate = _validator(core, reserved, _clock(args["now"]), lambda: True)
    if error is None:
        validate(quote)
    else:
        with pytest.raises(ValueError, match=error):
            validate(quote)


def test_shadow_runner_resolves_v1_capture_to_v1_campaign(tmp_path, monkeypatch):
    import test_oneil_shadow_runner
    original_fixture = test_oneil_shadow_runner.fixture

    def v1_fixture(path):
        # The capture registry is immutable, so freeze the v1 plan before insertion.
        (path / "fixture").mkdir()
        runtime, cid, args, capture = original_fixture(path / "fixture")
        attrs = capture["attributes"]["adaptive_setup"]
        attrs["plan"] = _legacy(attrs["plan"])
        return runtime, cid, args, capture

    monkeypatch.setattr(test_oneil_shadow_runner, "fixture", v1_fixture)
    kwargs, capture, _, _ = runner_setup(tmp_path)
    runner = ShadowRunner(**kwargs)
    result = runner.once()
    expected = _hash(["oneil-adaptive-v1", capture["position_id"], "initial-policy-50-v1"])
    row = result["rows"][0]
    assert row["campaign_id"] == expected and row["status"] == "RECORDED"
    assert row["decision"]["action"] == "ADD" and Decimal(row["decision"]["target_allocation"]) == Decimal(".5")
    assert runner.runtime.snapshot(expected)["state"]["plan"]["policy_version"] == V1_VERSION
    with runner.runtime.ledger._transaction() as db:
        cohorts = sorted(json.loads(r[0])["cohort"] for r in db.execute("SELECT data FROM books"))
    assert cohorts and all(c.startswith("oneil-adaptive-v1:") for c in cohorts)

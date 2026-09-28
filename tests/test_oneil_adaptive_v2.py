"""oneil-adaptive-v2 (B3) SHADOW rules; deterministic fixtures, not performance evidence."""
from copy import deepcopy
from decimal import Decimal

import pytest

from prism_core.oneil_adaptive_policy import (
    V1_VERSION, VERSION, _hash, create_plan, evaluate_target, initial_sizing,
)
from prism_core.oneil_auto_review import evaluate_auto_review
from prism_core.oneil_auto_review_output import build_review_bundle
from prism_core.oneil_config import ConfigurationError, defaults, implementation_hash, validate
from prism_core.oneil_input_bridge import assemble_evidence
from prism_core.oneil_intraday_inputs import build_intraday_inputs
from prism_core.oneil_runtime import OneilRuntime
from prism_core.scenario_shadow_policy import create_plan as create_original_plan
from prism_core.strategy_ledger import LedgerError, StrategyLedger, _time as ledger_time
from test_oneil_adaptive_policy import evidence as v1_evidence, plan as v1_plan
from test_oneil_auto_review import AS_OF, valid_snapshot
from test_oneil_current_capture import arguments
from test_oneil_input_bridge import inputs
from test_oneil_intraday_inputs import fixture as intraday_fixture
from test_oneil_setup_inputs import build, review


def setup(atr="4.666667", pivot="100"):
    return {"proper_base": "VERIFIED", "pivot": pivot, "as_of": "2026-09-25T13:29:00Z",
            "source_ref": "base-research", "price_basis_ref": "unadjusted-v1",
            "fundamental_leader": True, "fundamental_source_ref": "leader-research",
            "fundamental_as_of": "2026-09-25T13:29:00Z", "atr14": atr,
            "atr14_source_ref": "daily-prices", "atr14_as_of": "2026-09-25T13:29:00Z",
            "atr14_last_trade_date": "2026-09-24"}


def plan(atr="4.666667", entry="100", stop="90", frozen_setup=None):
    return create_plan(symbol="TEST", entry_reference=entry, initial_stop=stop, entry_eligible=True,
                       source_decision_ref="decision-test", created_at="2026-09-25T13:30:00Z",
                       setup=frozen_setup or setup(atr))


def trend(closes=None, as_of="2026-09-24T20:00:00+00:00"):
    return {"basis": "COMPLETED_DAILY_CLOSE_SMA20", "as_of": as_of,
            "trade_dates": list(v1_evidence()["volume"]["expected_prior_trade_dates"]),
            "closes": closes or [str(80 + i) for i in range(20)],
            "calendar_ref": "caller-calendar", "source_ref": "daily-closes"}


def evidence(price="104", with_trend=True):
    facts = v1_evidence(price)
    facts["contract_version"] = "oneil-adaptive-evidence-v2"
    if with_trend:
        facts["trend"] = trend()
    return facts


INITIAL = dict(cumulative_allocation=0, remaining_allocation=0, normalized_units=0,
               remaining_entry_cost=0, current_stop="90")


def run(frozen, facts, **overrides):
    args = {"now": "2026-09-25T13:41:00Z", "cumulative_allocation": ".5",
            "remaining_allocation": ".5", "normalized_units": str(Decimal(".5") / Decimal(frozen["entry_reference"])),
            "remaining_entry_cost": ".0005", "current_stop": frozen["entry_reference"]}
    args.update(overrides)
    return evaluate_target(frozen, facts, **args)


# --- Plan: ATR14 volatility-scaled initial size, frozen and hashed ----------

def test_plan_freezes_atr_sizing_version_and_14_day_expiry():
    frozen = plan()
    assert frozen["policy_version"] == VERSION == "oneil-adaptive-v2"
    assert frozen["setup"]["atr14"] == "4.666667"
    assert frozen["stop_proxy"] == "0.070000" and frozen["initial_nominal"] == "0.5000"
    assert frozen["expires_at"] == "2026-10-09T13:30:00+00:00"
    tampered = deepcopy(frozen)
    tampered["initial_nominal"] = "0.8000"
    tampered["plan_hash"] = _hash({k: v for k, v in tampered.items() if k != "plan_hash"})
    with pytest.raises(ValueError, match="noncanonical"):
        run(tampered, evidence())


@pytest.mark.parametrize("atr,proxy,initial", [
    ("1", "0.040000", "0.8000"),        # proxy floor .04 -> .875 clipped to the .80 cap
    ("4.666667", "0.070000", "0.5000"),
    ("6", "0.090000", "0.3888"),
    ("20", "0.100000", "0.3500"),       # proxy cap .10 -> .35 (the .30 floor cannot bind)
])
def test_initial_size_formula_and_clip_bounds(atr, proxy, initial):
    assert initial_sizing("100", atr) == (Decimal(proxy), Decimal(initial))
    frozen = plan(atr)
    assert (frozen["stop_proxy"], frozen["initial_nominal"]) == (proxy, initial)
    # At 101 the unchanged risk clip (loss at the 90 stop <= 10%) does not bind.
    first = run(frozen, evidence("101"), **INITIAL)
    assert first["action"] == "ADD" and Decimal(first["target_allocation"]) == Decimal(initial)
    assert all(Decimal(".30") <= initial_sizing("100", a)[1] <= Decimal(".80")
               for a in ("0.01", "3", "7", "9", "15", "1000"))


@pytest.mark.parametrize("change", [
    {"atr14": "0"}, {"atr14": "NaN"}, {"atr14": "-1"}, {"atr14": True}, {"atr14": None},
    {"atr14_source_ref": ""}, {"atr14_as_of": "2026-09-25T13:31:00Z"},
    {"atr14_last_trade_date": "2026-09-25"}, {"atr14_last_trade_date": 20260924},
    {"atr14_as_of": "2026-09-25T03:00:00Z"},  # NY date 09-24 == last trade date: same-session ATR
])
def test_missing_or_invalid_atr_fails_closed_never_default_size(change):
    with pytest.raises((ValueError, TypeError)):
        plan(frozen_setup=dict(setup(), **change))
    missing = setup()
    missing.pop("atr14")
    with pytest.raises(ValueError, match="strict structured setup"):
        plan(frozen_setup=missing)


# --- Rule 1: volume never gates --------------------------------------------

@pytest.mark.parametrize("mutation", ["removed", "zero", "below_ratio", "malformed"])
def test_volume_never_blocks_initial_or_adds(mutation):
    facts = evidence()
    if mutation == "removed":
        facts.pop("volume")
    elif mutation == "zero":
        facts["volume"]["cumulative_volume"] = 0
        for sample in facts["volume"]["samples"]:
            sample["cumulative_volume"] = 0
    elif mutation == "below_ratio":
        facts["volume"]["cumulative_volume"] = 149
    else:
        facts["volume"]["samples"] = "not-a-list"
    first = run(plan(), facts, **INITIAL)
    assert first["action"] == "ADD" and Decimal(first["target_allocation"]) == Decimal(".5")
    add = run(plan(), facts)
    assert add["action"] == "ADD" and Decimal(add["target_allocation"]) == 1
    # The same evidence still blocks a frozen v1 plan.
    v1 = deepcopy(facts)
    v1["contract_version"] = "oneil-adaptive-evidence-v1"
    assert evaluate_target(v1_plan(), v1, now="2026-09-25T13:41:00Z", cumulative_allocation=".1",
                           remaining_allocation=".1", normalized_units=".001",
                           remaining_entry_cost=".0001", current_stop="100")["action"] == "WAIT"


# --- Rule 3: ladder relative to entry_reference, capped at +10% -------------

@pytest.mark.parametrize("price,reason,target", [
    ("105", "TARGET_ALREADY_REACHED", ".5"),   # v1 would add at pivot*1.02=102
    ("106.08", "QUALIFIED", ".8"),             # entry*1.02
    ("108.16", "QUALIFIED", "1"),              # entry*1.04
    ("114.4", "QUALIFIED", "1"),               # entry*1.10 is still allowed
    ("114.41", "OUTSIDE_ADD_BAND", ".5"),
])
def test_add_ladder_relative_to_entry_with_ten_percent_cap(price, reason, target):
    # pivot 100, entry 104: pivot-relative and entry-relative ladders differ.
    frozen = plan(atr="4.853334", entry="104", stop="94")
    assert frozen["initial_nominal"] == "0.5000"
    out = run(frozen, evidence(price))
    assert out["reason"] == reason
    assert Decimal(out["target_allocation"]) == Decimal(target)


def test_ladder_needs_quote_and_bar_close_persistence_and_profit():
    frozen = plan(atr="4.853334", entry="104", stop="94")
    facts = evidence("106.1")
    facts["quote"]["price"] = "105"  # bar reached +2%, quote did not
    assert run(frozen, facts)["reason"] == "TARGET_ALREADY_REACHED"
    facts = evidence("106.1")
    facts["bars"][0]["close"] = "99"  # not two completed bars above the pivot
    assert run(frozen, facts)["reason"] == "TARGET_ALREADY_REACHED"
    underwater = run(frozen, evidence("106.1"), normalized_units=".004")
    assert underwater["reason"] == "NOT_PROFITABLE"
    # One step per bar: a repeated bar cannot add again.
    first = run(frozen, evidence("108.2"))
    assert first["action"] == "ADD"
    assert run(frozen, evidence("108.2"), last_add_bar_end=first["bar_end"])["action"] == "WAIT"


def test_initial_entry_keeps_pivot_buy_band_and_gates():
    frozen = plan()
    assert run(frozen, evidence("105.5"), **INITIAL)["reason"] == "OUTSIDE_BUY_BAND"
    for key, value in (("admission", False), ("market_pulse", "UNDER_PRESSURE"), ("regime", "sideways")):
        facts = evidence()
        facts["gates"][key] = value
        assert run(frozen, facts, **INITIAL)["reason"] == "ADD_GATE_NOT_MET"
        assert run(frozen, facts)["reason"] == "ADD_GATE_NOT_MET"


def test_initial_at_or_above_eight_only_gets_full_step():
    frozen = plan(atr="1")
    assert frozen["initial_nominal"] == "0.8000"
    state = dict(cumulative_allocation=".8", remaining_allocation=".8",
                 normalized_units=".008", remaining_entry_cost=".0008")
    assert run(frozen, evidence("103"), **state)["reason"] == "TARGET_ALREADY_REACHED"
    full = run(frozen, evidence("104"), **state)
    assert full["action"] == "ADD" and Decimal(full["target_allocation"]) == 1


def test_risk_clip_and_protective_stop_precedence_unchanged():
    frozen = plan(atr="1")
    clipped = run(frozen, evidence("104"), **dict(INITIAL, current_stop="90"))
    assert clipped["risk_clipped"] and Decimal(clipped["target_allocation"]) < Decimal(".8")
    loss = Decimal(clipped["target_allocation"]) * (1 + Decimal(".001") - Decimal(90) / 104 * Decimal(".999"))
    assert loss <= Decimal(frozen["risk_limit"])
    protective = run(frozen, evidence("99", with_trend=False), current_stop="100")
    assert protective["action"] == "PROTECTIVE_EXIT_REQUIRED"


# --- Rule 4: completed-daily MA20 trend for adds, not the initial entry -----

def test_missing_trend_blocks_adds_but_not_initial_entry():
    facts = evidence(with_trend=False)
    first = run(plan(), facts, **INITIAL)
    assert first["action"] == "ADD"
    add = run(plan(), facts)
    assert (add["action"], add["reason"], add["evidence_status"]) == (
        "WAIT", "MISSING_TREND_EVIDENCE", "MISSING")
    facts["trend"] = None
    assert run(plan(), facts)["reason"] == "MISSING_TREND_EVIDENCE"


def test_trend_below_or_equal_to_sma20_blocks_adds():
    facts = evidence()
    facts["trend"] = trend(closes=[str(120 - i) for i in range(20)])
    out = run(plan(), facts)
    assert (out["reason"], out["evidence_status"]) == ("TREND_NOT_CONFIRMED", "CONDITION_NOT_MET")
    facts["trend"] = trend(closes=["100"] * 20)  # equal is not above
    assert run(plan(), facts)["reason"] == "TREND_NOT_CONFIRMED"
    assert run(plan(), facts, **INITIAL)["action"] == "ADD"


@pytest.mark.parametrize("mutate", [
    lambda t: t["trade_dates"].pop(),
    lambda t: t["closes"].pop(),
    lambda t: t["trade_dates"].__setitem__(-1, "2026-09-25"),          # not completed
    lambda t: t.update(as_of="2026-09-25T20:00:00Z"),                  # future close
    lambda t: t.update(as_of="2026-09-17T20:00:00Z"),                  # wrong/stale session
    lambda t: t.update(calendar_ref="other-calendar"),
    lambda t: t.update(basis="INTRADAY_PROXY"),
    lambda t: t.update(source_ref=""),
    lambda t: t["closes"].__setitem__(0, "NaN"),
])
def test_invalid_trend_is_rejected_not_assumed_true(mutate):
    facts = evidence()
    mutate(facts["trend"])
    out = run(plan(), facts)
    assert out["action"] == "WAIT" and out["reason"] in {"ADD_EVIDENCE_REJECTED", "MISSING_ADD_EVIDENCE"}


# --- Rule 5: expiry ----------------------------------------------------------

def _at(facts, now):
    facts["quote"]["observed_at"] = now
    facts["gates"]["observed_at"] = now
    return facts


def test_expiry_is_14_calendar_days_for_v2_and_5_for_v1():
    frozen = plan()
    active = run(frozen, _at(evidence(), "2026-10-09T13:29:00Z"), now="2026-10-09T13:29:30Z", **INITIAL)
    assert active["reason"] != "PLAN_NOT_ACTIVE"
    expired = run(frozen, _at(evidence(), "2026-10-09T13:30:00Z"), now="2026-10-09T13:30:30Z", **INITIAL)
    assert expired["reason"] == "PLAN_NOT_ACTIVE"
    old = _at(v1_evidence(), "2026-09-30T13:30:00Z")
    assert evaluate_target(v1_plan(), old, now="2026-09-30T13:30:30Z", **INITIAL)["reason"] == "PLAN_NOT_ACTIVE"


# --- v1 stays readable and unchanged ----------------------------------------

def test_v1_plan_is_byte_identical_and_keeps_v1_rules():
    frozen = v1_plan()
    # Same hash as origin/main produced before v2 existed.
    assert frozen["plan_hash"] == "84b8f5dcacb29bf6bfb02e1a02055beab0c0e1691af5f12b584e9391dcbfac7f"
    assert frozen["policy_version"] == V1_VERSION and "initial_nominal" not in frozen
    facts = v1_evidence()
    facts["volume"]["cumulative_volume"] = 149
    assert evaluate_target(frozen, facts, now="2026-09-25T13:41:00Z", cumulative_allocation=".1",
                           remaining_allocation=".1", normalized_units=".001",
                           remaining_entry_cost=".0001", current_stop="100")["reason"] == "VOLUME_NOT_CONFIRMED"
    # Evidence contracts cannot be swapped between versions.
    assert run(frozen, evidence(), **INITIAL)["reason"] == "INVALID_QUOTE_OR_IDENTITY"
    assert run(plan(), v1_evidence(), **INITIAL)["reason"] == "INVALID_QUOTE_OR_IDENTITY"


# --- Setup builder freezes point-in-time ATR ---------------------------------

def test_setup_builder_supplies_frozen_atr():
    out = build()
    assert out["status"] == "OK"
    assert {k: out["setup"][k] for k in ("atr14", "atr14_source_ref", "atr14_as_of", "atr14_last_trade_date")} == {
        "atr14": "4.666667", "atr14_source_ref": "daily-prices",
        "atr14_as_of": "2026-09-25T13:29:00+00:00", "atr14_last_trade_date": "2026-09-24"}


@pytest.mark.parametrize("change,status,reason", [
    ("absent", "MISSING", "ATR_EVIDENCE_MISSING"),
    ("status", "MISSING", "ATR_EVIDENCE_MISSING"),
    ("value", "INVALID", "ATR_VALUE_MISMATCH"),
    ("same_day", "MISSING", "ATR_NOT_POINT_IN_TIME"),
    ("stale", "MISSING", "ATR_NOT_POINT_IN_TIME"),
    ("future", "INVALID", "FUTURE_CLAIM"),
])
def test_setup_builder_fails_closed_on_atr(change, status, reason):
    record = review()
    volatility = record["volatility"]
    if change == "absent":
        record.pop("volatility")
    elif change == "status":
        volatility["status"] = "MISSING"
    elif change == "value":
        volatility["atr14"] = "4.7"
    elif change == "same_day":
        volatility["last_trade_date"] = "2026-09-25"
    elif change == "stale":
        volatility.update(data_as_of="2026-09-24T13:29:00Z", last_trade_date="2026-09-23")
    else:
        volatility["data_as_of"] = "2026-09-25T13:29:30Z"
    out = build(record)
    assert (out["status"], out["reason_codes"], out["setup"]) == (status, [reason], None)


def test_auto_review_atr14_uses_only_sessions_before_review_date():
    result = evaluate_auto_review(valid_snapshot(), as_of=AS_OF)
    volatility = result["volatility"]
    assert volatility["status"] == "VALIDATED_RULE_OUTPUT"
    assert volatility["atr14"] == "5.000000" and volatility["last_trade_date"] == "2026-09-25"
    # Intraday on 09-25: today's bar is excluded and cannot change ATR.
    snapshot = valid_snapshot()
    for key in ("prices", "benchmark", "financials"):
        snapshot[key]["available_at"] = "2026-09-25T14:00:00Z"
    today = next(bar for bar in snapshot["prices"]["bars"] if bar["date"] == "2026-09-25")
    before = evaluate_auto_review(snapshot, as_of="2026-09-25T15:00:00Z")["volatility"]
    today.update(high=500, low=1)
    after = evaluate_auto_review(snapshot, as_of="2026-09-25T15:00:00Z")["volatility"]
    assert before == after and before["last_trade_date"] == "2026-09-24"
    snapshot["prices"]["bars"] = [b for b in snapshot["prices"]["bars"] if b["date"] != "2026-09-22"]
    missing = evaluate_auto_review(snapshot, as_of="2026-09-25T15:00:00Z")["volatility"]
    assert missing["status"] == "MISSING" and missing["atr14"] is None


def test_review_bundle_freezes_atr_into_hashed_plan():
    bundle = build_review_bundle(valid_snapshot(), reviewed_at=AS_OF)
    frozen_setup = bundle["setup_input"]["setup"]
    assert bundle["setup_input"]["status"] == "OK"
    assert frozen_setup["atr14"] == "5.000000" and frozen_setup["atr14_source_ref"] == "test-prices"
    assert "ATR14 USD: 5.000000" in bundle["report_text"]
    frozen = create_plan(symbol="TEST", entry_reference=frozen_setup["pivot"], initial_stop="95",
                         source_decision_ref="decision-fixture", created_at=AS_OF,
                         setup=frozen_setup, entry_eligible=True)
    assert frozen["setup"]["atr14"] == "5.000000" and frozen["initial_nominal"] == str(
        initial_sizing(frozen_setup["pivot"], "5.000000")[1])


# --- Intraday builder produces the trend block; bridge carries it for v2 ----

def test_intraday_builder_trend_from_completed_sessions_only():
    result = build_intraday_inputs(**intraday_fixture())
    assert result["status"] == "OK"
    block = result["trend"]
    assert block["trade_dates"] == result["volume"]["expected_prior_trade_dates"]
    assert block["closes"] == [str(81 + i) for i in range(20)]
    assert block["as_of"] == "2026-09-24T20:00:00+00:00" and block["calendar_ref"] == "exchange-fixture"
    for change in ("drop", "duplicate"):
        data = intraday_fixture()
        final_bars = [b for b in data["bars"] if b["provider_timestamp"].endswith("15:55:00-04:00")]
        if change == "drop":
            data["bars"].remove(final_bars[3])
        else:
            data["bars"].append(dict(final_bars[3], close=500))
        partial = build_intraday_inputs(**data)
        # Missing trend never blocks the matched-prefix input the first entry uses.
        assert partial["status"] == "OK" and partial["trend"] is None
        assert partial["input_hash"] != result["input_hash"]


def test_bridge_carries_trend_for_v2_only():
    args = inputs()
    evidence_v2 = assemble_evidence(**args)["evidence"]
    assert evidence_v2["contract_version"] == "oneil-adaptive-evidence-v2" and evidence_v2["trend"]
    add = evaluate_target(args["plan"], evidence_v2, now=args["now"], cumulative_allocation=".5",
                          remaining_allocation=".5", normalized_units=".005",
                          remaining_entry_cost=".0005", current_stop="100")
    assert add["action"] == "ADD"
    args["intraday_input"]["trend"] = None
    no_trend = assemble_evidence(**args)["evidence"]
    assert evaluate_target(args["plan"], no_trend, now=args["now"], cumulative_allocation=".5",
                           remaining_allocation=".5", normalized_units=".005",
                           remaining_entry_cost=".0005", current_stop="100")["reason"] == "MISSING_TREND_EVIDENCE"
    args = inputs()
    legacy_setup = {k: v for k, v in args["plan"]["setup"].items() if not k.startswith("atr14")}
    args["plan"] = create_plan(policy_version=V1_VERSION, symbol="TEST", entry_reference="100",
                               initial_stop="95", source_decision_ref="d1", entry_eligible=True,
                               created_at="2026-09-25T13:30:00Z", setup=legacy_setup)
    args["setup_input"] = dict(status="OK", setup=legacy_setup)
    legacy = assemble_evidence(**args)["evidence"]
    assert legacy["contract_version"] == "oneil-adaptive-evidence-v1" and "trend" not in legacy


# --- Runtime/ledger: v2 SHADOW exactly like v1, v1 campaigns untouched -------

def _capture(args, position_id):
    p = args["plan"]
    original = create_original_plan(entry_price=p["entry_reference"], initial_stop=p["initial_stop"],
                                    entry_at=p["created_at"], source_decision_ref=p["source_decision_ref"],
                                    entry_eligible=True)
    return dict(market="US", ticker=p["symbol"], position_id=position_id,
                decision_id=p["source_decision_ref"], event_id="capture:" + position_id,
                event_time=p["created_at"], attributes=dict(
                    capture_schema_version=1, phase="POST_STRATEGY_COMMIT_PRE_BROKER",
                    confirmed_fill=False, trading_impact="none", plan=original,
                    adaptive_setup=dict(status="OK", plan=p)))


def _v1_arguments():
    args = arguments()
    legacy_setup = {k: v for k, v in args["plan"]["setup"].items() if not k.startswith("atr14")}
    p = args["plan"]
    args["plan"] = create_plan(policy_version=V1_VERSION, symbol=p["symbol"],
                               entry_reference=p["entry_reference"], initial_stop=p["initial_stop"],
                               source_decision_ref=p["source_decision_ref"], created_at=p["created_at"],
                               setup=legacy_setup, entry_eligible=True)
    args["setup_input"] = dict(status="OK", setup=legacy_setup)
    return args


def _cohorts(runtime):
    import json
    with runtime.ledger._transaction() as db:
        return sorted(json.loads(row[0])["cohort"] for row in db.execute("SELECT data FROM books"))


def test_runtime_v2_and_v1_campaigns_coexist_with_their_own_owner(tmp_path):
    from prism_core.oneil_current_capture import capture_current_record
    runtime = OneilRuntime(tmp_path / "runtime.sqlite")
    v1_args = _v1_arguments()
    old = runtime.open_capture(_capture(v1_args, v1_args["position_id"]))
    # v1 identity is exactly the pre-v2 campaign id and cohort.
    assert old["campaign_id"] == _hash(["oneil-adaptive-v1", v1_args["position_id"], "initial-policy-50-v1"])
    assert old["campaign_id"] == runtime.campaign_id_for_position(v1_args["position_id"], V1_VERSION)
    new = runtime.open_capture(_capture(arguments(), "holding:2"))
    assert new["campaign_id"] == runtime.campaign_id_for_position("holding:2")
    assert [c.split(":")[0] for c in _cohorts(runtime)] == ["oneil-adaptive-v1"] * 2 + ["oneil-adaptive-v2"] * 2
    first = runtime.advance(old["campaign_id"], capture_current_record(**v1_args), expected_revision=0)
    assert first["decision"]["action"] == "ADD" and first["arms"]["adaptive"]["target_pct"] == "50.0000"
    restarted = OneilRuntime(runtime.ledger.path)
    assert restarted.snapshot(new["campaign_id"])["state"]["plan"]["policy_version"] == VERSION
    with pytest.raises(LedgerError):
        runtime.campaign_id_for_position("holding:3", "oneil-adaptive-v0")


def test_runtime_v2_initial_entry_uses_frozen_size_and_v2_owner(tmp_path):
    from prism_core.oneil_current_capture import capture_current_record
    runtime = OneilRuntime(tmp_path / "runtime.sqlite")
    args = arguments()
    opened = runtime.open_capture(_capture(args, args["position_id"]))
    result = runtime.advance(opened["campaign_id"], capture_current_record(**args), expected_revision=0)
    assert result["decision"]["action"] == "ADD"
    assert result["arms"]["adaptive"]["target_pct"] == "50.0000"
    assert all(c.startswith("oneil-adaptive-v2:initial-policy-50-v1:") for c in _cohorts(runtime))
    with runtime.ledger._transaction() as db:
        legs = [row[0] for row in db.execute("SELECT data FROM legs")]
    assert legs and all('"policy_version": "oneil-adaptive-v1"' not in leg for leg in legs)


def _payload(book, campaign, target="10"):
    return dict(kind="target", book_id=book, campaign_id=campaign, symbol="TEST", target_pct=target,
                price="100", occurred_at=ledger_time("2026-09-25T13:30:00Z"),
                policy_version=VERSION, reason="adaptive", regime="paired_shadow",
                source_hash=None, fee_rate="0.001", slippage_rate="0")


def test_ledger_guards_v2_books_exactly_like_v1(tmp_path):
    ledger = StrategyLedger(tmp_path / "ledger.sqlite")
    for version in (V1_VERSION, VERSION):
        book = version + ":book"
        ledger.create_book(book, "US", 1, cohort=version + ":initial-policy-50-v1:cid:adaptive", mode="SHADOW")
        with pytest.raises(LedgerError, match="rejects direct target"):
            ledger.apply_target("direct:" + version, book, version + ":c", "TEST", 10, 100,
                                "2026-09-25T13:30:00Z", policy_version=version)
        other = VERSION if version == V1_VERSION else V1_VERSION
        with pytest.raises(LedgerError, match="rejects direct target"):
            with ledger._transaction() as db:
                ledger._apply_target_in_transaction(db, "cross:" + version, _payload(book, version + ":c"), owner=other)
        with ledger._transaction() as db:
            ledger._apply_target_in_transaction(db, "owned:" + version, _payload(book, version + ":c"), owner=version)
        with pytest.raises(LedgerError, match="rejects direct mark"):
            ledger.mark("mark:" + version, version + ":c", 101, "2026-09-25T13:31:00Z")
        with pytest.raises(LedgerError, match="rejects direct sell"):
            ledger.sell("sell:" + version, version + ":c", 101, "2026-09-25T13:31:00Z")
        # A non-SHADOW book under an adaptive cohort is refused even to its owner.
        validation = version + ":validation"
        ledger.create_book(validation, "US", 1, cohort=version + ":x:adaptive", mode="VALIDATION")
        with pytest.raises(LedgerError, match="rejects direct target"):
            with ledger._transaction() as db:
                ledger._apply_target_in_transaction(db, "val:" + version, _payload(validation, version + ":v"),
                                                    owner=version)


# --- Config: SHADOW keeps running from a v1 file; LIVE needs explicit v2 -----

def _config(**extra):
    return {**dict(mode="SHADOW", accounts=["primary"], capture_since="2026-09-27T00:00:00Z"), **extra}


def _approval(policy):
    approval = dict(policy=policy, initial_arm="INITIAL_POLICY_50", scope="NEW_CAMPAIGNS_ONLY",
                    accounts=["primary"], approved_by="test-operator",
                    approved_at="2026-09-27T00:00:00Z", expires_at="2026-09-28T00:00:00Z",
                    max_unit_budget_usd="1000", implementation_hash=implementation_hash())
    approval["approval_hash"] = _hash(approval)
    return approval


def test_config_policy_v2_live_requires_v2_config_and_approval():
    assert defaults()["policy"] == VERSION
    assert validate(_config(policy=V1_VERSION))["mode"] == "SHADOW"
    assert validate(_config(policy=V1_VERSION, mode="OFF"))["mode"] == "OFF"
    now = "2026-09-27T10:00:00Z"
    with pytest.raises(ConfigurationError):
        validate(_config(mode="LIVE", policy=V1_VERSION, live_approval=_approval(V1_VERSION)), now=now)
    with pytest.raises(ConfigurationError):
        validate(_config(mode="LIVE", policy=V1_VERSION, live_approval=_approval(VERSION)), now=now)
    with pytest.raises(ConfigurationError):
        validate(_config(mode="LIVE", live_approval=_approval(V1_VERSION)), now=now)
    with pytest.raises(ConfigurationError):
        validate(_config(mode="LIVE", live_approval=None), now=now)
    assert validate(_config(mode="LIVE", live_approval=_approval(VERSION)), now=now)["mode"] == "LIVE"
    with pytest.raises(ConfigurationError):
        validate(_config(policy="oneil-adaptive-v3"))


# --- Submission-time quote validator mirrors the v2 band -------------------

@pytest.mark.parametrize("confirmed,target,quote,error", [
    (48, "1", 107, None),                                  # above pivot*1.05, within entry*1.10
    (48, "1", 110.5, "RESERVED_PRICE_OUTSIDE_POLICY"),     # above entry*1.10
    (48, "1", 103, "TARGET_PRICE_CONFIRMATION_LOST"),      # below entry*1.04
    (0, ".5", 106, "RESERVED_PRICE_OUTSIDE_POLICY"),       # first entry keeps the pivot band
    (0, ".5", 100.5, None),
])
def test_dispatch_validator_uses_v2_band(tmp_path, monkeypatch, confirmed, target, quote, error):
    from dataclasses import replace
    from prism_core.oneil_dispatcher import _clock, _validator
    from test_oneil_execution import reserve, start
    core, cid, args, account = start(tmp_path, "LIVE")
    reserved = reserve(core, cid, args, account)
    reserved = dict(reserved, intent=replace(reserved["intent"], limit_price="111"))
    snapshot = core.snapshot(cid)
    assert snapshot["plan"]["policy_version"] == VERSION
    snapshot["confirmed_quantity"] = confirmed
    snapshot["orders"][0]["target_allocation"] = target
    monkeypatch.setattr(core, "snapshot", lambda _: snapshot)
    validate = _validator(core, reserved, _clock(args["now"]), lambda: True)
    if error is None:
        validate(quote)
    else:
        with pytest.raises(ValueError, match=error):
            validate(quote)

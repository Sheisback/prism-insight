"""Deterministic caller-attested fixtures; not historical performance evidence."""
from copy import deepcopy
from decimal import Decimal

import pytest

from prism_core.oneil_adaptive_policy import create_plan, evaluate_target


def plan(fee_bps=10):
    return create_plan(symbol="TEST", entry_reference="100", initial_stop="90",
                       entry_eligible=True,
                       source_decision_ref="decision-test", created_at="2026-09-25T13:30:00Z",
                       setup={"proper_base": "VERIFIED", "pivot": "100",
                              "as_of": "2026-09-25T13:29:00Z", "source_ref": "base-research",
                              "price_basis_ref": "unadjusted-v1", "fundamental_leader": True,
                              "fundamental_source_ref": "leader-research",
                              "fundamental_as_of": "2026-09-25T13:29:00Z"}, fee_bps=fee_bps)


def evidence(price="104"):
    dates = ["2026-08-27", "2026-08-28", "2026-08-31"] + [
        f"2026-09-{day:02}" for day in (1, 2, 3, 4, 8, 9, 10, 11, 14, 15, 16, 17, 18, 21, 22, 23, 24)]
    return {"contract_version": "oneil-adaptive-evidence-v1", "symbol": "TEST",
            "price_basis_ref": "unadjusted-v1", "source": "mechanical", "source_ref": "research",
            "market_window": {"trade_date": "2026-09-25", "verified": True,
                              "open_at": "2026-09-25T13:30:00Z", "close_at": "2026-09-25T20:00:00Z",
                              "source_ref": "caller-calendar"},
            "quote": {"price": price, "observed_at": "2026-09-25T13:40:30Z", "source_ref": "quote"},
            "bars": [{"start_at": "2026-09-25T13:30:00Z", "end_at": "2026-09-25T13:35:00Z",
                      "close": "102", "complete": True, "regular": True, "source_ref": "bars"},
                     {"start_at": "2026-09-25T13:35:00Z", "end_at": "2026-09-25T13:40:00Z",
                      "close": price, "complete": True, "regular": True, "source_ref": "bars"}],
            "volume": {"as_of": "2026-09-25T13:40:00Z", "elapsed_minutes": 10,
                       "basis": "MATCHED_REGULAR_CUMULATIVE",
                       "complete": True, "regular": True, "source_ref": "volume",
                       "cumulative_volume": 150, "calendar_ref": "caller-calendar",
                       "expected_prior_trade_dates": dates,
                       "samples": [{"trade_date": day, "elapsed_minutes": 10, "cumulative_volume": 100,
                                    "complete": True, "regular": True, "source_ref": "volume"}
                                   for day in dates]},
            "gates": {"observed_at": "2026-09-25T13:40:30Z", "source_ref": "gate",
                      "admission": True, "risk": True, "RR": True, "sector": True, "slot": True,
                      "market_pulse": "UPTREND", "regime": "moderate_bull"}}


def evaluate(facts=None, frozen=None, **overrides):
    args = {"now": "2026-09-25T13:41:00Z", "cumulative_allocation": ".1",
            "remaining_allocation": ".1", "normalized_units": ".001",
            "remaining_entry_cost": ".0001", "current_stop": "100"}
    args.update(overrides)
    return evaluate_target(frozen or plan(), evidence() if facts is None else facts, **args)


@pytest.mark.parametrize("price,target", [("101", ".5"), ("102", ".8"), ("104", "1")])
def test_direct_target(price, target):
    out = evaluate(evidence(price))
    assert out["action"] == "ADD"
    assert Decimal(out["target_allocation"]) == Decimal(target)


def test_initial_capped_at_half():
    out = evaluate(cumulative_allocation=0, remaining_allocation=0, normalized_units=0,
                   remaining_entry_cost=0, current_stop=90)
    assert out["action"] == "ADD"
    assert Decimal(out["target_allocation"]) == Decimal(".5")


def test_initial_entry_can_use_fresh_bar_already_known_at_plan_creation():
    original = plan()
    frozen = create_plan(symbol="TEST", entry_reference="100", initial_stop="90",
                         source_decision_ref="decision-test", created_at="2026-09-25T13:40:45Z",
                         setup=original["setup"], entry_eligible=True)
    initial = evaluate(frozen=frozen, cumulative_allocation=0, remaining_allocation=0,
                       normalized_units=0, remaining_entry_cost=0, current_stop=90)
    assert initial["action"] == "ADD" and Decimal(initial["target_allocation"]) == Decimal(".5")
    # A previously entered scout needs post-plan confirmation, not the old bar.
    assert evaluate(frozen=frozen)["action"] == "WAIT"


@pytest.mark.parametrize("path,value", [
    (("symbol",), "WRONG"), (("contract_version",), "v0"),
    (("price_basis_ref",), "split-adjusted"), (("source_ref",), ""),
    (("quote", "observed_at"), "2026-09-25T13:38:00Z"),
    (("quote", "observed_at"), "2026-09-25T13:42:00Z"),
    (("quote", "price"), "NaN"), (("gates", "admission"), False),
    (("gates", "RR"), False), (("gates", "sector"), False),
    (("gates", "slot"), False), (("gates", "risk"), False),
    (("gates", "market_pulse"), "DOWNTREND"), (("gates", "regime"), "neutral"),
    (("gates", "observed_at"), "2026-09-25T13:38:00Z"),
    (("volume", "cumulative_volume"), 149), (("volume", "complete"), False),
    (("volume", "basis"), "LINEAR_DAILY_PROXY"),
    (("volume", "elapsed_minutes"), 11), (("volume", "calendar_ref"), ""),
    (("volume", "as_of"), "2026-09-25T13:39:00Z"),
    (("market_window", "verified"), False), (("source",), "LLM"),
])
def test_fail_closed(path, value):
    facts = evidence()
    item = facts
    for key in path[:-1]:
        item = item[key]
    item[path[-1]] = value
    assert evaluate(facts)["action"] == "WAIT"


@pytest.mark.parametrize("mutation", ["duplicate", "future", "missing", "elapsed", "incomplete", "zero"])
def test_volume_population(mutation):
    facts = evidence()
    samples = facts["volume"]["samples"]
    if mutation == "duplicate":
        samples[0] = deepcopy(samples[1])
    elif mutation == "future":
        samples[0]["trade_date"] = "2026-09-26"
    elif mutation == "missing":
        samples.pop()
    elif mutation == "elapsed":
        samples[0]["elapsed_minutes"] = 9
    elif mutation == "incomplete":
        samples[0]["complete"] = False
    else:
        for item in samples:
            item["cumulative_volume"] = 0
    assert evaluate(facts)["action"] == "WAIT"


def test_false_breakout_and_gap_and_persistence():
    assert evaluate(evidence("106"))["reason"] == "OUTSIDE_BUY_BAND"
    assert evaluate(evidence("99"), current_stop=90)["reason"] == "OUTSIDE_BUY_BAND"
    facts = evidence()
    facts["bars"][0]["close"] = "99"
    assert evaluate(facts)["nominal_target"] == "0.5"
    facts["bars"][0]["end_at"] = "2026-09-25T13:34:00Z"
    assert evaluate(facts)["action"] == "WAIT"


def test_stop_protection_before_pending_expiry_and_missing_add_gates():
    facts = evidence("89")
    facts.pop("gates")
    assert evaluate(facts, add_permission="PENDING")["action"] == "PROTECTIVE_EXIT_REQUIRED"
    facts["quote"]["observed_at"] = "2026-10-01T13:40:30Z"
    assert evaluate(facts, now="2026-10-01T13:41:00Z")["action"] == "PROTECTIVE_EXIT_REQUIRED"


def test_repeated_bar_pending_and_reduced_or_closed():
    assert evaluate(last_add_bar_end="2026-09-25T13:40:00Z")["action"] == "WAIT"
    assert evaluate(add_permission="PENDING")["action"] == "WAIT"
    assert evaluate(remaining_allocation=".05")["reason"] == "REDUCED_POSITION"
    assert evaluate(normalized_units=0)["reason"] == "STRATEGY_CLOSED"


@pytest.mark.parametrize("fee", [10, 25])
def test_cost_inclusive_risk_clipping(fee):
    frozen = plan(fee)
    out = evaluate(frozen=frozen, current_stop=90, remaining_entry_cost=str(Decimal(".1") * fee / 10000))
    assert out["action"] == "ADD" and out["risk_clipped"]
    delta, price = Decimal(out["delta_allocation"]), Decimal(out["price"])
    rate = Decimal(frozen["fee_rate"])
    loss = Decimal(".1") + Decimal(".1") * rate - Decimal(".001") * 90 * (1-rate)
    loss += delta * (1+rate-90/price*(1-rate))
    assert loss <= Decimal(frozen["risk_limit"])
    assert loss + Decimal(".0001") * (1+rate-90/price*(1-rate)) > Decimal(frozen["risk_limit"])


def test_risk_already_exceeded_no_add():
    assert evaluate(remaining_entry_cost=".2")["action"] == "WAIT"


def test_plan_hash_and_stop_ratcheting():
    frozen = plan()
    frozen["initial_stop"] = "80"
    with pytest.raises(ValueError):
        evaluate(frozen=frozen)
    with pytest.raises(ValueError):
        evaluate(current_stop="89")
    for bad in ("NaN", "Infinity", "-1", True):
        with pytest.raises(ValueError):
            evaluate(cumulative_allocation=bad)


def test_same_candidate_admission_remains_required_and_no_mutation():
    facts, frozen = evidence(), plan()
    original = deepcopy((facts, frozen))
    assert evaluate(facts, frozen)["action"] == "ADD"
    assert (facts, frozen) == original
    facts["gates"]["admission"] = False
    assert evaluate(facts, frozen)["action"] == "WAIT"


def test_missing_volume_is_not_a_negative_volume_signal():
    facts = evidence()
    del facts['volume']
    missing = evaluate(facts)
    assert missing['action'] == 'WAIT' and missing['evidence_status'] == 'MISSING'
    facts = evidence()
    facts['volume']['cumulative_volume'] = 100
    weak = evaluate(facts)
    assert weak['action'] == 'WAIT' and weak['evidence_status'] == 'CONDITION_NOT_MET'


def test_unknown_gate_is_not_confirmed_rejection():
    facts = evidence()
    facts['gates']['risk'] = None
    assert evaluate(facts)['evidence_status'] == 'MISSING'
    facts['gates']['risk'] = False
    assert evaluate(facts)['evidence_status'] == 'CONDITION_NOT_MET'


def test_extended_winner_is_not_sold_by_the_buy_band():
    out = evaluate(evidence('110'), cumulative_allocation='1', remaining_allocation='1',
                   normalized_units='.01', remaining_entry_cost='.001', current_stop='100')
    assert out['action'] == 'WAIT'  # buy-band cap must not become a profit-taking rule
    assert out['target_allocation'] == '1'


@pytest.mark.parametrize("key,value", [("proper_base", "20_DAY_HIGH_PROXY"),
                                     ("fundamental_leader", False),
                                     ("as_of", "2026-09-25T13:31:00Z"),
                                     ("fundamental_source_ref", ""), ("pivot", "NaN")])
def test_setup_cannot_be_inferred_from_admission(key, value):
    setup = plan()["setup"]
    setup[key] = value
    with pytest.raises(ValueError):
        create_plan(symbol="TEST", entry_reference=100, initial_stop=90,
                    entry_eligible=True,
                    source_decision_ref="decision", created_at="2026-09-25T13:30:00Z", setup=setup)


def test_quote_before_bar_and_future_bar_and_expiry():
    facts = evidence()
    facts["quote"]["observed_at"] = "2026-09-25T13:39:30Z"
    assert evaluate(facts)["action"] == "WAIT"
    assert evaluate(now="2026-09-25T13:39:00Z")["action"] == "WAIT"
    facts = evidence()
    facts["quote"]["observed_at"] = "2026-09-25T13:51:00Z"
    assert evaluate(facts, now="2026-09-25T13:51:00Z")["action"] == "WAIT"
    facts["quote"]["observed_at"] = "2026-10-01T13:40:30Z"
    assert evaluate(facts, now="2026-10-01T13:41:00Z")["reason"] == "PLAN_NOT_ACTIVE"


def test_initial_quote_below_stop_cannot_allocate():
    out = evaluate(cumulative_allocation=0, remaining_allocation=0, normalized_units=0,
                   remaining_entry_cost=0, current_stop=105)
    assert out["reason"] == "STOP_NOT_BELOW_PRICE"


@pytest.mark.parametrize("key,value", [
    ("open_at", "2026-09-25T12:30:00Z"),
    ("close_at", "2026-09-25T20:01:00Z"),
    ("trade_date", "2026-09-26"),
])
def test_us_regular_window_contract(key, value):
    facts = evidence()
    facts["market_window"][key] = value
    assert evaluate(facts)["action"] == "WAIT"


def test_early_close_attestation_accepted_before_close():
    facts = evidence()
    facts["market_window"]["close_at"] = "2026-09-25T17:00:00Z"
    assert evaluate(facts)["action"] == "ADD"


def test_strict_setup_no_raw_llm_and_entry_band():
    kwargs = {"symbol": "TEST", "entry_reference": 100, "initial_stop": 90,
              "entry_eligible": True, "source_decision_ref": "decision",
              "created_at": "2026-09-25T13:30:00Z", "setup": plan()["setup"]}
    kwargs["setup"]["llm_prose"] = "not a structured attestation"
    with pytest.raises(ValueError):
        create_plan(**kwargs)
    kwargs["setup"].pop("llm_prose")
    kwargs["entry_reference"] = 99
    with pytest.raises(ValueError):
        create_plan(**kwargs)
    kwargs["entry_reference"] = 106
    with pytest.raises(ValueError):
        create_plan(**kwargs)
    kwargs["entry_reference"] = 100
    kwargs.pop("entry_eligible")
    with pytest.raises(TypeError):
        create_plan(**kwargs)


def test_unknown_inputs_never_claim_qualification():
    assert evaluate({})["action"] == "WAIT"
    facts = evidence()
    facts.pop("volume")
    assert evaluate(facts)["action"] == "WAIT"
    facts = evidence()
    facts["volume"]["expected_prior_trade_dates"][0] = "2026-08-22"
    facts["volume"]["samples"][0]["trade_date"] = "2026-08-22"
    assert evaluate(facts)["action"] == "WAIT"


def test_cost_stress_reduces_capacity_and_never_changes_stop():
    normal = evaluate(current_stop=90)
    stress = evaluate(frozen=plan(25), current_stop=90, remaining_entry_cost=".00025")
    assert Decimal(stress["target_allocation"]) < Decimal(normal["target_allocation"])
    assert "current_stop" not in normal and "stop" not in normal

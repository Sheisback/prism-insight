from copy import deepcopy
from decimal import Decimal

import pytest

from prism_core.scenario_shadow_policy import create_plan, create_state, evaluate, revise_plan

ENTRY = "2026-09-21T14:00:00Z"
NOW = "2026-09-21T14:10:00Z"


def plan(**kwargs):
    return create_plan(**dict(entry_price=100, initial_stop=90, entry_at=ENTRY,
                             source_decision_ref="decision-ref", entry_eligible=True, **kwargs))


def evidence(price=105):
    return {"source": "mechanical", "quote": {"price": price, "observed_at": NOW},
            "bar": {"open_at": "2026-09-21T14:00:00Z", "close_at": "2026-09-21T14:05:00Z",
                    "observed_at": NOW, "completed": True, "close": price},
            "session": {"open_at": "2026-09-21T13:30:00Z", "close_at": "2026-09-21T20:00:00Z",
                        "verified": True, "source_ref": "NYSE-fixture"},
            "gates": {"risk": True, "regime": True, "sector": True, "slot": True,
                      "observed_at": NOW, "source_ref": "gate-snapshot"}}


def run(p=None, s=None, facts=None, **kwargs):
    p = p or plan()
    return evaluate(p, s or create_state(p), evidence() if facts is None else facts,
                    **dict(now=NOW, cumulative_allocation=".1", remaining_allocation=".1",
                           normalized_units=".001", **kwargs))


@pytest.mark.parametrize("field,value", [("entry_price", None), ("entry_price", "NaN"),
    ("initial_stop", None), ("initial_stop", 0), ("initial_stop", 100),
    ("entry_price", True), ("entry_eligible", 1), ("source_decision_ref", ""),
    ("source", "mechanical"), ("entry_at", "2026-09-21T14:00:00"), ("market", "KR")])
def test_creation_fails_closed(field, value):
    args = dict(entry_price=100, initial_stop=90, entry_at=ENTRY,
                source_decision_ref="ref", entry_eligible=True)
    args[field] = value
    with pytest.raises(ValueError):
        create_plan(**args)


def test_plan_hash_and_fixed_policy():
    p = plan()
    assert p["expires_at"] == "2026-09-26T14:00:00+00:00"
    assert p["stages"][0] == {"target_allocation": "0.3", "min_price": "105.0", "max_price": "110.0"}
    assert "decision-ref" not in str(p)
    tampered = deepcopy(p)
    tampered["cost_rate"] = "0"
    with pytest.raises(ValueError, match="hash"):
        create_state(tampered)


def test_same_policy_for_both_sources_and_inputs_not_mutated():
    f, p = evidence(), plan()
    before = deepcopy(f)
    mechanical = run(p, facts=f)
    f["source"] = "regular"
    regular = run(p, facts=f)
    assert mechanical["action"] == regular["action"] == "ADD"
    assert Decimal(mechanical["delta_allocation"]) == Decimal(".2")
    assert mechanical["state"]["filled_target"] == "0.3"
    assert mechanical["state"]["stop"] == "90"
    assert before == evidence()
    assert create_state(p)["last_bar_end"] is None


@pytest.mark.parametrize("section,key,value", [
    ("quote", "observed_at", "2026-09-21T14:07:59Z"),
    ("quote", "observed_at", "2026-09-21T14:10:01Z"),
    ("quote", "price", "Infinity"), ("quote", "price", 0),
    ("bar", "close_at", "2026-09-21T14:15:00Z"),
    ("bar", "open_at", "2026-09-21T13:55:00Z"),
    ("bar", "completed", False), ("bar", "observed_at", "2026-09-21T14:04:00Z"),
    ("bar", "observed_at", "2026-09-21T14:11:00Z"),
    ("session", "verified", 1), ("session", "source_ref", ""),
    ("session", "close_at", NOW), ("gates", "risk", 1),
    ("gates", "regime", None), ("gates", "sector", False), ("gates", "slot", False),
    ("gates", "observed_at", "2026-09-21T14:07:59Z"), ("gates", "source_ref", "")])
def test_missing_stale_future_evidence_blocks_add(section, key, value):
    facts = evidence()
    facts[section][key] = value
    assert run(facts=facts)["action"] == "WAIT"


@pytest.mark.parametrize("section", ["quote", "bar", "session", "gates"])
def test_missing_evidence_blocks_add(section):
    facts = evidence()
    del facts[section]
    assert run(facts=facts)["action"] == "WAIT"


def test_no_chase_no_averaging_down_and_single_stage():
    assert run(facts=evidence(111))["action"] == "WAIT"
    assert run(facts=evidence(99))["action"] == "WAIT"
    # Book is losing despite price above original entry (e.g corrupt/inconsistent units).
    assert evaluate(plan(), create_state(plan()), evidence(), now=NOW,
                    cumulative_allocation=".1", remaining_allocation=".1",
                    normalized_units=".0009")["reason"] == "NO_AVERAGING_DOWN"
    first = run(facts=evidence(110))
    assert first["target_allocation"] == "0.3"
    repeat = evaluate(plan(), first["state"], evidence(110), now=NOW,
                      cumulative_allocation=".3", remaining_allocation=".3",
                      normalized_units=str(Decimal(".001") + Decimal(".2") / 110))
    assert repeat["reason"] == "BAR_ALREADY_CONSUMED"


def test_trailing_uses_completed_close_not_intrabar_high_and_precedes_add():
    facts = evidence(110)
    facts["bar"]["high"] = 999
    assert run(facts=facts)["state"]["stop"] == "100"
    facts["bar"]["close"] = 120
    result = run(facts=facts)
    assert result["action"] == "EXIT"
    assert result["state"]["stop"] == "110"
    facts["bar"]["completed"] = False
    assert run(facts=facts)["state"]["stop"] == "90"


def test_existing_protection_ignores_invalid_add_inputs_and_cancellation():
    p = plan()
    state = create_state(p)
    state["add_cancelled"] = True
    facts = {"quote": {"price": 89, "observed_at": "2026-09-28T14:00:00Z"}}
    result = evaluate(p, state, facts, now="2026-09-28T14:00:00Z",
                      cumulative_allocation=".1", remaining_allocation=".1", normalized_units=".001")
    assert result["action"] == "EXIT"


def test_expiry_does_not_prevent_trailing():
    p = plan()
    revision = revise_plan(p, create_state(p), source="regular", expected_version=0,
                           now=ENTRY, expires_at=NOW)
    result = run(revision["plan"], revision["state"], evidence(110))
    assert result["reason"] == "ADD_EXPIRED"
    assert result["state"]["stop"] == "100"


def test_negative_thesis_persists_without_bar_but_stale_claim_does_not():
    facts = evidence()
    facts["thesis_negative"] = True
    del facts["bar"]
    assert run(facts=facts)["state"]["add_cancelled"] is True
    facts["gates"]["observed_at"] = ENTRY
    assert run(facts=facts)["state"]["add_cancelled"] is False


def test_reduction_cancels_and_risk_bound_accounts_for_costs():
    p = plan()
    reduced = evaluate(p, create_state(p), evidence(), now=NOW,
                       cumulative_allocation=".1", remaining_allocation=".05", normalized_units=".0005")
    assert reduced["reason"] == "ADD_CANCELLED"
    # Fixed policy loss budget includes costs. Changing it without resealing is rejected.
    assert Decimal(p["max_risk_allocation"]) == Decimal(".1")
    assert Decimal(p["cost_rate"]) == Decimal(".001")
    tiny_r = create_plan(entry_price=100, initial_stop="99.95", entry_at=ENTRY,
                         source_decision_ref="tight-risk", entry_eligible=True)
    result = run(tiny_r, facts=evidence("100.025"))
    assert result["reason"] == "TOTAL_RISK_EXCEEDED"
    with pytest.raises(ValueError, match="initial position"):
        create_plan(entry_price=100, initial_stop="99.99", entry_at=ENTRY,
                    source_decision_ref="too-tight-risk", entry_eligible=True)


@pytest.mark.parametrize("changes", [{"source": "mechanical"}, {"expected_version": 2},
    {"stop": 89}, {"expires_at": "2026-09-27T14:00:00Z"}, {"cancel": "true"}])
def test_revision_authority_and_envelope(changes):
    p = plan()
    args = dict(source="regular", expected_version=0, now=NOW)
    args.update(changes)
    with pytest.raises(ValueError):
        revise_plan(p, create_state(p), **args)


def test_revision_tightening_and_filled_stage_immutable():
    p = plan()
    future = deepcopy(p["stages"])
    future[0]["max_price"] = "108"
    revised = revise_plan(p, create_state(p), source="regular", expected_version=0,
                          now=NOW, stages=future, stop=95, cancel=True)
    assert revised["plan"]["revision"] == 1
    assert revised["state"]["stop"] == "95"
    assert revised["state"]["add_cancelled"] is True
    state = run()["state"]
    with pytest.raises(ValueError, match="filled"):
        revise_plan(p, state, source="regular", expected_version=0, now=NOW, stages=future)
    future[0]["max_price"] = "115"
    with pytest.raises(ValueError, match="tighten"):
        revise_plan(p, create_state(p), source="regular", expected_version=0, now=NOW, stages=future)


def test_out_of_order_and_plan_state_mismatch_rejected():
    p = plan()
    state = create_state(p)
    state["last_evaluated_at"] = "2026-09-21T14:11:00Z"
    with pytest.raises(ValueError, match="order"):
        run(p, state)
    state["plan_hash"] = "other"
    with pytest.raises(ValueError, match="mismatch"):
        run(p, state)


def test_full_campaign_monotonic_stop_and_one_stage_per_new_bar():
    p, state = plan(), create_state(plan())
    deployed, units = Decimal(".1"), Decimal(".001")
    for minute, price, target, stop in ((10, 105, ".3", "90"),
                                       (20, 110, ".6", "100"),
                                       (30, 120, "1", "110")):
        now = f"2026-09-21T14:{minute}:00Z"
        facts = evidence(price)
        facts["quote"]["observed_at"] = now
        facts["gates"]["observed_at"] = now
        facts["bar"].update(open_at=f"2026-09-21T14:{minute-5:02d}:00Z",
                            close_at=now, observed_at=now)
        result = evaluate(p, state, facts, now=now, cumulative_allocation=str(deployed),
                          remaining_allocation=str(deployed), normalized_units=str(units))
        assert result["action"] == "ADD"
        assert Decimal(result["target_allocation"]) == Decimal(target)
        assert result["state"]["stop"] == stop
        units += Decimal(result["delta_allocation"]) / price
        deployed, state = Decimal(target), result["state"]
    facts = evidence(115)
    facts["quote"]["observed_at"] = "2026-09-21T14:40:00Z"
    facts["gates"]["observed_at"] = "2026-09-21T14:40:00Z"
    facts["bar"].update(open_at="2026-09-21T14:35:00Z", close_at="2026-09-21T14:40:00Z",
                        observed_at="2026-09-21T14:40:00Z")
    result = evaluate(p, state, facts, now="2026-09-21T14:40:00Z", cumulative_allocation="1",
                      remaining_allocation="1", normalized_units=str(units))
    assert result["reason"] == "FULL_ALLOCATION"
    assert result["state"]["stop"] == "110"


def test_quote_before_completed_bar_cannot_use_later_bar_information():
    facts = evidence()
    facts["bar"].update(open_at="2026-09-21T14:05:00Z", close_at=NOW)
    facts["quote"]["observed_at"] = "2026-09-21T14:09:00Z"
    assert run(facts=facts)["action"] == "WAIT"

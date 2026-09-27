"""Synthetic contract/ledger checks; never empirical profitability proof."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys

import pytest

from prism_core.oneil_paired_replay import assess_entry_quality_packet, digest, evaluate_replay
from test_oneil_adaptive_policy import evidence, plan


def payload(exit_price="120", tick_price="104"):
    p = plan()
    identity = dict(symbol="TEST", source_decision_ref="decision-test",
                    price_basis_ref="unadjusted-v1", source_ref="original-capture")
    entry = dict(identity, occurred_at=p["created_at"], available_at=p["created_at"], price="100")
    tick_at = "2026-09-25T13:41:00Z"
    tick = dict(identity, occurred_at=tick_at, available_at=tick_at,
                current_stop="100", stop_source_ref="original-stop", stop_available_at=tick_at,
                evidence=evidence(tick_price))
    terminal_at = "2026-09-25T19:59:00Z"
    terminal = dict(identity, occurred_at=terminal_at, available_at=terminal_at, price=exit_price)
    return dict(contract="oneil-paired-replay-input-v1", kind="SYNTHETIC",
                campaigns=[dict(campaign_id="c1", plan=p, entry=entry, ticks=[tick], exit=terminal)])


def test_actual_policy_bulk_add_and_full_budget_costs():
    out = evaluate_replay(payload())
    row = out["results"][0]["cost_cases"]["10"]
    assert row["decisions"][0]["action"] == "ADD"
    assert Decimal(row["decisions"][0]["target_allocation"]) == 1
    returns = row["net_return_full_budget"]
    assert Decimal(returns["baseline"]) == Decimal(".1978")
    expected = (Decimal(".001") + Decimal(".9") / 104) * 120 * Decimal(".999") - Decimal("1.001")
    assert abs(Decimal(returns["adaptive"]) - expected) < Decimal("1e-24")
    assert Decimal(row["paired_delta"]) < 0  # Later confirmation can sacrifice winner profit.
    assert Decimal(out["cost_cases"]["10"]["winner_profit_capture_ratio"]) < Decimal(".9")
    assert not out["performance_validated"] and not out["minimum_descriptive_sample_met"]


def test_protective_priority_same_quote_not_ideal_stop():
    out = evaluate_replay(payload(exit_price="200", tick_price="89"))
    row = out["results"][0]["cost_cases"]["10"]
    assert row["exit_reason"] == "COMMON_PROTECTIVE_EXIT"
    assert row["decisions"][0]["action"] == "PROTECTIVE_EXIT_REQUIRED"
    assert Decimal(row["net_return_full_budget"]["baseline"]) == Decimal("-.11189")
    assert Decimal(row["net_return_full_budget"]["adaptive"]) == Decimal("-.011189")
    assert Decimal(row["paired_delta"]) > 0


def test_cost_stress_and_determinism_no_mutation():
    data = payload()
    before = deepcopy(data)
    first = evaluate_replay(data)
    assert first == evaluate_replay(data) and data == before
    cases = first["results"][0]["cost_cases"]
    for arm in ("adaptive", "baseline"):
        assert Decimal(cases["25"]["net_return_full_budget"][arm]) < Decimal(cases["10"]["net_return_full_budget"][arm])


@pytest.mark.parametrize("mutation", ["future", "identity", "missing", "stop", "future_stop", "quote_future", "bad_bar", "source", "price", "missing_after_rejected_gate"])
def test_invalid_inputs_are_unavailable_not_wait_profit(mutation):
    data = payload()
    c = data["campaigns"][0]
    tick = c["ticks"][0]
    if mutation == "future":
        tick["available_at"] = "2026-09-25T14:00:00Z"
    elif mutation == "identity":
        tick["source_decision_ref"] = "other"
    elif mutation == "missing":
        del tick["evidence"]["volume"]
    elif mutation == "stop":
        tick["current_stop"] = "89"
    elif mutation == "future_stop":
        tick["stop_available_at"] = "2026-09-25T14:00:00Z"
    elif mutation == "quote_future":
        tick["evidence"]["quote"]["observed_at"] = "2026-09-25T14:00:00Z"
    elif mutation == "bad_bar":
        tick["evidence"]["bars"][0]["complete"] = False
    elif mutation == "source":
        tick["source_ref"] = ""
    elif mutation == "price":
        c["entry"]["price"] = "99"
    else:
        tick["evidence"]["gates"]["risk"] = False
        tick["evidence"]["volume"]["samples"] = []
    out = evaluate_replay(data)
    assert out["status"] == "INPUT_UNAVAILABLE"
    assert out["cost_cases"]["10"]["baseline"] is None
    assert out["coverage"]["unavailable"] == 1


def test_real_negative_gate_is_valid_wait():
    data = payload()
    data["campaigns"][0]["ticks"][0]["evidence"]["gates"]["risk"] = False
    row = evaluate_replay(data)["results"][0]["cost_cases"]["10"]
    assert row["decisions"][0]["reason"] == "ADD_GATE_NOT_MET"
    assert Decimal(row["net_return_full_budget"]["adaptive"]) == Decimal(".01978")


def test_paired_same_id_winner_removal_and_coverage():
    data = payload()
    loser = payload(exit_price="95")["campaigns"][0]
    loser["campaign_id"] = "loser"
    missing = dict(campaign_id="missing")
    data["campaigns"] += [loser, missing]
    out = evaluate_replay(data)
    assert out["coverage"] == dict(supplied=3, evaluated=2, unavailable=1, entry_dates=1)
    stats = out["cost_cases"]["10"]
    assert stats["remove_best_baseline_winner"]["removed_campaign_id"] == "c1"
    assert stats["remove_best_baseline_winner"]["remaining_pairs"] == 1
    assert stats["baseline"]["profit_factor_status"] == "DEFINED"


def test_no_self_attested_prospective_proof_or_duplicate_ids():
    data = payload()
    data["kind"] = "PROSPECTIVE"
    with pytest.raises(ValueError):
        evaluate_replay(data)
    data = payload()
    data["campaigns"].append(deepcopy(data["campaigns"][0]))
    with pytest.raises(ValueError):
        evaluate_replay(data)
    data = payload()
    data["kind"] = "HISTORICAL"
    data["performance_validated"] = True
    out = evaluate_replay(data)
    assert out["validation_kind"] == "EXPLORATORY_ONLY" and not out["performance_validated"]


def test_empty_is_unavailable_and_pf_is_not_infinite():
    data = payload()
    assert evaluate_replay(data)["cost_cases"]["10"]["baseline"]["profit_factor"] is None
    data["campaigns"] = []
    assert evaluate_replay(data)["status"] == "INPUT_UNAVAILABLE"


def test_cli_deterministic_exclusive_output(tmp_path):
    source, output = tmp_path / "input.json", tmp_path / "result.json"
    data = payload()
    source.write_text(json.dumps(data))
    cmd = [sys.executable, str(Path(__file__).resolve().parents[1] / "tools/evaluate_oneil_paired_replay.py"),
           "--input", str(source), "--output", str(output)]
    run = subprocess.run(cmd, capture_output=True, text=True, check=True)
    assert json.loads(run.stdout) == json.loads(output.read_text()) == evaluate_replay(data)
    assert subprocess.run(cmd, capture_output=True).returncode != 0


def canonical_packet():
    p = dict(packet_schema_version=3, analysis_contract_version="entry-quality-harness-v2",
             market="US", source_contract=dict(kind="local_sanitized_observability_jsonl"),
             as_of="2026-09-27T00:00:00Z",
             strategy_readiness=dict(analysis_basis="strategy_ledger", broker_fill_coverage_required=False,
                                     insufficiency_reasons=[dict(code="STRATEGY_CLOSED_TRADES_LT_30")]),
             coverage=dict(strategy_closed_trade_count=1),
             prospective_cohort=dict(candidate_count=2, decision_date_count=1),
             missingness=dict(component_status_distribution={"setup_quality.daily": {"MISSING": 2}}),
             robustness_inputs=dict(strategy_ranked=[dict(decision_ref="decision", ticker="TEST",
                 trigger_type="test", regime="moderate_bull", policy_version="v1", return_pct=-5)]),
             fill_provenance=dict(status_distribution={"REJECTED": 1}))
    p["packet_id"] = digest(p)[:24]
    return p


def test_canonical_coverage_preserves_rejected_order_strategy_loss():
    p = canonical_packet()
    out = assess_entry_quality_packet(p)
    assert out == assess_entry_quality_packet(p)
    assert out["status"] == "INPUT_UNAVAILABLE" and out["verdict"] == "CONTINUE_CAPTURE"
    assert out["coverage"]["strategy_closed_count"] == 1
    assert out["strategy_outcomes_independent_of_fills"][0]["return_pct"] == -5
    assert out["canonical_insufficiency_reasons"] == p["strategy_readiness"]["insufficiency_reasons"]
    assert not out["performance_validated"]


@pytest.mark.parametrize("field,value", [("packet_schema_version", 2), ("market", "KR"),
                                       ("packet_id", "forged"), ("as_of", "not-a-date")])
def test_reject_noncanonical_packet(field, value):
    p = canonical_packet()
    p[field] = value
    with pytest.raises(ValueError):
        assess_entry_quality_packet(p)


def test_error_text_cannot_export_arbitrary_source_payload():
    data = payload()
    data["campaigns"][0]["ticks"][0]["occurred_at"] = "secret-not-time"
    out = evaluate_replay(data)
    assert "secret-not-time" not in json.dumps(out)


def test_stop_cannot_ratchet_down_from_previous_tick():
    data = payload()
    ticks = data["campaigns"][0]["ticks"]
    next_tick = deepcopy(ticks[0])
    next_tick.update(occurred_at="2026-09-25T13:42:00Z", current_stop="99")
    ticks.append(next_tick)
    assert evaluate_replay(data)["status"] == "INPUT_UNAVAILABLE"

"""Fixed economic counterexamples: passing does not establish profitability."""
from decimal import Decimal

from oneil_comparison_cases import comparison_cases
from prism_core.oneil_paired_replay import evaluate_replay


def test_preregistered_comparison_and_counterexamples():
    result = evaluate_replay(comparison_cases())
    assert result == evaluate_replay(comparison_cases())
    assert result["coverage"] == {
        "supplied": 8, "evaluated": 7, "unavailable": 1, "entry_dates": 1}
    rows = {row["campaign_id"]: row for row in result["results"]}
    assert rows["missing_current_gates"]["status"] == "INPUT_UNAVAILABLE"
    for bps in ("10", "25"):
        cases = {key: row["cost_cases"][bps] for key, row in rows.items()
                 if row["status"] == "EVALUATED"}
        assert Decimal(cases["initial_failure"]["paired_delta"]) > 0
        assert Decimal(cases["failure_after_add"]["paired_delta"]) < 0
        assert Decimal(cases["strong_rise"]["paired_delta"]) < 0
        assert cases["initial_failure"]["exit_reason"] == "COMMON_PROTECTIVE_EXIT"
        assert cases["failure_after_add"]["exit_reason"] == "COMMON_PROTECTIVE_EXIT"
        assert Decimal(cases["strong_rise"]["decisions"][0]["target_allocation"]) == 1
        assert Decimal(cases["unchanged_stop_rise"]["decisions"][0]["target_allocation"]) < 1
        assert cases["weak_volume_winner"]["decisions"][0]["action"] == "WAIT"
        assert cases["extended_winner"]["decisions"][0]["action"] == "WAIT"
    assert result["validation_kind"] == "FUNCTIONAL_ONLY"
    assert result["performance_validated"] is False
    assert result["minimum_descriptive_sample_met"] is False

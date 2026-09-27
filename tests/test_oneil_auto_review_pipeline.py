from copy import deepcopy
from decimal import Decimal
import json

import numpy as np
import pandas as pd
import pytest

from prism_core.oneil_auto_review_output import build_review_bundle
from prism_core.oneil_adaptive_policy import create_plan, evaluate_target
from prism_core.oneil_setup_inputs import build_setup_input
from test_oneil_auto_review import AS_OF, valid_snapshot
from test_oneil_adaptive_policy import evidence
from tools.build_oneil_auto_review import build_packet, financial_frame_records


def test_automatic_review_to_setup_to_target_without_manual_approval():
    snapshot = valid_snapshot()
    before = deepcopy(snapshot)
    bundle = build_review_bundle(snapshot, reviewed_at=AS_OF)
    assert snapshot == before and bundle["setup_input"]["status"] == "OK"
    assert bundle["review"]["reviewer_kind"] == "VALIDATED_RULE_OUTPUT"
    pivot = Decimal(bundle["setup_input"]["setup"]["pivot"])
    plan = create_plan(
        symbol="TEST",
        entry_reference=str(pivot),
        initial_stop="100",
        source_decision_ref="decision-fixture",
        created_at=AS_OF,
        setup=bundle["setup_input"]["setup"],
        entry_eligible=True,
    )
    facts = json.loads(json.dumps(evidence()).replace("2026-09-25", "2026-09-28"))
    facts["price_basis_ref"] = snapshot["price_basis_ref"]
    facts["quote"]["price"] = str(pivot * Decimal("1.04"))
    facts["bars"][0]["close"] = str(pivot * Decimal("1.02"))
    facts["bars"][1]["close"] = facts["quote"]["price"]
    dates = evidence()["volume"]["expected_prior_trade_dates"][1:] + ["2026-09-25"]
    facts["volume"]["expected_prior_trade_dates"] = dates
    for sample, day in zip(facts["volume"]["samples"], dates):
        sample["trade_date"] = day
    decision = evaluate_target(
        plan,
        facts,
        now="2026-09-28T13:41:00Z",
        cumulative_allocation=".1",
        remaining_allocation=".1",
        normalized_units=str(Decimal(".1") / pivot),
        remaining_entry_cost=".0001",
        current_stop=str(pivot),
    )
    assert decision["action"] == "ADD" and decision["target_allocation"] == "1.0000"
    assert (
        not bundle["live_ready"]
        and not bundle["broker_execution"]
        and not bundle["policy_executed"]
    )


def test_negative_and_unknown_reviews_never_promoted_by_positive_prose():
    snapshot = valid_snapshot()
    snapshot["rationale"] = "Extremely strong leadership; buy confidently"
    snapshot["financials"]["records"][0]["revenue"] = "124"
    bundle = build_review_bundle(snapshot, reviewed_at=AS_OF)
    assert bundle["setup_input"]["status"] == "REJECTED"
    assert bundle["setup_input"]["setup"] is None
    snapshot["financials"]["records"][0]["revenue"] = None
    bundle = build_review_bundle(snapshot, reviewed_at=AS_OF)
    assert bundle["setup_input"]["status"] == "MISSING"


def test_generated_report_tampering_rejected_and_frozen_replay_identical():
    source = dict(snapshot=valid_snapshot(), reviewed_at=AS_OF, raw_sources={})
    assert build_packet(source) == build_packet(source)
    bundle = build_packet(source)["bundle"]
    result = build_setup_input(
        report_text=bundle["report_text"] + "changed",
        review=bundle["review"],
        symbol="TEST",
        decision_ref="decision-fixture",
        as_of=AS_OF,
        price_basis_ref="unadjusted-price-v1",
    )
    assert result["status"] == "INVALID"


def test_exact_quarterly_financial_rows_and_no_basic_eps_alias():
    date = pd.Timestamp("2026-06-30")
    frame = pd.DataFrame({date: [1.25, 125]}, index=["Diluted EPS", "Total Revenue"])
    assert financial_frame_records(frame) == [
        {"period_end": "2026-06-30", "eps": 1.25, "revenue": 125.0}
    ]
    frame.index = ["Basic EPS", "Total Revenue"]
    assert financial_frame_records(frame)[0]["eps"] is None
    assert financial_frame_records(pd.concat([frame, frame])) == []
    frame.columns = ["TTM"]
    assert financial_frame_records(frame) == []


@pytest.mark.parametrize(
    "value", [True, np.bool_(True), np.nan, np.inf, 10**400, "25%", None]
)
def test_provider_numbers_not_guessed(value):
    frame = pd.DataFrame(
        {pd.Timestamp("2026-06-30"): pd.Series([value, 125], dtype=object).values},
        index=["Diluted EPS", "Total Revenue"],
        dtype=object,
    )
    assert financial_frame_records(frame)[0]["eps"] is None

from copy import deepcopy

from prism_core.oneil_adaptive_policy import create_plan, evaluate_target
from prism_core.oneil_input_bridge import assemble_evidence
from prism_core.oneil_intraday_inputs import build_intraday_inputs
from test_oneil_adaptive_policy import evidence
from test_oneil_intraday_inputs import fixture
from test_oneil_setup_inputs import build


def inputs():
    setup = build()
    plan = create_plan(
        symbol="TEST",
        entry_reference="100",
        initial_stop="95",
        source_decision_ref="d1",
        created_at="2026-09-25T13:30:00Z",
        setup=setup["setup"],
        entry_eligible=True,
    )
    data = fixture()
    data.update(kind="LIVE_CAPTURE", retrieval_started_at="2026-09-25T13:40:00Z")
    intraday = build_intraday_inputs(**data)
    e = evidence()
    return dict(
        plan=plan,
        setup_input=setup,
        intraday_input=intraday,
        quote=e["quote"],
        gates=e["gates"],
        now="2026-09-25T13:41:00Z",
    )


def test_source_to_policy_integration_and_no_mutation():
    args = inputs()
    original = deepcopy(args)
    result = assemble_evidence(**args)
    assert result["status"] == "OK" and not result["execution_authorized"]
    assert args == original
    decision = evaluate_target(
        args["plan"],
        result["evidence"],
        now=args["now"],
        cumulative_allocation=".1",
        remaining_allocation=".1",
        normalized_units=".001",
        remaining_entry_cost=".0001",
        current_stop="100",
    )
    assert decision["action"] == "ADD" and decision["target_allocation"] == "1.0000"
    result["evidence"]["bars"][0]["close"] = "1"
    assert args == original


def test_reconstructed_data_never_becomes_live_evidence():
    args = inputs()
    args["intraday_input"]["kind"] = "RECONSTRUCTED_REPLAY"
    assert assemble_evidence(**args)["evidence"] is None


def test_missing_setup_quote_or_gate_never_inferred():
    for key in ("setup_input", "quote", "gates"):
        args = inputs()
        args[key] = {}
        result = assemble_evidence(**args)
        assert result["status"] == "MISSING" and result["evidence"] is None


def test_stale_or_future_quote_and_price_basis_fail_closed():
    args = inputs()
    args["quote"]["observed_at"] = "2026-09-25T13:39:00Z"
    assert assemble_evidence(**args)["evidence"] is None
    args = inputs()
    args["quote"]["observed_at"] = "2026-09-25T13:42:00Z"
    assert assemble_evidence(**args)["evidence"] is None
    args = inputs()
    args["intraday_input"]["price_basis_ref"] = "another-basis"
    assert assemble_evidence(**args)["evidence"] is None

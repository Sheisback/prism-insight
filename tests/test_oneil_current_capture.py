"""Real input producer -> adapter -> policy, without network or order effects."""

import asyncio
from copy import deepcopy

import pytest

from prism_core.oneil_adaptive_policy import evaluate_target
from prism_core.oneil_current_capture import (
    capture_current_record, collect_current_record, existing_us_snapshot,
)
from test_oneil_input_bridge import inputs


def arguments():
    args = inputs()
    plan = args["plan"]
    identity = dict(symbol=plan["symbol"], source_decision_ref=plan["source_decision_ref"],
                    position_id="holding:1", price_basis_ref=plan["setup"]["price_basis_ref"])
    args["position_id"] = identity["position_id"]
    args["quote"].update(identity)
    args["gates"].update(identity, quote_source_ref=args["quote"]["source_ref"], price=args["quote"]["price"])
    args["stop"] = dict(identity, current_stop="100", source_ref="original-stop-update",
                        available_at="2026-09-25T13:40:00Z")
    return args


def decision(args, record):
    return evaluate_target(args["plan"], record["tick"]["evidence"], now=args["now"],
                           cumulative_allocation=".1", remaining_allocation=".1",
                           normalized_units=".001", remaining_entry_cost=".0001", current_stop="100")


def test_live_input_through_actual_policy_is_bound_and_nonmutating():
    args = arguments()
    before = deepcopy(args)
    record = capture_current_record(**args)
    assert record["status"] == "OK"
    assert decision(args, record)["action"] == "ADD"
    assert args == before
    assert record["execution_authorized"] is False
    assert capture_current_record(**args) == record


def test_negative_gate_is_valid_observation_not_missing():
    args = arguments()
    args["gates"]["risk"] = False
    record = capture_current_record(**args)
    assert record["status"] == "OK"
    assert decision(args, record)["reason"] == "ADD_GATE_NOT_MET"


@pytest.mark.parametrize("kind,key,value", [
    ("quote", "observed_at", None),
    ("quote", "observed_at", "2026-09-25T13:38:00Z"),
    ("quote", "observed_at", "2026-09-25T13:42:00Z"),
    ("quote", "price", float("nan")),
    ("quote", "symbol", "OTHER"),
    ("gates", "observed_at", "2026-09-25T13:38:00Z"),
    ("gates", "observed_at", "2026-09-25T13:42:00Z"),
    ("gates", "source_decision_ref", "other"),
    ("gates", "position_id", "other"),
    ("gates", "quote_source_ref", "other"),
    ("gates", "price", "103"),
    ("gates", "risk", "True"),
    ("gates", "market_pulse", None),
    ("stop", "available_at", "2026-09-25T13:42:00Z"),
    ("stop", "current_stop", "90"),
])
def test_missing_stale_future_wrong_identity_never_becomes_tick(kind, key, value):
    args = arguments()
    args[kind][key] = value
    # A negative condition must not short-circuit source validation.
    args["gates"]["admission"] = False
    record = capture_current_record(**args)
    assert record["status"] == "MISSING" and record["tick"] is None
    assert record["reason_codes"]


def test_protection_and_terminal_observation_survive_absent_add_inputs():
    args = arguments()
    args["exit_event"] = dict(args["quote"], occurred_at=args["now"], available_at=args["now"])
    args.update(gates=None, intraday_input=None)
    record = capture_current_record(**args)
    assert record["tick"] is None
    assert record["protection"]["quote"]["price"] == "104"
    assert record["protection"]["stop"]["current_stop"] == "100"
    assert record["exit_event"]["price"] == "104"


def test_existing_numeric_snapshot_cannot_be_falsely_fresh():
    args = arguments()
    snapshot = existing_us_snapshot(current_price=104, current_stop=100,
                                   source_ref="regular-batch", **{k: args["quote"][k] for k in
                                   ("symbol", "source_decision_ref", "position_id", "price_basis_ref")})
    assert snapshot["quote"]["observed_at"] is None
    record = capture_current_record(**{**args, **snapshot})
    assert record["tick"] is None and record["protection"]["quote"] is None


def test_async_suppliers_keep_original_clocks_and_failure_is_isolated():
    args = arguments()

    async def quote():
        return args["quote"]

    def gates():
        raise RuntimeError("private provider details")

    record = asyncio.run(collect_current_record(suppliers=dict(quote=quote, gates=gates), **args))
    assert record["tick"] is None
    assert record["protection"]["quote"]["observed_at"] == args["quote"]["observed_at"]
    assert "private" not in str(record)


def test_supplier_timeout_does_not_hide_independent_protection():
    args = arguments()

    async def delayed():
        await asyncio.sleep(1)

    record = asyncio.run(collect_current_record(suppliers={"gates": delayed}, timeout_seconds=.001, **args))
    assert record["tick"] is None
    assert record["protection"]["stop"] is not None

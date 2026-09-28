import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from prism_core.execution_service import ExecutionService
from prism_core.oneil_current_capture import capture_current_record
from prism_core.oneil_execution import OneilExecution
from test_oneil_current_capture import arguments


def start(tmp_path, mode="SHADOW"):
    args = arguments()
    core = OneilExecution(tmp_path / "execution.sqlite", mode=mode)
    account = dict(status="OK", account_id="account", symbol="TEST", quantity=0,
                   observed_at=args["now"], source_ref="account-source", open_orders_status="OK", open_orders_count=0)
    state = core.claim_campaign(account_id="account", position_id=args["position_id"], plan=args["plan"],
                                unit_budget=10000, account_snapshot=account, now=args["now"])
    return core, state["campaign_id"], args, account


def reserve(core, cid, args, account):
    return core.evaluate(cid, capture_current_record(**args),
                         expected_revision=core.snapshot(cid)["revision"], account_snapshot=account)


def ack(core, reserved, order="order-1"):
    intent = reserved["intent"]
    core.store.claim_reservation(reserved["reservation"], intent, expected_side=intent.side)
    core.store.record_result(intent, status="SUBMITTED", accepted=True,
                             response=dict(order_no=order, quantity=intent.quantity, price=intent.limit_price))


def receipt(reserved, *, quantity=None, status="FILLED", virtual=False, fees="1", order="order-1"):
    intent = reserved["intent"]
    quantity = intent.quantity if quantity is None else quantity
    return dict(intent_id=intent.id, account_id=intent.account_id, symbol=intent.symbol, side=intent.side,
                virtual=virtual, broker_order_id=order, broker_order_date="2026-09-25",
                filled_quantity=quantity, filled_notional=str(quantity * 104), fees=fees,
                status=status, observed_at="2026-09-25T13:41:00Z")


def test_shadow_full_path_uses_same_reservation_and_reconciliation(tmp_path):
    core, cid, args, account = start(tmp_path)
    reserved = reserve(core, cid, args, account)
    assert reserved["intent"].quantity == 48
    assert reserved["intent"].execution_mode == "shadow"
    state = core.simulate_submission(reserved, now=args["now"])
    assert state["confirmed_quantity"] == 48 and state["confirmed_buy_notional"] == "4992"
    restarted = OneilExecution(core.store.db_path)
    assert restarted.snapshot(cid) == core.snapshot(cid)
    with pytest.raises(TypeError):
        core.simulate_submission(reserved, now=args["now"])


def test_mode_immutable_and_existing_holding_never_adopted(tmp_path):
    core, cid, args, account = start(tmp_path)
    with pytest.raises(ValueError):
        OneilExecution(core.store.db_path, mode="LIVE")
    account["quantity"] = 100
    with pytest.raises(ValueError):
        core.claim_campaign(account_id="account", position_id="other", plan=args["plan"],
                            unit_budget=10000, account_snapshot=account, now=args["now"])
    assert core.owner_by_symbol("account", "TEST")["campaign_id"] == cid
    assert len(core.list_campaigns()) == 1
    core.link_strategy_position(cid, "strategy:1")
    with pytest.raises(ValueError):
        core.link_strategy_position(cid, "strategy:2")


def test_live_actual_executionservice_consumes_original_reservation(tmp_path):
    core, cid, args, account = start(tmp_path, "LIVE")
    reserved = reserve(core, cid, args, account)
    calls = []
    class Trader:
        async def async_buy_stock(self, **kwargs):
            calls.append(kwargs)
            return dict(success=True, order_no="order-1", quantity=48, price=104)
    async def submit():
        async with ExecutionService(Trader(), intent_store=core.store) as service:
            return await service.execute_pre_reserved_buy(intent=reserved["intent"], reservation=reserved["reservation"], ticker="TEST")
    asyncio.run(submit())
    state = core.reconcile(cid, reserved["intent"].id, receipt(reserved))
    assert len(calls) == 1 and state["confirmed_quantity"] == 48
    with pytest.raises(TypeError):
        asyncio.run(submit())
    assert len(calls) == 1


def test_partial_cancel_exit_latched_and_only_confirmed_owned_sold(tmp_path):
    core, cid, args, account = start(tmp_path, "LIVE")
    reserved = reserve(core, cid, args, account)
    ack(core, reserved)
    part = receipt(reserved, quantity=12, status="PARTIAL")
    core.reconcile(cid, reserved["intent"].id, part)
    latched = core.request_exit(cid, reason="PROTECTIVE_STOP")
    assert latched["pending_orders"] and latched["status"] == "EXIT_PENDING"
    args["source"] = "regular"
    assert reserve(core, cid, args, account)["status"] == "ACCOUNT_UNKNOWN_OR_FOREIGN"
    cancelled = dict(part, status="CANCELLED")
    core.reconcile(cid, reserved["intent"].id, cancelled)
    args["now"] = "2026-09-25T13:41:01Z"
    account["quantity"] = 12
    sell = reserve(core, cid, args, account)
    assert sell["intent"].side == "SELL" and sell["intent"].quantity == 12
    ack(core, sell, "sell-order")
    final = receipt(sell, quantity=12, order="sell-order")
    final["observed_at"] = args["now"]
    state = core.reconcile(cid, sell["intent"].id, final)
    assert state["status"] == "CLOSED" and state["confirmed_quantity"] == 0


def test_unknown_fees_preserves_confirmed_quantity_and_models_risk(tmp_path):
    core, cid, args, account = start(tmp_path, "LIVE")
    reserved = reserve(core, cid, args, account)
    ack(core, reserved)
    fill = receipt(reserved, fees=None)
    fill["fees_status"] = "UNKNOWN"
    state = core.reconcile(cid, reserved["intent"].id, fill)
    assert state["confirmed_quantity"] == 48
    assert state["remaining_entry_fees"] == "4.992"
    assert not state["actual_fees_known"]


@pytest.mark.parametrize("field,value", [("broker_order_id", "wrong"), ("account_id", "wrong"),
                                        ("symbol", "wrong"), ("virtual", True),
                                        ("filled_quantity", 49), ("filled_notional", "99999")])
def test_receipt_identity_and_cumulative_bounds(tmp_path, field, value):
    core, cid, args, account = start(tmp_path, "LIVE")
    reserved = reserve(core, cid, args, account)
    ack(core, reserved)
    fill = receipt(reserved)
    fill[field] = value
    before = core.snapshot(cid)
    with pytest.raises(ValueError):
        core.reconcile(cid, reserved["intent"].id, fill)
    assert core.snapshot(cid) == before


def test_created_restart_never_blindly_resubmits(tmp_path):
    core, cid, args, account = start(tmp_path)
    reserve(core, cid, args, account)
    core = OneilExecution(core.store.db_path)
    args["source"] = "regular"
    result = reserve(core, cid, args, account)
    assert result["status"] == "RECONCILE_REQUIRED" and result["intent"] is None


def test_concurrent_reservation_at_most_once(tmp_path):
    core, cid, args, account = start(tmp_path)
    record = capture_current_record(**args)
    def run(_):
        return core.evaluate(cid, record, expected_revision=0, account_snapshot=account)["status"]
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(run, range(2))) == ["DUPLICATE", "RESERVED"]
    assert len(core.snapshot(cid)["orders"]) == 1


def test_disable_add_does_not_disable_owned_exit(tmp_path):
    core, cid, args, account = start(tmp_path)
    reserved = reserve(core, cid, args, account)
    core.simulate_submission(reserved, now=args["now"])
    core.request_exit(cid, reason="ORIGINAL_EXIT")
    args["source"] = "regular"
    account["quantity"] = 48
    result = core.evaluate(cid, capture_current_record(**args), expected_revision=core.snapshot(cid)["revision"],
                           account_snapshot=account, allow_add=False)
    assert result["intent"].side == "SELL"


def test_reservation_rolls_back_with_state_failure(tmp_path, monkeypatch):
    core, cid, args, account = start(tmp_path)
    before = core.snapshot(cid)
    def fail(*args):
        raise RuntimeError("injected")
    monkeypatch.setattr(core, "_save", fail)
    with pytest.raises(RuntimeError):
        reserve(core, cid, args, account)
    assert core.snapshot(cid) == before


def test_exit_cancels_unsubmitted_capability_before_broker_call(tmp_path):
    core, cid, args, account = start(tmp_path)
    reserved = reserve(core, cid, args, account)
    result = core.request_exit(cid, reason="PROTECTIVE_STOP")
    assert result["pending_orders"] == []
    with pytest.raises(RuntimeError):
        core.simulate_submission(reserved, now=args["now"])
    assert core.snapshot(cid)["confirmed_quantity"] == 0


def test_terminal_receipt_cannot_regress_to_unknown(tmp_path):
    core, cid, args, account = start(tmp_path)
    reserved = reserve(core, cid, args, account)
    core.simulate_submission(reserved, now=args["now"])
    unknown = receipt(reserved, status="UNKNOWN", virtual=True)
    with pytest.raises(ValueError, match="terminal"):
        core.reconcile(cid, reserved["intent"].id, unknown)


def test_protective_exit_survives_missing_add_inputs(tmp_path):
    core, cid, args, account = start(tmp_path)
    reserved = reserve(core, cid, args, account)
    core.simulate_submission(reserved, now=args["now"])
    account["quantity"] = 48
    args["gates"] = None
    args["quote"]["price"] = "99"
    result = reserve(core, cid, args, account)
    assert result["intent"].side == "SELL" and result["intent"].quantity == 48
    assert result["campaign"]["exit_reason"] == "PROTECTIVE_STOP"


def test_unknown_fees_to_lower_actual_does_not_lose_new_shares(tmp_path):
    core, cid, args, account = start(tmp_path, "LIVE")
    reserved = reserve(core, cid, args, account)
    ack(core, reserved)
    first = receipt(reserved, quantity=12, status="PARTIAL", fees=None)
    first["fees_status"] = "UNKNOWN"
    core.reconcile(cid, reserved["intent"].id, first)
    state = core.reconcile(cid, reserved["intent"].id, receipt(reserved, quantity=24, status="PARTIAL", fees="1"))
    assert state["confirmed_quantity"] == 24
    assert state["remaining_entry_fees"] == "1.248"
    assert not state["actual_fees_known"]


def test_authoritative_rejection_releases_pending_without_fill(tmp_path):
    core, cid, args, account = start(tmp_path, "LIVE")
    reserved = reserve(core, cid, args, account)
    intent = reserved["intent"]
    core.store.claim_reservation(reserved["reservation"], intent, expected_side="BUY")
    core.store.record_result(intent, status="REJECTED", accepted=False, response=dict(message="rejected"))
    rejected = receipt(reserved, quantity=0, status="REJECTED", fees="0", order=None)
    state = core.reconcile(cid, intent.id, rejected)
    assert state["confirmed_quantity"] == 0
    assert core.snapshot(cid)["orders"][0]["reserved_notional"] == "0"


def test_pending_ack_preserves_reservation(tmp_path):
    core, cid, args, account = start(tmp_path, "LIVE")
    reserved = reserve(core, cid, args, account)
    ack(core, reserved)
    core.reconcile(cid, reserved["intent"].id, receipt(reserved, quantity=0, status="PENDING", fees="0"))
    assert core.snapshot(cid)["orders"][0]["reserved_notional"] == "4992"
    with pytest.raises(ValueError):
        core.release_unsubmitted(cid, reserved["intent"].id, reason="failed-preflight")


def test_preflight_release_is_created_only_and_invalidates_capability(tmp_path):
    core, cid, args, account = start(tmp_path)
    reserved = reserve(core, cid, args, account)
    core.release_unsubmitted(cid, reserved["intent"].id, reason="quote-changed")
    assert core.snapshot(cid)["status"] == "ACTIVE"
    assert core.snapshot(cid)["orders"][0]["reserved_notional"] == "0"
    with pytest.raises(RuntimeError):
        core.simulate_submission(reserved, now=args["now"])


def test_noncent_quote_does_not_reserve_or_change_observed_price(tmp_path):
    core, cid, args, account = start(tmp_path)
    args["quote"]["price"] = "104.001"
    args["gates"]["price"] = "104.001"
    result = reserve(core, cid, args, account)
    assert result["status"] == "UNSUPPORTED_PRICE_PRECISION"
    assert not core.snapshot(cid)["orders"]


def test_exit_protects_partial_shares_while_buy_is_unknown_and_late_fills(tmp_path):
    core, cid, args, account = start(tmp_path, "LIVE")
    buy = reserve(core, cid, args, account)
    ack(core, buy)
    fill = receipt(buy, quantity=12, status="UNKNOWN", fees=None)
    fill.update(fill_evidence_status="CONFIRMED", fees_status="UNKNOWN")
    core.reconcile(cid, buy["intent"].id, fill)
    core.request_exit(cid, reason="PROTECTIVE_STOP")
    args["now"] = "2026-09-25T13:41:01Z"
    account["quantity"] = 18  # Six shares not yet attributable must not be sold.
    sell = reserve(core, cid, args, account)
    assert sell["intent"].quantity == 12
    ack(core, sell, "sell1")
    first_exit = receipt(sell, quantity=12, order="sell1")
    core.reconcile(cid, sell["intent"].id, first_exit)
    state = core.snapshot(cid)
    assert state["status"] == "EXIT_PENDING" and state["confirmed_quantity"] == 0
    late_fill = dict(fill, filled_quantity=18, filled_notional=str(18 * 104), status="CANCELLED")
    core.reconcile(cid, buy["intent"].id, late_fill)
    assert core.snapshot(cid)["confirmed_quantity"] == 6
    args["now"] = "2026-09-25T13:41:02Z"
    account["quantity"] = 6
    sell2 = reserve(core, cid, args, account)
    assert sell2["intent"].quantity == 6
    ack(core, sell2, "sell2")
    core.reconcile(cid, sell2["intent"].id, receipt(sell2, quantity=6, order="sell2"))
    assert core.snapshot(cid)["status"] == "CLOSED"


def test_claim_requires_authoritative_empty_pending_orders(tmp_path):
    core, _, args, account = start(tmp_path)
    for bad in (dict(account, open_orders_count=1), dict(account, open_orders_status="UNKNOWN")):
        with pytest.raises(ValueError, match="existing orders"):
            core.claim_campaign(account_id="account", position_id="new", plan=args["plan"],
                                unit_budget=10000, account_snapshot=bad, now=args["now"])


def test_context_is_bounded_and_immutable(tmp_path):
    core, cid, _, _ = start(tmp_path)
    core.attach_context(cid, dict(account_name="account", scenario={"stop": 95}, report_path="report.pdf"))
    with pytest.raises(ValueError):
        core.attach_context(cid, dict(account_name="other"))
    with pytest.raises(ValueError):
        core.attach_context(cid, dict(api_key="forbidden"))


def test_claim_context_is_atomic_and_retry_immutable(tmp_path, monkeypatch):
    core, _, args, account = start(tmp_path)
    account = dict(account, account_id="second")
    kwargs = dict(account_id="second", position_id="new", plan=args["plan"], unit_budget=10000,
                  account_snapshot=account, now=args["now"], context=dict(account_name="second", scenario={"stop": 95}))
    actual = core._event
    def fail(*args):
        raise RuntimeError("injected context persistence failure")
    monkeypatch.setattr(core, "_event", fail)
    with pytest.raises(RuntimeError):
        core.claim_campaign(**kwargs)
    assert core.owner_by_symbol("second", "TEST") is None
    monkeypatch.setattr(core, "_event", actual)
    state = core.claim_campaign(**kwargs)
    assert state["context"] == kwargs["context"] and state["context_hash"]
    assert core.claim_campaign(**kwargs) == state
    with pytest.raises(ValueError):
        core.claim_campaign(**dict(kwargs, context={"account_name": "different"}))


def test_expired_zero_without_pending_releases_owner(tmp_path):
    core, cid, args, account = start(tmp_path)
    # v2 plans expire 14 calendar days after creation (10 trading sessions).
    args["now"] = "2026-10-09T13:41:00Z"
    args["quote"] = None
    args["gates"] = None
    result = reserve(core, cid, args, account)
    assert result["status"] == "NO_ENTRY_EXPIRED"
    assert core.owner_by_symbol("account", "TEST") is None


def test_expiry_never_closes_unknown_unfilled_order(tmp_path):
    core, cid, args, account = start(tmp_path, "LIVE")
    reserved = reserve(core, cid, args, account)
    core.store.claim_reservation(reserved["reservation"], reserved["intent"], expected_side="BUY")
    core.store.record_result(reserved["intent"], status="UNKNOWN", accepted=False, response={})
    args.update(now="2026-09-30T13:41:00Z", quote=None, gates=None)
    result = reserve(core, cid, args, account)
    assert result["status"] == "RECONCILE_REQUIRED"
    assert core.owner_by_symbol("account", "TEST") is not None


def test_strategy_exit_marker_is_independent_durable_and_idempotent(tmp_path):
    core, cid, args, _ = start(tmp_path)
    before = core.snapshot(cid)
    state = core.mark_strategy_exit(cid, at=args["now"], source_ref="strategy:exit")
    assert state["status"] == before["status"]
    assert state["confirmed_quantity"] == before["confirmed_quantity"]
    assert core.mark_strategy_exit(cid, at=args["now"], source_ref="strategy:exit") == state
    assert OneilExecution(core.store.db_path).snapshot(cid)["strategy_exit_recorded"] == state["strategy_exit_recorded"]
    with pytest.raises(ValueError):
        core.mark_strategy_exit(cid, at=args["now"], source_ref="other")


def test_strategy_exit_reference_freezes_first_decision_not_later_quote(tmp_path):
    core, cid, args, account = start(tmp_path)
    reserved = reserve(core, cid, args, account)
    core.simulate_submission(reserved, now=args["now"])
    account["quantity"] = 48
    args.update(gates=None, now="2026-09-25T13:41:01Z")
    args["quote"]["price"] = "99"
    first = reserve(core, cid, args, account)
    reference = first["campaign"]["strategy_exit_reference"]
    assert reference["price"] == "99"
    later = core.request_exit(cid, reason="ORIGINAL_EXIT", reference=dict(at="2026-09-25T13:42:00Z", price="98", source_ref="later"))
    assert later["strategy_exit_reference"] == reference
    assert OneilExecution(core.store.db_path).snapshot(cid)["strategy_exit_reference"] == reference


def test_invalid_exit_reference_does_not_latch_or_create_reference(tmp_path):
    core, cid, args, _ = start(tmp_path)
    before = core.snapshot(cid)
    with pytest.raises(ValueError):
        core.request_exit(cid, reason="ORIGINAL_EXIT", reference=dict(at=args["now"], price=0, source_ref="exit"))
    assert core.snapshot(cid) == before

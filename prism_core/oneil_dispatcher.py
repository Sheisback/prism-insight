"""Async owned-campaign driver; SHADOW never constructs a real broker.

Accepted orders are reconciled by exact broker identifiers. Restart recovery
releases only locally CREATED reservations, never retries uncertain submission.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import inspect
import logging
import time

from prism_core.oneil_adaptive_policy import V1_VERSION, _num, _time
from prism_core.oneil_broker import OneilBroker
from prism_core.oneil_runtime import OneilRuntime
from prism_core.order_intents import OrderIntent


def _clock(now):
    if callable(now):
        return now
    if now is None:
        return lambda: datetime.now(timezone.utc).isoformat()
    anchor, started = _time(now), time.monotonic()
    return lambda: (anchor + timedelta(seconds=time.monotonic() - started)).isoformat()


def _order_metadata(execution, intent_id):
    with execution.store._connect() as db:
        row = db.execute("SELECT status FROM order_intents WHERE id=?", (intent_id,)).fetchone()
        accepted = db.execute("SELECT broker_order_id FROM broker_orders WHERE intent_id=? AND accepted=1 ORDER BY submitted_at DESC", (intent_id,)).fetchall()
        return dict(status=row[0] if row else "UNKNOWN", order_id=next((r[0] for r in accepted if r[0]), None))


def _rejected_receipt(intent, now):
    return dict(intent_id=intent.id, account_id=intent.account_id, symbol=intent.symbol, side=intent.side,
                virtual=False, status="REJECTED", broker_order_id=None, broker_order_date="NO_ACCEPTED_ORDER",
                filled_quantity=0, filled_notional="0", fees="0", observed_at=now)


async def _read_receipt(execution, cid, broker, intent, order_id, order_date, clock):
    try:
        receipt = await broker.reconcile(intent, order_id, order_date)
        if receipt.get("intent_id") != intent.id or receipt.get("account_id") != intent.account_id:
            raise ValueError("incomplete receipt")
        await asyncio.to_thread(execution.reconcile, cid, intent.id, receipt)
    except Exception:
        unknown = dict(intent_id=intent.id, account_id=intent.account_id, symbol=intent.symbol,
                       side=intent.side, virtual=False, status="UNKNOWN", observed_at=clock())
        try:
            await asyncio.to_thread(execution.reconcile, cid, intent.id, unknown)
        except ValueError:
            pass  # A concurrent terminal receipt wins; never regress it.


async def _reconcile(execution, cid, broker, clock):
    snapshot = await asyncio.to_thread(execution.snapshot, cid)
    for order in snapshot["orders"]:
        if order["status"] in {"FILLED", "CANCELLED", "REJECTED"}:
            continue
        intent = OrderIntent(**order["intent"])
        metadata = await asyncio.to_thread(_order_metadata, execution, intent.id)
        if metadata["status"] == "CREATED":
            try:
                await asyncio.to_thread(execution.release_unsubmitted, cid, intent.id, reason="RESTART_UNSUBMITTED_RECOVERY")
            except ValueError:
                pass  # A concurrent dispatcher claimed it; never resubmit.
            continue
        if metadata["status"] in {"FAILED", "REJECTED"}:
            await asyncio.to_thread(execution.reconcile, cid, intent.id, _rejected_receipt(intent, clock()))
            continue
        order_id = order["broker_order_id"] or metadata["order_id"]
        if not order_id:
            continue
        await _read_receipt(execution, cid, broker, intent, order_id, order["broker_order_date"], clock)
        current = await asyncio.to_thread(execution.snapshot, cid)
        reprice_sell = intent.side == "SELL" and (_time(clock()) - _time(order.get("reserved_at", intent.created_at))).total_seconds() >= 60
        if current["status"] == "EXIT_PENDING" and (intent.side == "BUY" or reprice_sell):
            latest = next(o for o in current["orders"] if o["intent"]["id"] == intent.id)
            if latest["status"] not in {"FILLED", "CANCELLED", "REJECTED"}:
                try:
                    await broker.cancel(intent, order_id, latest["broker_order_date"])
                except Exception:
                    logging.getLogger(__name__).warning("owned cancellation unconfirmed; protecting known shares")
                # Cancellation acknowledgement alone releases no reservation.
                await _read_receipt(execution, cid, broker, intent, order_id, latest["broker_order_date"], clock)


async def _recover_shadow(execution, cid, clock):
    snapshot = await asyncio.to_thread(execution.snapshot, cid)
    for order in snapshot["orders"]:
        if order["status"] in {"FILLED", "CANCELLED", "REJECTED"}:
            continue
        intent = OrderIntent(**order["intent"])
        metadata = await asyncio.to_thread(_order_metadata, execution, intent.id)
        if metadata["status"] == "CREATED":
            try:
                await asyncio.to_thread(execution.release_unsubmitted, cid, intent.id, reason="RESTART_UNSUBMITTED_RECOVERY")
            except ValueError:
                pass
        elif metadata["status"] == "SUBMITTED" and metadata["order_id"] == "SHADOW:" + intent.id:
            notional = _num(intent.limit_price) * intent.quantity
            await asyncio.to_thread(execution.reconcile, cid, intent.id, dict(
                intent_id=intent.id, account_id=intent.account_id, symbol=intent.symbol, side=intent.side,
                virtual=True, status="FILLED", broker_order_id=metadata["order_id"],
                broker_order_date=_time(clock()).date().isoformat(), filled_quantity=intent.quantity,
                filled_notional=str(notional), fees=str(notional * _num(snapshot["plan"]["fee_rate"])), observed_at=clock()))


def _validator(execution, reserved, clock, authorize_add=None):
    intent, state = reserved["intent"], reserved["campaign"]
    def validate(price):
        current = execution.snapshot(state["campaign_id"])
        if not 0 <= (_time(clock()) - _time(state["last_observed_at"])).total_seconds() <= 120:
            raise ValueError("STALE_RESERVED_EVIDENCE")
        observed = _num(price, True)
        if intent.side == "BUY":
            if execution.mode == "LIVE" and (authorize_add is None or authorize_add() is not True):
                raise ValueError("LIVE_ADD_AUTHORIZATION_REQUIRED")
            if observed > _num(intent.limit_price, True):
                raise ValueError("QUOTE_ABOVE_RESERVED_LIMIT")
            if current["status"] != "ACTIVE" or current["revision"] != state["revision"]:
                raise ValueError("ADD_AUTHORITY_CHANGED")
            plan = current["plan"]
            pivot = _num(plan["setup"]["pivot"], True)
            persisted_order = next(order for order in current["orders"] if order["intent"]["id"] == intent.id)
            target = _num(persisted_order["target_allocation"])
            if plan["policy_version"] == V1_VERSION:
                upper = pivot * Decimal("1.05")
                threshold = pivot * (Decimal("1.04") if target > Decimal(".8") else
                                     Decimal("1.02") if target > Decimal(".5") else Decimal(1))
            else:
                # v2 mirrors the policy: first entry keeps the pivot buy band;
                # later steps are relative to the frozen entry, capped at +10%.
                entry, initial = _num(plan["entry_reference"], True), _num(plan["initial_nominal"], True)
                upper = entry * Decimal("1.10") if current["confirmed_quantity"] else pivot * Decimal("1.05")
                threshold = (entry * Decimal("1.04") if target > Decimal(".8") else
                             entry * Decimal("1.02") if target > initial else pivot)
            if not pivot <= _num(price) <= upper or _num(price) <= _num(current["current_stop"]):
                raise ValueError("RESERVED_PRICE_OUTSIDE_POLICY")
            if observed < threshold:
                raise ValueError("TARGET_PRICE_CONFIRMATION_LOST")
            if current["confirmed_quantity"] and observed * current["confirmed_quantity"] <= (
                    _num(current["remaining_principal"]) + _num(current["remaining_entry_fees"])):
                raise ValueError("ADD_NO_LONGER_PROFITABLE")
        elif current["status"] != "EXIT_PENDING" or current["confirmed_quantity"] < intent.quantity:
            raise ValueError("SELL_OWNERSHIP_CHANGED")
    return validate


async def _submit(execution, reserved, broker, clock, authorize_add=None):
    cid, intent = reserved["campaign"]["campaign_id"], reserved["intent"]
    validator = _validator(execution, reserved, clock, authorize_add)
    try:
        await asyncio.to_thread(validator, intent.limit_price)
        acknowledgement = await broker.submit(intent, reserved["reservation"], quote_validator=validator)
    except Exception as exc:
        try:
            await asyncio.to_thread(execution.release_unsubmitted, cid, intent.id, reason="SUBMISSION_PREFLIGHT_REFUSED")
        except ValueError:
            pass  # SUBMITTING/UNKNOWN cannot be declared unsubmitted.
        await _reconcile(execution, cid, broker, clock)
        return dict(status="SUBMISSION_UNCONFIRMED", exception_type=type(exc).__name__,
                    campaign=await asyncio.to_thread(execution.snapshot, cid))
    await _reconcile(execution, cid, broker, clock)
    return dict(status="SUBMITTED_FOR_RECONCILIATION", acknowledgement=acknowledgement,
                campaign=await asyncio.to_thread(execution.snapshot, cid))


async def _before_submit(execution, reserved, callback, authorize_add=None):
    if execution.mode == "LIVE" and reserved["intent"].side == "BUY":
        try:
            authorized = authorize_add is not None and await asyncio.to_thread(authorize_add) is True
        except Exception:
            authorized = False
        if not authorized:
            await asyncio.to_thread(execution.release_unsubmitted, reserved["campaign"]["campaign_id"],
                                    reserved["intent"].id, reason="LIVE_ADD_AUTHORIZATION_REQUIRED")
            return "LIVE_ADD_AUTHORIZATION_REQUIRED"
    if callback is None:
        return "READY"
    try:
        result = await callback(reserved) if inspect.iscoroutinefunction(callback) else await asyncio.to_thread(callback, reserved)
        if result is False:
            raise ValueError("strategy link refused")
        return "READY"
    except Exception:
        await asyncio.to_thread(execution.release_unsubmitted, reserved["campaign"]["campaign_id"],
                                reserved["intent"].id, reason="STRATEGY_LINK_REFUSED")
        return "STRATEGY_LINK_REFUSED"


async def dispatch_reserved(execution, reserved, *, account_name, broker_factory=OneilBroker, now=None, before_submit=None, authorize_add=None):
    clock = _clock(now)
    before_status = await _before_submit(execution, reserved, before_submit, authorize_add)
    if before_status != "READY":
        return dict(status=before_status, campaign=await asyncio.to_thread(execution.snapshot, reserved["campaign"]["campaign_id"]))
    if execution.mode == "SHADOW":
        await asyncio.to_thread(execution.simulate_submission, reserved, now=clock())
        return dict(status="SHADOW_SIMULATED", campaign=await asyncio.to_thread(execution.snapshot, reserved["campaign"]["campaign_id"]))
    context = reserved["campaign"].get("context") or {}
    async with broker_factory(account_name, execution.store, exchange=context.get("exchange", "NASD")) as broker:
        return await _submit(execution, reserved, broker, clock, authorize_add)


async def drive(execution, cid, envelope, *, account_name, allow_add=True, broker_factory=OneilBroker, now=None, before_submit=None, authorize_add=None):
    clock = _clock(now)
    state = await asyncio.to_thread(execution.snapshot, cid)
    if state["status"] == "CLOSED":
        return dict(status="CLOSED", campaign=state)
    protection = envelope.get("protection") or {}
    quote = protection.get("quote")
    terminal = envelope.get("exit_event")
    # Latch before reconciliation so pending buys are canceled on this pass.
    # Full envelope validation still occurs in evaluate; do not trust an
    # unbound raw terminal to change execution authority.
    from prism_core.oneil_adaptive_policy import _hash
    bound = dict(plan=state["plan"], position_id=state["position_id"])
    if (envelope.get("contract_version") == "oneil-current-capture-v1"
            and envelope.get("record_hash") == _hash({k: v for k, v in envelope.items() if k != "record_hash"})
            and all(envelope.get(k) == v for k, v in dict(position_id=state["position_id"], symbol=state["symbol"],
                source_decision_ref=state["plan"]["source_decision_ref"], price_basis_ref=state["plan"]["setup"]["price_basis_ref"],
                plan_hash=state["plan"]["plan_hash"]).items())):
        exit_requested = False
        try:
            if terminal:
                OneilRuntime._bound(terminal, bound, _time(clock()), "available_at")
                exit_requested = _time(terminal["occurred_at"]) <= _time(clock())
            elif quote:
                OneilRuntime._bound(quote, bound, _time(clock()), "observed_at", True)
                exit_requested = _num(quote["price"], True) <= _num(state["current_stop"])
        except (KeyError, TypeError, ValueError):
            pass
        if exit_requested:
            reference = dict(at=terminal["occurred_at"] if terminal else envelope["occurred_at"],
                             price=terminal["price"] if terminal else quote["price"],
                             source_ref=terminal["source_ref"] if terminal else quote["source_ref"])
            await asyncio.to_thread(execution.request_exit, cid, reason="ORIGINAL_EXIT" if terminal else "PROTECTIVE_STOP", reference=reference)
    if execution.mode == "SHADOW":
        await _recover_shadow(execution, cid, clock)
        state = await asyncio.to_thread(execution.snapshot, cid)
        if state["status"] == "CLOSED":
            return dict(status="CLOSED", campaign=state)
        account = dict(status="OK", account_id=state["account_id"], symbol=state["symbol"],
                       quantity=state["confirmed_quantity"], observed_at=clock(), source_ref="SHADOW_SIMULATED_ACCOUNT")
        result = await asyncio.to_thread(execution.evaluate, cid, envelope, expected_revision=state["revision"],
                                         account_snapshot=account, allow_add=allow_add, evaluation_at=clock())
        if result.get("intent") is not None:
            return await dispatch_reserved(execution, result, account_name=account_name, now=clock, before_submit=before_submit, authorize_add=authorize_add)
        return result
    context = state.get("context") or {}
    async with broker_factory(account_name, execution.store, exchange=context.get("exchange", "NASD")) as broker:
        await _reconcile(execution, cid, broker, clock)
        state = await asyncio.to_thread(execution.snapshot, cid)
        if state["status"] == "CLOSED":
            return dict(status="CLOSED", campaign=state)
        account = await broker.holdings(state["symbol"])
        result = await asyncio.to_thread(execution.evaluate, cid, envelope, expected_revision=state["revision"],
                                         account_snapshot=account, allow_add=allow_add, evaluation_at=clock())
        if result.get("intent") is not None:
            before_status = await _before_submit(execution, result, before_submit, authorize_add)
            if before_status != "READY":
                return dict(status=before_status, campaign=await asyncio.to_thread(execution.snapshot, cid))
            return await _submit(execution, result, broker, clock, authorize_add)
        return result

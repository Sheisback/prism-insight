"""Owned account lifecycle with atomic reservations; no implicit LIVE activation.

The same reservation/reconciliation path serves SHADOW and LIVE. The caller
supplies an explicitly authorized LIVE broker adapter; this module has no network
client and never adopts an existing holding.
"""
from contextlib import contextmanager
from dataclasses import asdict
from decimal import Decimal, ROUND_FLOOR
import json

from prism_core.oneil_adaptive_policy import _hash, _num, _ref, _time, _validate, evaluate_target
from prism_core.oneil_runtime import OneilRuntime
from prism_core.order_intents import IntentStore, OrderIntent


class OneilExecution:
    def __init__(self, path, *, mode="SHADOW"):
        if mode not in {"SHADOW", "LIVE"}:
            raise ValueError("explicit SHADOW or LIVE mode required")
        self.mode, self.store = mode, IntentStore(path)
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS oneil_execution_config (id INTEGER PRIMARY KEY CHECK(id=1), mode TEXT NOT NULL)")
            row = db.execute("SELECT mode FROM oneil_execution_config WHERE id=1").fetchone()
            if row and row[0] != mode:
                raise ValueError("execution database mode is immutable")
            db.execute("INSERT OR IGNORE INTO oneil_execution_config VALUES (1,?)", (mode,))
            db.execute("CREATE TABLE IF NOT EXISTS oneil_accounts (id TEXT PRIMARY KEY, account_id TEXT NOT NULL, symbol TEXT NOT NULL, active INTEGER NOT NULL, data TEXT NOT NULL)")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS oneil_active_owner ON oneil_accounts(account_id,symbol) WHERE active=1")
            db.execute("CREATE TABLE IF NOT EXISTS oneil_orders (intent_id TEXT PRIMARY KEY REFERENCES order_intents(id), campaign_id TEXT NOT NULL REFERENCES oneil_accounts(id), data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS oneil_execution_events (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    @contextmanager
    def _transaction(self):
        db = self.store._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _get(db, cid):
        row = db.execute("SELECT data FROM oneil_accounts WHERE id=?", (cid,)).fetchone()
        if row is None:
            raise ValueError("unknown owned campaign")
        return json.loads(row[0])

    @staticmethod
    def _save(db, state):
        db.execute("UPDATE oneil_accounts SET active=?,data=? WHERE id=?",
                   (int(state["status"] != "CLOSED"), json.dumps(state, sort_keys=True), state["campaign_id"]))

    @staticmethod
    def _event(db, eid, payload):
        encoded = json.dumps(payload, sort_keys=True, allow_nan=False)
        row = db.execute("SELECT payload FROM oneil_execution_events WHERE id=?", (eid,)).fetchone()
        if row:
            if row[0] != encoded:
                raise ValueError("immutable execution event conflict")
            return False
        db.execute("INSERT INTO oneil_execution_events VALUES (?,?)", (eid, encoded))
        return True

    @staticmethod
    def _account(snapshot, account, symbol, now, quantity, *, allow_extra=False):
        if (snapshot.get("status") != "OK" or snapshot.get("account_id") != account
                or snapshot.get("symbol") != symbol or type(snapshot.get("quantity")) is not int
                or (snapshot["quantity"] < quantity if allow_extra else snapshot["quantity"] != quantity)):
            raise ValueError("confirmed exact owned account snapshot required")
        _ref(snapshot["source_ref"])
        if not 0 <= (_time(now) - _time(snapshot["observed_at"])).total_seconds() <= 120:
            raise ValueError("stale account snapshot")

    def claim_campaign(self, *, account_id, position_id, plan, unit_budget, account_snapshot, now, context=None):
        _validate(plan)
        _ref(account_id)
        _ref(position_id)
        budget = _num(unit_budget, True)
        frozen_context = self._context(context) if context is not None else None
        if _time(now) < _time(plan["created_at"]):
            raise ValueError("claim before plan")
        cid = _hash(["oneil-owned-execution-v1", self.mode, account_id, position_id, plan["plan_hash"]])
        with self._transaction() as db:
            old = db.execute("SELECT data FROM oneil_accounts WHERE id=?", (cid,)).fetchone()
            if old:
                state = json.loads(old[0])
                if _num(state["unit_budget"]) != budget:
                    raise ValueError("immutable account budget")
                if frozen_context is not None and state.get("context") != frozen_context:
                    raise ValueError("immutable execution context")
                return state
            self._account(account_snapshot, account_id, plan["symbol"], now, 0)
            if (account_snapshot.get("open_orders_status") != "OK"
                    or type(account_snapshot.get("open_orders_count")) is not int
                    or account_snapshot["open_orders_count"] != 0):
                raise ValueError("confirmed no existing orders required for ownership")
            state = dict(campaign_id=cid, account_id=account_id, position_id=position_id,
                         symbol=plan["symbol"], plan=plan, unit_budget=str(budget), mode=self.mode,
                         status="ACTIVE", revision=0, current_stop=plan["initial_stop"],
                         confirmed_quantity=0, confirmed_buy_notional="0", remaining_principal="0",
                         remaining_entry_fees="0", realized_pnl="0", last_add_bar_end=None,
                         fees_basis="SIMULATED_MODEL" if self.mode == "SHADOW" else "ACTUAL",
                         actual_fees_known=self.mode == "LIVE",
                         last_target="0", exit_reason=None, last_observed_at=now)
            if frozen_context is not None:
                state.update(context=frozen_context, context_hash=_hash(frozen_context))
            db.execute("INSERT INTO oneil_accounts VALUES (?,?,?,?,?)", (cid, account_id, plan["symbol"], 1, json.dumps(state)))
            if frozen_context is not None:
                self._event(db, _hash([cid, "CONTEXT"]), frozen_context)
            return state

    def snapshot(self, cid):
        with self._transaction() as db:
            state = self._get(db, cid)
            orders = [json.loads(r[0]) for r in db.execute("SELECT data FROM oneil_orders WHERE campaign_id=?", (cid,))]
            return dict(state, orders=orders)

    def list_campaigns(self, *, active_only=True):
        with self._transaction() as db:
            if active_only:
                rows = db.execute("SELECT data FROM oneil_accounts WHERE active=1")
            else:
                rows = db.execute("SELECT data FROM oneil_accounts")
            return [json.loads(r[0]) for r in rows]

    def owner_by_symbol(self, account_id, symbol):
        with self._transaction() as db:
            row = db.execute("SELECT data FROM oneil_accounts WHERE account_id=? AND symbol=? AND active=1",
                             (account_id, symbol)).fetchone()
            return json.loads(row[0]) if row else None

    def link_strategy_position(self, cid, position_id):
        _ref(position_id)
        with self._transaction() as db:
            state = self._get(db, cid)
            if state.get("strategy_position_id") not in (None, position_id):
                raise ValueError("immutable strategy position link")
            state["strategy_position_id"] = position_id
            self._save(db, state)
            return state

    def mark_strategy_exit(self, cid, *, at, source_ref):
        """Record independent strategy finalization; broker state is unchanged."""
        stamp = _time(at).isoformat()
        _ref(source_ref)
        with self._transaction() as db:
            state = self._get(db, cid)
            if _time(stamp) < _time(state["plan"]["created_at"]):
                raise ValueError("strategy exit precedes plan")
            marker = dict(at=stamp, source_ref=source_ref)
            previous = state.get("strategy_exit_recorded")
            if previous is not None and previous != marker:
                raise ValueError("immutable strategy exit marker")
            self._event(db, _hash([cid, "STRATEGY_EXIT"]), marker)
            state["strategy_exit_recorded"] = marker
            self._save(db, state)
            return state

    @staticmethod
    def _context(context):
        allowed = {"scenario", "account_name", "entry_slot_context", "exchange", "source_capture_id",
                   "company_name", "rank_change_msg", "report_path"}
        allowed.update({"max_slots", "source_position_id"})
        if not isinstance(context, dict) or set(context) - allowed:
            raise ValueError("unsupported execution context")
        encoded = json.dumps(context, sort_keys=True, allow_nan=False)
        if len(encoded.encode()) > 32768:
            raise ValueError("bounded execution context required")
        return json.loads(encoded)

    def attach_context(self, cid, context):
        """Freeze bounded non-secret recovery context; never accept credentials."""
        context = self._context(context)
        with self._transaction() as db:
            state = self._get(db, cid)
            if "context" in state and state["context"] != context:
                raise ValueError("immutable execution context")
            state["context"] = context
            state["context_hash"] = _hash(state["context"])
            self._event(db, _hash([cid, "CONTEXT"]), state["context"])
            self._save(db, state)
            return state

    @staticmethod
    def _pending(db, cid):
        return [o for o in (json.loads(r[0]) for r in db.execute("SELECT data FROM oneil_orders WHERE campaign_id=?", (cid,)))
                if o["status"] not in {"FILLED", "CANCELLED", "REJECTED"}]

    def _cancel_unsubmitted(self, db, cid):
        # A reservation capability cannot be claimed after this CAS. If the
        # dispatcher already claimed SUBMITTING, leave it pending for broker
        # cancellation/reconciliation; a local ACK is never a broker cancel.
        for order in self._pending(db, cid):
            if order["intent"]["side"] != "BUY":
                continue
            intent_id = order["intent"]["id"]
            changed = db.execute("UPDATE order_intents SET status='CANCELLED' WHERE id=? AND status='CREATED'", (intent_id,)).rowcount
            if changed:
                order.update(status="CANCELLED", reserved_notional="0", cancellation_basis="NEVER_SUBMITTED_LOCAL_CAS")
                db.execute("UPDATE oneil_orders SET data=? WHERE intent_id=?", (json.dumps(order), intent_id))

    @staticmethod
    def _exit_reference(state, reference):
        if reference is None:
            return
        stamp = _time(reference["at"])
        if stamp < _time(state["plan"]["created_at"]):
            raise ValueError("strategy exit reference precedes plan")
        price = str(_num(reference["price"], True))
        _ref(reference["source_ref"])
        if "strategy_exit_reference" not in state:
            state["strategy_exit_reference"] = dict(at=stamp.isoformat(), price=price, source_ref=reference["source_ref"])

    def request_exit(self, cid, *, reason, reference=None):
        _ref(reason)
        with self._transaction() as db:
            state = self._get(db, cid)
            self._exit_reference(state, reference)
            if state["status"] == "CLOSED":
                self._save(db, state)
                return dict(state, pending_orders=[])
            if state["status"] != "EXIT_PENDING":
                state.update(status="EXIT_PENDING", exit_reason=reason, revision=state["revision"] + 1)
            self._save(db, state)
            self._cancel_unsubmitted(db, cid)
            return dict(state, pending_orders=self._pending(db, cid))

    def release_unsubmitted(self, cid, intent_id, *, reason):
        """Release only a still-CREATED intent after a preflight refusal."""
        _ref(reason)
        with self._transaction() as db:
            state = self._get(db, cid)
            row = db.execute("SELECT data FROM oneil_orders WHERE campaign_id=? AND intent_id=?", (cid, intent_id)).fetchone()
            if row is None:
                raise ValueError("unknown owned reservation")
            order = json.loads(row[0])
            changed = db.execute("UPDATE order_intents SET status='CANCELLED' WHERE id=? AND status='CREATED'", (intent_id,)).rowcount
            if not changed:
                raise ValueError("submitted or uncertain reservation cannot be released")
            order.update(status="CANCELLED", reserved_notional="0", cancellation_basis="PREFLIGHT_NOT_SUBMITTED", cancellation_reason=reason)
            db.execute("UPDATE oneil_orders SET data=? WHERE intent_id=?", (json.dumps(order), intent_id))
            state["revision"] += 1
            self._save(db, state)
            return state

    def evaluate(self, cid, envelope, *, expected_revision, account_snapshot, allow_add=True, evaluation_at=None):
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("nonnegative revision required")
        if (envelope.get("contract_version") != "oneil-current-capture-v1"
                or envelope.get("record_hash") != _hash({k: v for k, v in envelope.items() if k != "record_hash"})):
            raise ValueError("current envelope integrity required")
        now = evaluation_at or envelope["occurred_at"]
        if _time(now) < _time(envelope["occurred_at"]):
            raise ValueError("evaluation precedes source observation")
        with self._transaction() as db:
            state = self._get(db, cid)
            plan = state["plan"]
            for key, expected in dict(position_id=state["position_id"], symbol=state["symbol"],
                                      source_decision_ref=plan["source_decision_ref"], plan_hash=plan["plan_hash"],
                                      price_basis_ref=plan["setup"]["price_basis_ref"]).items():
                if envelope.get(key) != expected:
                    raise ValueError("owned observation identity mismatch")
            eid = _hash([cid, envelope["record_hash"]])
            if not self._event(db, eid, envelope):
                return dict(status="DUPLICATE", campaign=state, intent=None)
            if state["revision"] != expected_revision or _time(now) < _time(state["last_observed_at"]):
                raise ValueError("execution revision or chronology conflict")
            if state["status"] == "CLOSED":
                raise ValueError("closed execution campaign")
            bound = dict(plan=plan, position_id=state["position_id"])
            quote = (envelope.get("protection") or {}).get("quote")
            stop = (envelope.get("protection") or {}).get("stop")
            valid = envelope.get("status") == "OK" and isinstance(envelope.get("tick"), dict)
            if stop:
                OneilRuntime._bound(stop, bound, _time(now), "available_at")
                if _num(stop["current_stop"], True) < _num(state["current_stop"]):
                    valid = False
                state["current_stop"] = str(max(_num(state["current_stop"]), _num(stop["current_stop"], True)))
            if quote:
                OneilRuntime._bound(quote, bound, _time(now), "observed_at", True)
                _num(quote["price"], True)
            terminal = envelope.get("exit_event")
            if terminal:
                OneilRuntime._bound(terminal, bound, _time(now), "available_at")
            if terminal or (quote and _num(quote["price"]) <= _num(state["current_stop"])):
                state.update(status="EXIT_PENDING", exit_reason="ORIGINAL_EXIT" if terminal else "PROTECTIVE_STOP")
                self._exit_reference(state, dict(at=terminal["occurred_at"] if terminal else now,
                                                price=terminal["price"] if terminal else quote["price"],
                                                source_ref=terminal["source_ref"] if terminal else quote["source_ref"]))
                self._cancel_unsubmitted(db, cid)
            state.update(revision=expected_revision + 1, last_observed_at=now)
            pending = self._pending(db, cid)
            pending_sells = [order for order in pending if order["intent"]["side"] == "SELL"]
            if pending and (state["status"] != "EXIT_PENDING" or pending_sells):
                self._save(db, state)
                return dict(status="RECONCILE_REQUIRED", campaign=state, pending_orders=pending, intent=None)
            if (not pending and state["confirmed_quantity"] == 0
                    and _num(state["confirmed_buy_notional"]) == 0
                    and _time(now) >= _time(plan["expires_at"])):
                state.update(status="CLOSED", exit_reason="NO_ENTRY_EXPIRED")
                self._save(db, state)
                return dict(status="NO_ENTRY_EXPIRED", campaign=state, intent=None)
            if state["status"] == "EXIT_PENDING" and state["confirmed_quantity"] == 0 and not pending:
                state["status"] = "CLOSED"
                self._save(db, state)
                return dict(status="CLOSED_NO_HOLDING", campaign=state, intent=None)
            try:
                self._account(account_snapshot, state["account_id"], state["symbol"], now, state["confirmed_quantity"],
                              allow_extra=state["status"] == "EXIT_PENDING")
            except (ValueError, KeyError, TypeError):
                self._save(db, state)
                return dict(status="ACCOUNT_UNKNOWN_OR_FOREIGN", campaign=state, intent=None)
            if not quote:
                self._save(db, state)
                return dict(status="MISSING_QUOTE", campaign=state, intent=None)
            if state["status"] == "EXIT_PENDING" and state["confirmed_quantity"] == 0:
                self._save(db, state)
                return dict(status="RECONCILE_REQUIRED", campaign=state, pending_orders=pending, intent=None)
            price, budget = _num(quote["price"], True), _num(state["unit_budget"], True)
            if price != price.quantize(Decimal(".01")):
                self._save(db, state)
                return dict(status="UNSUPPORTED_PRICE_PRECISION", campaign=state, intent=None)
            decision = None
            if state["status"] == "EXIT_PENDING":
                side, quantity, target = "SELL", state["confirmed_quantity"], state["last_target"]
            else:
                if allow_add is not True:
                    self._save(db, state)
                    return dict(status="ADDS_DISABLED", campaign=state, intent=None)
                if not valid or stop is None:
                    self._save(db, state)
                    return dict(status="MISSING_ADD_INPUT", campaign=state, intent=None)
                tick = envelope["tick"]
                OneilRuntime._bound(tick, bound, _time(now), "available_at")
                facts = tick["evidence"]
                if ({k: quote[k] for k in ("price", "observed_at", "source_ref")} != facts.get("quote")
                        or _num(tick["current_stop"]) != _num(stop["current_stop"])):
                    raise ValueError("tick quote/stop binding mismatch")
                decision = evaluate_target(plan, facts, now=now,
                    cumulative_allocation=_num(state["confirmed_buy_notional"]) / budget,
                    remaining_allocation=_num(state["remaining_principal"]) / budget,
                    normalized_units=Decimal(state["confirmed_quantity"]) / budget,
                    remaining_entry_cost=_num(state["remaining_entry_fees"]) / budget,
                    current_stop=state["current_stop"], last_add_bar_end=state["last_add_bar_end"])
                if decision["action"] != "ADD":
                    self._save(db, state)
                    return dict(status="WAIT", campaign=state, decision=decision, intent=None)
                target = decision["target_allocation"]
                available = budget * _num(target) - _num(state["confirmed_buy_notional"])
                quantity = int((max(Decimal(0), available) / price).to_integral_value(rounding=ROUND_FLOOR))
                side = "BUY"
                if not quantity:
                    self._save(db, state)
                    return dict(status="BELOW_ONE_SHARE", campaign=state, decision=decision, intent=None)
            token = _hash([cid, side, decision["bar_end"] if decision else state["revision"]])
            intent = OrderIntent.create(market="US", account_id=state["account_id"], symbol=state["symbol"],
                side=side, order_style="limit", source="oneil-adaptive-v1", source_decision_id=token,
                source_position_id=token if side == "SELL" else state["position_id"],
                execution_mode=self.mode.lower(), quantity=quantity, limit_price=price, reason=state["exit_reason"] or "ADAPTIVE_TARGET")
            inserted, reservation = self.store.reserve_in_transaction(db, intent)
            if not inserted:
                raise ValueError("owned intent reservation conflict")
            order = dict(intent=asdict(intent), status="CREATED", filled_quantity=0, filled_notional="0", fees="0",
                         reserved_at=now,
                         reserved_notional=str(price * quantity) if side == "BUY" else "0",
                         broker_order_id=None, broker_order_date=None, target_allocation=target,
                         last_receipt_at=None)
            db.execute("INSERT INTO oneil_orders VALUES (?,?,?)", (intent.id, cid, json.dumps(order)))
            if side == "BUY":
                state.update(last_add_bar_end=decision["bar_end"], last_target=target)
            self._save(db, state)
            return dict(status="RESERVED", campaign=state, decision=decision, intent=intent, reservation=reservation)

    def reconcile(self, cid, intent_id, receipt):
        with self._transaction() as db:
            state = self._get(db, cid)
            row = db.execute("SELECT data FROM oneil_orders WHERE intent_id=? AND campaign_id=?", (intent_id, cid)).fetchone()
            if row is None:
                raise ValueError("unknown owned order")
            order = json.loads(row[0])
            intent = order["intent"]
            for key in ("account_id", "symbol", "side"):
                if receipt.get(key) != intent[key]:
                    raise ValueError("broker receipt identity mismatch")
            if receipt.get("intent_id") != intent_id or receipt.get("virtual") is not (self.mode == "SHADOW"):
                raise ValueError("broker receipt mode or intent mismatch")
            at = _time(receipt["observed_at"])
            if order["last_receipt_at"] and at < _time(order["last_receipt_at"]):
                raise ValueError("receipt chronology conflict")
            status = receipt.get("status")
            if status not in {"UNKNOWN", "PENDING", "PARTIAL", "FILLED", "CANCELLED", "REJECTED"}:
                raise ValueError("unsupported reconciliation status")
            if order["status"] in {"FILLED", "CANCELLED", "REJECTED"} and status != order["status"]:
                raise ValueError("terminal receipt conflict")
            broker_id = receipt.get("broker_order_id")
            acknowledgements = db.execute("SELECT broker_order_id FROM broker_orders WHERE intent_id=? AND accepted=1", (intent_id,)).fetchall()
            broker_rejected = status == "REJECTED" and db.execute(
                "SELECT 1 FROM order_intents WHERE id=? AND status IN ('REJECTED','FAILED')", (intent_id,)).fetchone()
            confirmed_fill = receipt.get("fill_evidence_status") == "CONFIRMED"
            if (status != "UNKNOWN" or confirmed_fill) and not broker_rejected and not any(r[0] == broker_id and broker_id for r in acknowledgements):
                raise ValueError("receipt lacks exact accepted broker order")
            if status == "UNKNOWN" and not confirmed_fill:
                order.update(status="UNKNOWN", last_receipt_at=receipt["observed_at"])
            else:
                _ref(receipt["broker_order_date"])
                if order["broker_order_id"] not in (None, broker_id) or order["broker_order_date"] not in (None, receipt["broker_order_date"]):
                    raise ValueError("broker order identity changed")
                qty = receipt["filled_quantity"]
                notional = _num(receipt["filled_notional"])
                actual_fees = receipt["fees"]
                if actual_fees is None and receipt.get("fees_status") != "UNKNOWN":
                    raise ValueError("explicit unknown fees required")
                fees = notional * _num(state["plan"]["fee_rate"]) if actual_fees is None else _num(actual_fees)
                if actual_fees is None:
                    state.update(actual_fees_known=False, fees_basis="MODEL_PLAN_RATE_BROKER_PNL_UNKNOWN")
                if actual_fees is not None and order.get("actual_fees") is not None and _num(actual_fees) < _num(order["actual_fees"]):
                    raise ValueError("actual cumulative fees decreased")
                # Learning a smaller actual fee cannot erase already-reserved
                # model risk or prevent recognition of newly confirmed shares.
                fees = max(fees, _num(order["fees"]))
                if (type(qty) is not int or not order["filled_quantity"] <= qty <= intent["quantity"]
                        or notional < _num(order["filled_notional"]) or fees < _num(order["fees"])
                        or (qty == 0 and (notional or fees)) or (qty > 0 and not notional)
                        or (status == "FILLED" and qty != intent["quantity"])
                        or (status == "PENDING" and qty != order["filled_quantity"])
                        or (status == "REJECTED" and qty != 0)):
                    raise ValueError("invalid cumulative execution totals")
                if order["status"] in {"FILLED", "CANCELLED", "REJECTED"}:
                    if (status != order["status"] or qty != order["filled_quantity"]
                            or notional != _num(order["filled_notional"]) or fees != _num(order["fees"])):
                        raise ValueError("terminal receipt conflict")
                    return state
                delta_qty = qty - order["filled_quantity"]
                delta_notional = notional - _num(order["filled_notional"])
                delta_fees = fees - _num(order["fees"])
                if not delta_qty and delta_notional:
                    raise ValueError("notional changed without fill")
                if intent["side"] == "BUY":
                    if notional > _num(intent["limit_price"]) * qty:
                        raise ValueError("buy fill exceeds reserved limit")
                    state["confirmed_quantity"] += delta_qty
                    for key, delta in (("confirmed_buy_notional", delta_notional), ("remaining_principal", delta_notional), ("remaining_entry_fees", delta_fees)):
                        state[key] = str(_num(state[key]) + delta)
                else:
                    held = state["confirmed_quantity"]
                    if delta_qty > held:
                        raise ValueError("sell exceeds confirmed owned shares")
                    ratio = Decimal(delta_qty) / held if held else Decimal(0)
                    cost, entry_fee = _num(state["remaining_principal"]) * ratio, _num(state["remaining_entry_fees"]) * ratio
                    state.update(confirmed_quantity=held - delta_qty,
                                 remaining_principal=str(_num(state["remaining_principal"]) - cost),
                                 remaining_entry_fees=str(_num(state["remaining_entry_fees"]) - entry_fee),
                                 realized_pnl=str(Decimal(state["realized_pnl"]) + delta_notional - cost - entry_fee - delta_fees))
                order.update(status=status, filled_quantity=qty, filled_notional=str(notional), fees=str(fees),
                             actual_fees=None if actual_fees is None else str(actual_fees),
                             fees_basis="SIMULATED_MODEL" if self.mode == "SHADOW" else "MODEL_PLAN_RATE" if actual_fees is None else "ACTUAL",
                             broker_order_id=broker_id, broker_order_date=receipt["broker_order_date"], last_receipt_at=receipt["observed_at"],
                             reserved_notional="0" if status in {"FILLED", "CANCELLED", "REJECTED"} else str(_num(intent["limit_price"]) * (intent["quantity"] - qty)))
                state["revision"] += 1
            db.execute("UPDATE oneil_orders SET data=? WHERE intent_id=?", (json.dumps(order), intent_id))
            if state["status"] == "EXIT_PENDING" and state["confirmed_quantity"] == 0 and not self._pending(db, cid):
                state["status"] = "CLOSED"
            self._event(db, _hash([intent_id, receipt]), receipt)
            self._save(db, state)
            return state

    def simulate_submission(self, reserved, *, now):
        if self.mode != "SHADOW":
            raise ValueError("virtual broker only allowed in SHADOW")
        intent, reservation = reserved["intent"], reserved["reservation"]
        self.store.claim_reservation(reservation, intent, expected_side=intent.side)
        order_id = "SHADOW:" + intent.id
        self.store.record_result(intent, status="SUBMITTED", accepted=True, broker="SHADOW",
                                 response=dict(order_no=order_id, quantity=intent.quantity, price=intent.limit_price))
        notional = _num(intent.limit_price) * intent.quantity
        state = reserved["campaign"]
        return self.reconcile(state["campaign_id"], intent.id, dict(intent_id=intent.id,
            account_id=intent.account_id, symbol=intent.symbol, side=intent.side, virtual=True,
            broker_order_id=order_id, broker_order_date=_time(now).date().isoformat(),
            filled_quantity=intent.quantity, filled_notional=str(notional),
            fees=str(notional * _num(state["plan"]["fee_rate"])), status="FILLED", observed_at=now))

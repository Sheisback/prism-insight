"""Atomic paired SHADOW execution on StrategyLedger, with no broker interface.

Intent candidates are normalized research allocations, never order quantities or
live authorization. Current-capture hashes establish integrity, not authenticity.
"""
from copy import deepcopy
from decimal import Decimal
import json

from prism_core.oneil_adaptive_policy import (
    VERSION, VERSIONS, _hash, _num, _ref, _time, _validate, evaluate_target, evidence_version,
)
from prism_core.scenario_shadow_policy import _validate_plan
from prism_core.strategy_ledger import StrategyLedger, LedgerError, _time as ledger_time

# New campaigns are owned by the current policy. Each campaign keeps the owner
# of its frozen plan version, so existing v1 books stay readable and unchanged.
OWNER = VERSION
OWNERS = VERSIONS


class OneilRuntime:
    def __init__(self, path, *, initial_arm="INITIAL_POLICY_50"):
        if initial_arm not in {"INITIAL_POLICY_50", "COMPATIBILITY_SCOUT_10"}:
            raise LedgerError("registered initial arm required")
        self.initial_arm = initial_arm
        self.ledger = StrategyLedger(path)
        with self.ledger._transaction() as db:
            for row in db.execute("SELECT data FROM books"):
                cohort = json.loads(row[0]).get("cohort") or ""
                for owner in OWNERS:
                    if cohort.startswith(owner + ":"):
                        is_fifty = cohort.startswith(owner + ":initial-policy-50-v1:")
                        if is_fifty != (initial_arm == "INITIAL_POLICY_50"):
                            raise LedgerError("existing initial arm cannot be adopted")
            self.ledger._event(db, "oneil-runtime:configuration", dict(initial_arm=initial_arm))

    def campaign_id_for_position(self, position_id, policy_version=OWNER):
        if policy_version not in OWNERS:
            raise LedgerError("registered adaptive policy version required")
        identity = [policy_version, _ref(position_id)]
        if self.initial_arm == "INITIAL_POLICY_50":
            identity.append("initial-policy-50-v1")
        return _hash(identity)

    def _cohort(self, cid, arm, owner):
        variant = "initial-policy-50-v1:" if self.initial_arm == "INITIAL_POLICY_50" else ""
        return owner + ":" + variant + cid + ":" + arm

    def _target(self, db, event, cid, arm, plan, target, price, at):
        owner = plan["policy_version"]
        return self.ledger._apply_target_in_transaction(db, event, dict(
            kind="target", book_id=cid + ":" + arm, campaign_id=cid + ":" + arm,
            symbol=plan["symbol"], target_pct=str(target), price=str(price),
            occurred_at=ledger_time(at), policy_version=owner, reason=arm,
            regime="paired_shadow", source_hash=plan["plan_hash"],
            fee_rate=plan["fee_rate"] if target else "0", slippage_rate="0"), owner=owner, notify=False)

    def open_capture(self, capture):
        capture = deepcopy(capture)
        attrs = capture["attributes"]
        if (capture["market"] != "US" or attrs.get("capture_schema_version") != 1
                or attrs.get("phase") != "POST_STRATEGY_COMMIT_PRE_BROKER"
                or attrs.get("confirmed_fill") is not False or attrs.get("trading_impact") != "none"):
            raise LedgerError("original non-broker capture required")
        for name in ("position_id", "decision_id", "ticker", "event_id"):
            _ref(capture[name])
        original = attrs["plan"]
        _validate_plan(original)
        setup = attrs.get("adaptive_setup") or {}
        if setup.get("status") != "OK":
            raise LedgerError("frozen adaptive plan unavailable")
        plan = setup["plan"]
        _validate(plan)
        at = _time(capture["event_time"])
        if (plan["symbol"] != capture["ticker"] or plan["source_decision_ref"] != capture["decision_id"]
                or original["source_decision_hash"] != _hash(capture["decision_id"])
                or _time(original["entry_at"]) != at or _time(plan["created_at"]) != at
                or _num(original["entry_price"]) != _num(plan["entry_reference"])
                or _num(original["initial_stop"]) != _num(plan["initial_stop"])):
            raise LedgerError("capture identity mismatch")
        owner = plan["policy_version"]
        cid = self.campaign_id_for_position(capture["position_id"], owner)
        # Provisioning is idempotent; both positions and their ownership state
        # are opened together in the subsequent single accounting transaction.
        for arm in ("baseline", "adaptive"):
            self.ledger.create_book(cid + ":" + arm, "US", 1,
                                    cohort=self._cohort(cid, arm, owner), mode="SHADOW")
        with self.ledger._transaction() as db:
            event = cid + ":open"
            if self.ledger._event(db, event, dict(kind="adaptive_open", capture=capture)):
                initial = 0 if self.initial_arm == "INITIAL_POLICY_50" else 10
                for arm, target in (("baseline", 100), ("adaptive", initial)):
                    self._target(db, event + ":" + arm, cid, arm, plan, target, plan["entry_reference"], at.isoformat())
                state = dict(plan=plan, position_id=capture["position_id"], revision=0, initial_arm=self.initial_arm,
                             entry_status="PENDING" if not initial else "ENTERED",
                             current_stop=plan["initial_stop"], last_add_bar_end=None,
                             missing_observations=0, closed=False, last_observed_at=at.isoformat(),
                             latest_input_status="NOT_OBSERVED", capture_hash=_hash(capture))
                self._save(db, cid, state)
            return self._snapshot(db, cid)

    def _load(self, db, cid):
        campaign = self.ledger._get(db, "campaigns", cid + ":adaptive")
        owner = campaign["oneil_runtime"]["plan"]["policy_version"]
        if owner not in OWNERS:
            raise LedgerError("registered adaptive policy version required")
        for arm in ("baseline", "adaptive"):
            book = self.ledger._get(db, "books", cid + ":" + arm)
            if book["mode"] != "SHADOW" or book["cohort"] != self._cohort(cid, arm, owner):
                raise LedgerError("dedicated paired SHADOW books required")
        return campaign, campaign["oneil_runtime"]

    def _save(self, db, cid, state):
        campaign = self.ledger._get(db, "campaigns", cid + ":adaptive")
        campaign["oneil_runtime"] = state
        self.ledger._save(db, "campaigns", cid + ":adaptive", campaign)

    def _snapshot(self, db, cid):
        _, state = self._load(db, cid)
        arms = {arm: self.ledger._get(db, "campaigns", cid + ":" + arm)
                for arm in ("baseline", "adaptive")}
        return dict(campaign_id=cid, mode="SHADOW", adaptive_arm=self.initial_arm,
                    broker_execution=False, live_ready=False, revision=state["revision"],
                    state=deepcopy(state), arms=arms, state_hash=_hash(arms))

    def snapshot(self, campaign_id):
        with self.ledger._transaction() as db:
            return self._snapshot(db, campaign_id)

    @staticmethod
    def _bound(value, state, at, clock, fresh=False):
        plan = state["plan"]
        identity = dict(symbol=plan["symbol"], position_id=state["position_id"],
                        source_decision_ref=plan["source_decision_ref"],
                        price_basis_ref=plan["setup"]["price_basis_ref"])
        if not isinstance(value, dict) or any(value.get(k) != v for k, v in identity.items()):
            raise LedgerError("source identity mismatch")
        _ref(value["source_ref"])
        age = (at - _time(value[clock])).total_seconds()
        if age < 0 or (fresh and age > 120):
            raise LedgerError("source clock invalid")

    def advance(self, campaign_id, envelope, *, expected_revision):
        if type(expected_revision) is not int or expected_revision < 0:
            raise LedgerError("nonnegative revision required")
        envelope = deepcopy(envelope)
        if len(json.dumps(envelope, allow_nan=False).encode()) > 131072:
            raise LedgerError("bounded observation required")
        content = {k: v for k, v in envelope.items() if k != "record_hash"}
        if (envelope.get("contract_version") != "oneil-current-capture-v1"
                or envelope.get("record_hash") != _hash(content)):
            raise LedgerError("observation integrity mismatch")
        cid, at = campaign_id, _time(envelope["occurred_at"])
        event = cid + ":input:" + envelope["record_hash"]
        with self.ledger._transaction() as db:
            campaign, state = self._load(db, cid)
            plan = state["plan"]
            for key, value in dict(position_id=state["position_id"], symbol=plan["symbol"],
                                   source_decision_ref=plan["source_decision_ref"],
                                   price_basis_ref=plan["setup"]["price_basis_ref"], plan_hash=plan["plan_hash"]).items():
                if envelope.get(key) != value:
                    raise LedgerError("observation identity mismatch")
            if not self.ledger._event(db, event, dict(kind="adaptive_input", envelope=envelope)):
                return dict(self._snapshot(db, cid), event_applied=False)
            if state["revision"] != expected_revision:
                raise LedgerError("adaptive revision conflict")
            if at < _time(state["last_observed_at"]):
                raise LedgerError("observation chronology conflict")
            if state["closed"]:
                raise LedgerError("closed campaign")
            previous_hash = _hash(campaign)
            protection = envelope.get("protection") or {}
            quote, stop = protection.get("quote"), protection.get("stop")
            regressive_stop = False
            if quote is not None:
                self._bound(quote, state, at, "observed_at", True)
                _num(quote["price"], True)
            if stop is not None:
                self._bound(stop, state, at, "available_at")
                # Preserve protection but do not size from a contradictory stop.
                regressive_stop = _num(stop["current_stop"], True) < _num(state["current_stop"])
                state["current_stop"] = str(max(_num(state["current_stop"]), _num(stop["current_stop"], True)))
            facts = dict(contract_version=evidence_version(plan), symbol=plan["symbol"],
                         price_basis_ref=plan["setup"]["price_basis_ref"], source_ref=event, quote=quote)
            tick = envelope.get("tick")
            valid = envelope.get("status") == "OK" and isinstance(tick, dict) and not regressive_stop
            if valid:
                self._bound(tick, state, at, "available_at")
                if (_time(tick["occurred_at"]) != at or quote is None
                        or {k: quote[k] for k in ("price", "observed_at", "source_ref")} != tick["evidence"].get("quote")
                        or stop is None or _num(tick["current_stop"]) != _num(stop["current_stop"])):
                    raise LedgerError("tick protection binding mismatch")
                facts = tick["evidence"]
            decision = evaluate_target(plan, facts, now=at.isoformat(),
                cumulative_allocation=campaign["cumulative_deployed_allocation"],
                remaining_allocation=campaign["remaining_allocation"],
                normalized_units=campaign["normalized_units"], remaining_entry_cost=campaign["remaining_entry_cost"],
                current_stop=state["current_stop"], last_add_bar_end=state["last_add_bar_end"],
                add_permission="AVAILABLE" if valid else "INPUT_UNAVAILABLE")
            terminal = envelope.get("exit_event")
            exit_price = None
            exit_at = at
            if terminal is not None:
                self._bound(terminal, state, at, "available_at")
                terminal_at = _time(terminal["occurred_at"])
                accounting_at = max(_time(self.ledger._get(db, "campaigns", cid + ":" + arm)["last_event_at"])
                                    for arm in ("baseline", "adaptive"))
                # Deferred persistence may follow a later missing-data poll.
                # Poll time is not accounting time, but an already-booked ADD
                # after this exit is a real conflict: never reprice that exit.
                if not accounting_at <= terminal_at <= at:
                    raise LedgerError("TERMINAL_ACCOUNTING_CHRONOLOGY_CONFLICT")
                exit_price = str(_num(terminal["price"], True))
                exit_at = _time(terminal["occurred_at"])
                decision = dict(decision, action="ORIGINAL_EXIT", reason="ORIGINAL_TERMINAL_EXIT", price=exit_price)
            elif decision["action"] == "PROTECTIVE_EXIT_REQUIRED":
                exit_price = decision["price"]
            elif quote is not None and _num(quote["price"]) <= _num(state["current_stop"]):
                # Baseline is already exposed even while adaptive is pending.
                exit_price = str(_num(quote["price"], True))
                decision = dict(decision, action="PROTECTIVE_EXIT_REQUIRED", reason="COMMON_PROTECTIVE_STOP", price=exit_price)
            if decision["evidence_status"] == "MISSING":
                valid = False
            if not valid and terminal is None:
                state["missing_observations"] += 1
            state["latest_input_status"] = "TERMINAL" if terminal is not None else "OK" if valid else "MISSING"
            intent = None
            if exit_price is not None:
                for arm in ("baseline", "adaptive"):
                    held = self.ledger._get(db, "campaigns", cid + ":" + arm)
                    if _num(held["normalized_units"]) == 0:
                        state["entry_status"] = "NO_ENTRY"
                        continue
                    self.ledger._sell_in_transaction(db, event + ":exit:" + arm, dict(
                        kind="sell", campaign_id=cid + ":" + arm, price=exit_price,
                        occurred_at=ledger_time(exit_at.isoformat()), normalized_units=None,
                        fee_rate=plan["fee_rate"], slippage_rate="0", source_hash=envelope["record_hash"]), owner=plan["policy_version"])
                state["closed"] = True
            elif decision["action"] == "ADD":
                action_id = cid + ":add:" + _hash(decision["bar_end"])
                self._target(db, action_id, cid, "adaptive", plan,
                             Decimal(decision["target_allocation"]) * 100, decision["price"], at.isoformat())
                state["last_add_bar_end"] = decision["bar_end"]
                state["entry_status"] = "ENTERED"
                intent = dict(contract_version="oneil-intent-candidate-v1", intent_id=action_id,
                              campaign_id=cid, position_id=state["position_id"], symbol=plan["symbol"],
                              source_decision_ref=plan["source_decision_ref"], plan_hash=plan["plan_hash"],
                              state_hash=previous_hash, input_hash=envelope["record_hash"],
                              occurred_at=at.isoformat(), quote_observed_at=quote["observed_at"],
                              revision=expected_revision, decision=decision, mode="SHADOW",
                              initial_arm=self.initial_arm,
                              execution_authorized=False, broker_quantity=None)
                intent["intent_hash"] = _hash(intent)
                self.ledger._event(db, action_id + ":candidate", intent)
            state.update(revision=expected_revision + 1, last_observed_at=at.isoformat())
            self.ledger._event(db, event + ":decision", dict(kind="adaptive_decision", decision=decision))
            self._save(db, cid, state)
            return dict(self._snapshot(db, cid), event_applied=True, decision=decision, intent=intent)

    def intent(self, intent_id):
        with self.ledger._transaction() as db:
            row = db.execute("SELECT payload FROM events WHERE id=?", (intent_id + ":candidate",)).fetchone()
            if row is None:
                raise LedgerError("unknown intent candidate")
            return json.loads(row[0])

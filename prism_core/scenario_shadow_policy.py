"""Pure, isolated scenario SHADOW policy; caller attestations are not authentication.

Allocation is cumulative principal in slots, units are fractional shares per slot.
Persist the returned state and virtual leg in one transaction. No broker semantics
or real-fill assumptions belong here. All prices must share one adjustment basis.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json

VERSION = "scenario-shadow-v1"
GATES = ("risk", "regime", "sector", "slot")


def _number(value, *, positive=False):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("finite number required") from None
    if isinstance(value, bool) or not result.is_finite() or result < 0 or (positive and not result):
        raise ValueError("finite nonnegative number required")
    return result


def _clock(value):
    if not isinstance(value, str):
        raise ValueError("aware timestamp required")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError("aware timestamp required")
    return result.astimezone(timezone.utc)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _seal(plan):
    plan = deepcopy(plan)
    plan.pop("plan_hash", None)
    plan["plan_hash"] = _hash(plan)
    return plan


def _validate_plan(plan):
    if not isinstance(plan, dict) or plan.get("policy_version") != VERSION or plan.get("mode") != "SHADOW":
        raise ValueError("owned SHADOW plan required")
    if _seal(plan)["plan_hash"] != plan.get("plan_hash"):
        raise ValueError("plan hash mismatch")


def create_plan(*, entry_price, initial_stop, entry_at, source_decision_ref,
                entry_eligible, source="regular", market="US", version=VERSION):
    """Create only from explicit original eligible scenario; never infer E or S."""
    if source != "regular" or market != "US" or version != VERSION or entry_eligible is not True:
        raise ValueError("eligible regular US source required")
    if not isinstance(source_decision_ref, str) or not source_decision_ref.strip():
        raise ValueError("source decision reference required")
    entry, stop = _number(entry_price, positive=True), _number(initial_stop, positive=True)
    entered = _clock(entry_at)
    if stop >= entry:
        raise ValueError("initial stop must be below entry")
    risk = entry - stop
    fee, initial = Decimal(".001"), Decimal(".1")
    if initial * (1 + fee) - initial / entry * stop * (1 - fee) > risk / entry:
        raise ValueError("initial position including costs exceeds risk cap")
    stages = [{"target_allocation": target, "min_price": str(entry + multiple * risk),
               "max_price": str(entry + (multiple + Decimal(".5")) * risk)}
              for target, multiple in (("0.3", Decimal(".5")), ("0.6", Decimal(1)), ("1.0", Decimal(2)))]
    return _seal({"policy_version": VERSION, "mode": "SHADOW", "market": market,
                  "revision": 0, "entry_price": str(entry), "initial_stop": str(stop),
                  "initial_r": str(risk), "entry_at": entered.isoformat(),
                  "expires_at": (entered + timedelta(days=5)).isoformat(),
                  "source_decision_hash": _hash(source_decision_ref),
                  "source_authority": "CALLER_ATTESTED_NOT_AUTHENTICATED",
                  "initial_allocation": "0.1", "max_allocation": "1.0", "cost_rate": "0.001",
                  "max_risk_allocation": str(risk / entry),
                  "stages": stages})


def create_state(plan):
    _validate_plan(plan)
    return {"plan_hash": plan["plan_hash"], "stop": plan["initial_stop"],
            "high_watermark": plan["entry_price"], "last_bar_end": None,
            "last_evaluated_at": plan["entry_at"], "add_cancelled": False,
            "filled_target": "0.1", "exited": False}


def evaluate(plan, state, evidence, *, now, cumulative_allocation,
             remaining_allocation, normalized_units):
    """Same evaluator for regular/mechanical sources. Returns intent, never I/O.

    Fresh quote protection precedes all add gates, expiry and cancellation.
    A bar is consumed only upon ADD, preventing duplicate stages on that bar.
    Costs bound loss at the stop, not gap risk. Cash reservation is ledger-owned.
    """
    _validate_plan(plan)
    if state.get("plan_hash") != plan["plan_hash"]:
        raise ValueError("state plan mismatch")
    current = _clock(now)
    if current < _clock(state["last_evaluated_at"]):
        raise ValueError("out-of-order evaluation")
    deployed, remaining, units = map(_number, (cumulative_allocation, remaining_allocation, normalized_units))
    if remaining > deployed or deployed > 1:
        raise ValueError("invalid ledger allocation")
    updated = deepcopy(state)
    updated["last_evaluated_at"] = current.isoformat()
    stop = _number(state["stop"], positive=True)
    entry, risk = _number(plan["entry_price"]), _number(plan["initial_r"])
    if stop < _number(plan["initial_stop"]) or _number(state["high_watermark"]) < entry:
        raise ValueError("invalid protection state")
    facts = evidence if isinstance(evidence, dict) else {}
    evidence_hash = _hash(facts)
    price = None

    def result(action, reason, delta=Decimal(0), target=None):
        return {"action": action, "reason": reason, "delta_allocation": str(delta),
                "target_allocation": str(deployed if target is None else target),
                "price": str(price) if price is not None else None, "state": updated,
                "plan_hash": plan["plan_hash"], "evidence_hash": evidence_hash}

    def fresh(value, limit):
        age = (current - _clock(value)).total_seconds()
        if not 0 <= age <= limit:
            raise ValueError("stale or future evidence")

    # A confirmed cancellation survives missing price/bar inputs and cannot be
    # silently forgotten on the next batch. Stale claims cannot cancel a plan.
    try:
        gates = facts["gates"]
        fresh(gates["observed_at"], 120)
        if (facts.get("source") in {"regular", "mechanical"}
                and isinstance(gates.get("source_ref"), str) and gates["source_ref"].strip()
                and (facts.get("thesis_negative") is True or facts.get("regime_negative") is True)):
            updated["add_cancelled"] = True
    except (KeyError, TypeError, ValueError):
        pass
    if not units or state.get("exited") is True:
        updated["exited"] = True
        return result("WAIT", "STRATEGY_EXITED")
    try:
        quote = facts["quote"]
        fresh(quote["observed_at"], 120)
        price = _number(quote["price"], positive=True)
    except (KeyError, TypeError, ValueError):
        return result("WAIT", "QUOTE_MISSING_STALE_OR_INVALID")
    if price <= stop:
        updated["exited"] = True
        return result("EXIT", "PROTECTIVE_STOP")
    if facts.get("source") not in {"regular", "mechanical"}:
        return result("WAIT", "INVALID_SOURCE")
    try:
        session, bar = facts["session"], facts["bar"]
        opened, closed = _clock(session["open_at"]), _clock(session["close_at"])
        if (session.get("verified") is not True or not isinstance(session.get("source_ref"), str)
                or not session["source_ref"].strip() or not opened <= current < closed):
            raise ValueError("verified regular session required")
        start, end, observed = map(_clock, (bar["open_at"], bar["close_at"], bar["observed_at"]))
        if (bar.get("completed") is not True or end - start != timedelta(minutes=5)
                or not opened <= start < end <= closed or not end <= observed <= current
                or _clock(quote["observed_at"]) < end
                or end <= _clock(plan["entry_at"])):
            raise ValueError("completed post-entry regular five minute bar required")
        fresh(bar["close_at"], 600)
        fresh(bar["observed_at"], 120)
        close = _number(bar["close"], positive=True)
        if state["last_bar_end"] and end <= _clock(state["last_bar_end"]):
            return result("WAIT", "BAR_ALREADY_CONSUMED")
    except (KeyError, TypeError, ValueError):
        return result("WAIT", "BAR_OR_SESSION_INVALID")
    high = max(_number(state["high_watermark"]), close)
    if high >= entry + risk:
        stop = max(stop, high - risk)
    updated.update(stop=str(stop), high_watermark=str(high))
    if price <= stop:
        updated["exited"] = True
        return result("EXIT", "TRAILING_STOP")
    if remaining < deployed:
        updated["add_cancelled"] = True
    try:
        gates = facts["gates"]
        fresh(gates["observed_at"], 120)
        if not isinstance(gates.get("source_ref"), str) or not gates["source_ref"].strip():
            raise ValueError("gate provenance required")
        if facts.get("thesis_negative") is True or facts.get("regime_negative") is True:
            updated["add_cancelled"] = True
        if updated["add_cancelled"]:
            return result("WAIT", "ADD_CANCELLED")
        if current >= _clock(plan["expires_at"]):
            return result("WAIT", "ADD_EXPIRED")
        if any(gates.get(gate) is not True for gate in GATES):
            return result("WAIT", "ADD_GATE_FAILED")
    except (KeyError, TypeError, ValueError):
        return result("WAIT", "GATES_MISSING_STALE_OR_INVALID")
    if deployed != _number(state["filled_target"]) or deployed not in map(Decimal, (".1", ".3", ".6", "1")):
        return result("WAIT", "STAGE_LEDGER_MISMATCH")
    stage = next((s for s in plan["stages"] if _number(s["target_allocation"]) > deployed), None)
    if stage is None:
        return result("WAIT", "FULL_ALLOCATION")
    if not (_number(stage["min_price"]) <= close and _number(stage["min_price"]) <= price <= _number(stage["max_price"])):
        return result("WAIT", "STAGE_PRICE_NOT_ELIGIBLE")
    if price <= entry or price * units <= remaining:
        return result("WAIT", "NO_AVERAGING_DOWN")
    target = _number(stage["target_allocation"])
    delta, fee = target - deployed, _number(plan["cost_rate"])
    proposed_units = units + delta / price
    loss_at_stop = max(Decimal(0), (remaining + delta) * (1 + fee) - proposed_units * stop * (1 - fee))
    if loss_at_stop > _number(plan["max_risk_allocation"]):
        return result("WAIT", "TOTAL_RISK_EXCEEDED")
    updated.update(last_bar_end=end.isoformat(), filled_target=str(target))
    return result("ADD", "STAGE_CONFIRMED", delta, target)


def revise_plan(plan, state, *, source, expected_version, now, cancel=False,
                stop=None, expires_at=None, stages=None):
    """Regular source may tighten future bounds only. No budget/initial-R edits."""
    _validate_plan(plan)
    if source != "regular" or expected_version != plan["revision"] or state.get("plan_hash") != plan["plan_hash"]:
        raise ValueError("regular source and current revision required")
    current = _clock(now)
    if current < _clock(state["last_evaluated_at"]) or not isinstance(cancel, bool):
        raise ValueError("invalid revision clock or cancellation")
    revised, updated = deepcopy(plan), deepcopy(state)
    if stop is not None:
        candidate = _number(stop, positive=True)
        if candidate < _number(state["stop"]):
            raise ValueError("stop cannot loosen")
        updated["stop"] = str(candidate)
    if expires_at is not None:
        expiration = _clock(expires_at)
        if expiration > _clock(plan["expires_at"]):
            raise ValueError("expiry cannot extend")
        revised["expires_at"] = expiration.isoformat()
    if stages is not None:
        if not isinstance(stages, list) or len(stages) != 3:
            raise ValueError("fixed three stages required")
        for old, new in zip(plan["stages"], stages):
            if not isinstance(new, dict) or set(new) != set(old) or new["target_allocation"] != old["target_allocation"]:
                raise ValueError("stage target and schema immutable")
            if _number(old["target_allocation"]) <= _number(state["filled_target"]) and new != old:
                raise ValueError("filled stage immutable")
            if not _number(old["min_price"]) <= _number(new["min_price"]) <= _number(new["max_price"]) <= _number(old["max_price"]):
                raise ValueError("stage may only tighten")
        revised["stages"] = deepcopy(stages)
    updated["add_cancelled"] = updated["add_cancelled"] or cancel
    updated["last_evaluated_at"] = current.isoformat()
    revised["revision"] += 1
    revised = _seal(revised)
    updated["plan_hash"] = revised["plan_hash"]
    return {"plan": revised, "state": updated}

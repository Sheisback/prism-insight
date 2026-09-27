"""Isolated caller-attested research policy. No orders, I/O or stop mutation."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import hashlib
import json
from zoneinfo import ZoneInfo

VERSION = "oneil-adaptive-v1"
EVIDENCE_VERSION = "oneil-adaptive-evidence-v1"


def _num(value, positive=False):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("finite number required") from None
    if isinstance(value, bool) or not number.is_finite() or number < 0 or (positive and not number):
        raise ValueError("finite nonnegative number required")
    return number


def _time(value):
    if not isinstance(value, str):
        raise ValueError("aware timestamp required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("aware timestamp required")
    return parsed.astimezone(timezone.utc)


def _ref(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("source reference required")
    return value


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def create_plan(*, symbol, entry_reference, initial_stop, source_decision_ref,
                created_at, setup, entry_eligible, fee_bps=10):
    """Freeze explicit proper-base and leader attestations, never infer them."""
    entry, stop = _num(entry_reference, True), _num(initial_stop, True)
    created, fee = _time(created_at), _num(fee_bps) / 10000
    if entry_eligible is not True or stop >= entry or fee_bps not in (10, 25):
        raise ValueError("eligible entry, valid stop and registered fee required")
    _ref(symbol)
    _ref(source_decision_ref)
    if setup["proper_base"] != "VERIFIED" or setup["fundamental_leader"] is not True:
        raise ValueError("explicit base and leader evidence required")
    if set(setup) != {"proper_base", "pivot", "as_of", "source_ref", "price_basis_ref",
                      "fundamental_leader", "fundamental_source_ref", "fundamental_as_of"}:
        raise ValueError("strict structured setup required")
    pivot = _num(setup["pivot"], True)
    for key in ("source_ref", "price_basis_ref", "fundamental_source_ref"):
        _ref(setup[key])
    if any(_time(setup[k]) > created for k in ("as_of", "fundamental_as_of")):
        raise ValueError("future setup")
    if not pivot <= entry <= pivot * Decimal("1.05"):
        raise ValueError("entry outside pivot band")
    plan = {"policy_version": VERSION, "mode": "RESEARCH_ONLY", "market": "US",
            "symbol": symbol, "entry_reference": str(entry), "initial_stop": str(stop),
            "source_decision_ref": source_decision_ref, "created_at": created.isoformat(),
            "expires_at": (created + timedelta(days=5)).isoformat(),
            "setup": deepcopy(setup), "fee_rate": str(fee),
            "risk_limit": str((entry - stop) / entry),
            "authority": "CALLER_ATTESTED_NOT_AUTHENTICATED"}
    plan["plan_hash"] = _hash(plan)
    return plan


def _validate(plan):
    content = deepcopy(plan)
    digest = content.pop("plan_hash", None)
    if content.get("policy_version") != VERSION or digest != _hash(content):
        raise ValueError("plan version or hash mismatch")
    rebuilt = create_plan(symbol=plan["symbol"], entry_reference=plan["entry_reference"],
                          initial_stop=plan["initial_stop"],
                          source_decision_ref=plan["source_decision_ref"],
                          created_at=plan["created_at"], setup=plan["setup"],
                          entry_eligible=True,
                          fee_bps=int(_num(plan["fee_rate"]) * 10000))
    if rebuilt != plan:
        raise ValueError("noncanonical plan")


def evaluate_target(plan, evidence, *, now, cumulative_allocation, remaining_allocation,
                    normalized_units, remaining_entry_cost, current_stop,
                    last_add_bar_end=None, add_permission="AVAILABLE"):
    """Return a sizing intent only; persisted last-add clock is caller-owned."""
    _validate(plan)
    current = _time(now)
    deployed, remaining, units, cost, stop = map(_num, (
        cumulative_allocation, remaining_allocation, normalized_units, remaining_entry_cost, current_stop))
    if remaining > deployed or deployed > 1 or stop < _num(plan["initial_stop"]):
        raise ValueError("invalid ledger allocation or lowered protection")
    if (not deployed and (remaining or units or cost)) or (not remaining and (units or cost)):
        raise ValueError("inconsistent ledger state")
    facts = evidence if isinstance(evidence, dict) else {}
    digest, price, bar_end, nominal = _hash(facts), None, None, deployed

    def result(reason, action="WAIT", target=None, evidence_status="NOT_ASSESSED"):
        target = deployed if target is None else target
        return {"action": action, "reason": reason, "nominal_target": str(nominal),
                "target_allocation": str(target), "delta_allocation": str(target - deployed),
                "bar_end": bar_end, "price": None if price is None else str(price),
                "plan_hash": plan["plan_hash"], "evidence_hash": digest,
                "risk_clipped": target < nominal, "evidence_status": evidence_status}

    def fresh(value, seconds):
        stamp = _time(value)
        if not 0 <= (current - stamp).total_seconds() <= seconds:
            raise ValueError("stale or future evidence")
        return stamp

    # Identity and fresh quote are required even for protection. Add-only gates,
    # expiry and pending intents cannot suppress a valid protective signal.
    try:
        if (facts["contract_version"] != EVIDENCE_VERSION or facts["symbol"] != plan["symbol"]
                or facts["price_basis_ref"] != plan["setup"]["price_basis_ref"]):
            raise ValueError("identity mismatch")
        _ref(facts["source_ref"])
        quote = facts["quote"]
        _ref(quote["source_ref"])
        quote_at = fresh(quote["observed_at"], 120)
        price = _num(quote["price"], True)
    except KeyError:
        return result("MISSING_QUOTE_OR_IDENTITY", evidence_status="MISSING")
    except (TypeError, ValueError, AttributeError):
        return result("INVALID_QUOTE_OR_IDENTITY")
    if units and price <= stop:
        return result("PROTECTIVE_STOP", "PROTECTIVE_EXIT_REQUIRED")
    if deployed and not units:
        return result("STRATEGY_CLOSED")
    if remaining != deployed:
        return result("REDUCED_POSITION")
    if add_permission != "AVAILABLE":
        return result("ADD_NOT_AVAILABLE")
    if not _time(plan["created_at"]) <= current < _time(plan["expires_at"]):
        return result("PLAN_NOT_ACTIVE")
    try:
        if facts["source"] not in ("regular", "mechanical"):
            raise ValueError("source")
        session = facts["market_window"]
        _ref(session["source_ref"])
        opened, closed = _time(session["open_at"]), _time(session["close_at"])
        today = date.fromisoformat(session["trade_date"])
        local_open, local_close = (stamp.astimezone(ZoneInfo("America/New_York"))
                                  for stamp in (opened, closed))
        if session["verified"] is not True or not opened <= current < closed:
            raise ValueError("regular session")
        if (local_open.date() != today or local_close.date() != today or today.weekday() >= 5
                or (local_open.hour, local_open.minute, local_open.second, local_open.microsecond)
                != (9, 30, 0, 0) or local_close.hour > 16
                or (local_close.hour == 16 and (local_close.minute or local_close.second
                                               or local_close.microsecond))
                or not timedelta(0) < closed - opened <= timedelta(hours=6, minutes=30)):
            raise ValueError("session period")
        bars = facts["bars"]
        if not 1 <= len(bars) <= 2:
            raise ValueError("one or two completed bars required")
        previous_end = None
        for bar in bars:
            _ref(bar["source_ref"])
            start, end = _time(bar["start_at"]), _time(bar["end_at"])
            if (bar["complete"] is not True or bar["regular"] is not True
                    or end - start != timedelta(minutes=5) or not opened <= start < end <= closed
                    or end > current or (previous_end is not None and start != previous_end)
                    or (start - opened).total_seconds() % 300):
                raise ValueError("invalid completed bars")
            _num(bar["close"], True)
            previous_end = end
        bar_end = bars[-1]["end_at"]
        ended = fresh(bar_end, 600)
        if ((deployed and ended <= _time(plan["created_at"])) or quote_at < ended
                or (last_add_bar_end is not None and ended <= _time(last_add_bar_end))):
            raise ValueError("repeated bar or older quote")
        gates = facts["gates"]
        _ref(gates["source_ref"])
        fresh(gates["observed_at"], 120)
        if (any(type(gates[k]) is not bool for k in ("admission", "risk", "RR", "sector", "slot"))
                or gates["market_pulse"] not in ("UPTREND", "UNDER_PRESSURE", "CORRECTION")
                or gates["regime"] not in ("moderate_bull", "strong_bull", "parabolic", "sideways",
                                           "moderate_bear", "strong_bear")):
            return result("MISSING_GATE_EVIDENCE", evidence_status="MISSING")
        if (any(gates[k] is not True for k in ("admission", "risk", "RR", "sector", "slot"))
                or gates["market_pulse"] != "UPTREND"
                or gates["regime"] not in ("moderate_bull", "strong_bull", "parabolic")):
            return result("ADD_GATE_NOT_MET", evidence_status="CONDITION_NOT_MET")
        volume = facts["volume"]
        _ref(volume["calendar_ref"])
        _ref(volume["source_ref"])
        elapsed = (ended - opened).total_seconds() / 60
        if (_time(volume["as_of"]) != ended or volume["elapsed_minutes"] != elapsed
                or volume["complete"] is not True or volume["regular"] is not True
                or volume["basis"] != "MATCHED_REGULAR_CUMULATIVE"):
            raise ValueError("volume period")
        expected = volume["expected_prior_trade_dates"]
        if len(expected) != 20 or len(set(expected)) != 20 or expected != sorted(expected):
            raise ValueError("comparison dates")
        if any(date.fromisoformat(day) >= today or date.fromisoformat(day).weekday() >= 5
               for day in expected):
            raise ValueError("future volume")
        samples = volume["samples"]
        if len(samples) != 20 or sorted(x["trade_date"] for x in samples) != expected:
            raise ValueError("comparison population")
        total = Decimal(0)
        for sample in samples:
            _ref(sample["source_ref"])
            if (sample["elapsed_minutes"] != elapsed or sample["complete"] is not True
                    or sample["regular"] is not True):
                raise ValueError("unmatched volume")
            total += _num(sample["cumulative_volume"])
        if not total:
            return result("MISSING_VOLUME_DENOMINATOR", evidence_status="MISSING")
        if _num(volume["cumulative_volume"]) / (total / 20) < Decimal("1.5"):
            return result("VOLUME_NOT_CONFIRMED", evidence_status="CONDITION_NOT_MET")
    except KeyError:
        return result("MISSING_ADD_EVIDENCE", evidence_status="MISSING")
    except (TypeError, ValueError, AttributeError):
        return result("ADD_EVIDENCE_REJECTED")
    pivot = _num(plan["setup"]["pivot"])
    closing = _num(bars[-1]["close"])
    if not all(pivot <= p <= pivot * Decimal("1.05") for p in (closing, price)):
        return result("OUTSIDE_BUY_BAND")
    nominal = Decimal(".5")
    persistent = len(bars) == 2 and all(_num(b["close"]) > pivot for b in bars)
    if deployed and persistent:
        if min(closing, price) >= pivot * Decimal("1.04"):
            nominal = Decimal(1)
        elif min(closing, price) >= pivot * Decimal("1.02"):
            nominal = Decimal(".8")
    if deployed and (price <= _num(plan["entry_reference"]) or price * units <= remaining + cost):
        return result("NOT_PROFITABLE")
    if nominal <= deployed:
        return result("TARGET_ALREADY_REACHED")
    fee, limit = _num(plan["fee_rate"]), _num(plan["risk_limit"])
    if price <= stop:
        return result("STOP_NOT_BELOW_PRICE")
    base_loss = remaining + cost - units * stop * (1 - fee)
    marginal = 1 + fee - stop / price * (1 - fee)
    if base_loss >= limit:
        return result("RISK_LIMIT")
    allowed = (limit - base_loss) / marginal
    target = min(nominal, deployed + allowed).quantize(Decimal(".0001"), rounding=ROUND_DOWN)
    if target <= deployed:
        return result("RISK_LIMIT")
    return result("QUALIFIED", "ADD", target, evidence_status="INPUT_CONTRACT_PASSED")

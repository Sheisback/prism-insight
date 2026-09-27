"""Read-only current-input boundary. Capture time is never source observation time.

No runtime installation, orders, network defaults, or protection scheduling lives
here. Suppliers must be read-only; callers run optional capture after protection.
"""

import asyncio
from copy import deepcopy
import inspect
import json

from prism_core.oneil_adaptive_policy import _hash, _num, _ref, _time, _validate
from prism_core.oneil_input_bridge import assemble_evidence

CONTRACT = "oneil-current-capture-v1"
MAX_BYTES = 65536


def capture_current_record(*, plan, position_id, setup_input, intraday_input,
                           quote, gates, stop, now, exit_event=None,
                           source="mechanical"):
    """Return replay-compatible ticks only when all source bindings are valid.

    Quote, gate, stop and exit sources must identify the original decision and
    position. Gate snapshots must name the exact quote they evaluated. Negative
    booleans are preserved, not confused with missing evidence. Independent
    protection/exit observations survive unavailable add inputs.
    """
    _validate(plan)
    _ref(position_id)
    current = _time(now)
    if current < _time(plan["created_at"]):
        raise ValueError("capture before initial plan")
    identity = dict(symbol=plan["symbol"], source_decision_ref=plan["source_decision_ref"],
                    position_id=position_id, price_basis_ref=plan["setup"]["price_basis_ref"])
    result = dict(contract_version=CONTRACT, **identity, occurred_at=now,
                  plan_hash=plan["plan_hash"], status="MISSING", reason_codes=[],
                  tick=None, protection=dict(quote=None, stop=None), exit_event=None,
                  execution_authorized=False, authority="CALLER_ATTESTED_NOT_AUTHENTICATED")

    def validated(value, fields, *, fresh=False, clock="observed_at"):
        if not isinstance(value, dict) or any(value.get(k) != v for k, v in identity.items()):
            raise ValueError("identity")
        _ref(value["source_ref"])
        age = (current - _time(value[clock])).total_seconds()
        if age < 0 or (fresh and age > 120):
            raise ValueError("clock")
        cleaned = {k: deepcopy(value[k]) for k in (*identity, "source_ref", clock, *fields)}
        if len(json.dumps(cleaned, allow_nan=False).encode()) > MAX_BYTES:
            raise ValueError("oversized")
        return cleaned

    def read(kind, value, fields, **kwargs):
        try:
            return validated(value, fields, **kwargs)
        except (KeyError, TypeError, ValueError, AttributeError):
            result["reason_codes"].append(kind + "_UNAVAILABLE_OR_INVALID")
            return None

    q = read("QUOTE", quote, ("price",), fresh=True)
    if q is not None:
        try:
            _num(q["price"], True)
        except (ValueError, TypeError):
            q = None
            result["reason_codes"].append("QUOTE_PRICE_INVALID")
    s = read("STOP", stop, ("current_stop",), clock="available_at")
    if s is not None:
        try:
            if _num(s["current_stop"], True) < _num(plan["initial_stop"], True):
                raise ValueError("lowered stop")
        except (ValueError, TypeError):
            s = None
            result["reason_codes"].append("STOP_PRICE_INVALID")
    result["protection"] = dict(quote=q, stop=s)
    if exit_event is not None:
        terminal = read("EXIT", exit_event, ("price", "occurred_at"), clock="available_at")
        if terminal is not None:
            try:
                _num(terminal["price"], True)
                if not _time(plan["created_at"]) < _time(terminal["occurred_at"]) <= _time(terminal["available_at"]) <= current:
                    raise ValueError("exit clock")
                result["exit_event"] = terminal
            except (ValueError, TypeError):
                result["reason_codes"].append("EXIT_PRICE_OR_CLOCK_INVALID")
    g = read("GATES", gates, ("admission", "risk", "RR", "sector", "slot",
                              "market_pulse", "regime", "quote_source_ref", "price"), fresh=True)
    if g is not None:
        try:
            if (q is None or g["quote_source_ref"] != q["source_ref"]
                    or _num(g["price"], True) != _num(q["price"], True)
                    or _time(g["observed_at"]) < _time(q["observed_at"])
                    or any(type(g[k]) is not bool for k in ("admission", "risk", "RR", "sector", "slot"))
                    or g["market_pulse"] not in ("UPTREND", "UNDER_PRESSURE", "CORRECTION")
                    or g["regime"] not in ("moderate_bull", "strong_bull", "parabolic", "sideways", "moderate_bear", "strong_bear")):
                raise ValueError("gate binding")
        except (ValueError, TypeError):
            g = None
            result["reason_codes"].append("GATE_QUOTE_BINDING_OR_VALUES_INVALID")
    assembled = assemble_evidence(plan=plan, setup_input=setup_input,
                                  intraday_input=intraday_input, quote=q, gates=g,
                                  now=now, source=source)
    if assembled["status"] == "OK" and s is not None:
        tick = dict(**identity, occurred_at=now, available_at=now,
                    source_ref=_hash([q, g, s, assembled["evidence"]]),
                    current_stop=s["current_stop"], stop_source_ref=s["source_ref"],
                    stop_available_at=s["available_at"], evidence=assembled["evidence"])
        if len(json.dumps(tick, allow_nan=False).encode()) <= MAX_BYTES:
            result.update(status="OK", tick=tick)
        else:
            result["reason_codes"].append("TICK_TOO_LARGE")
    else:
        result["reason_codes"].extend(assembled["reason_codes"])
    result["record_hash"] = _hash(result)
    return result


def existing_us_snapshot(*, current_price, current_stop, source_ref,
                         symbol, source_decision_ref, position_id, price_basis_ref,
                         stop_available_at=None, quote_observed_at=None):
    """Adapt existing US numeric values without inventing missing source clocks.

    _refresh_buy_quote currently discards regularMarketTime; ordinary current_price
    values therefore cannot qualify as fresh evidence. A caller may only provide
    quote_observed_at when the actual provider observation timestamp is retained.
    """
    identity = dict(symbol=symbol, source_decision_ref=source_decision_ref,
                    position_id=position_id, price_basis_ref=price_basis_ref,
                    source_ref=source_ref)
    return dict(quote=dict(**identity, price=current_price, observed_at=quote_observed_at),
                stop=dict(**identity, current_stop=current_stop, available_at=stop_available_at),
                gates=None)


async def collect_current_record(*, suppliers, timeout_seconds=1, **kwargs):
    """Bound optional read-only supplier waits, never call from protection path.

    Sync suppliers run in threads; timeout does not kill an already running
    supplier. They must have their own bounded I/O and no mutation side effects.
    """
    if not 0 < timeout_seconds <= 10 or set(suppliers) - {"quote", "gates", "stop", "exit_event", "intraday_input"}:
        raise ValueError("bounded supported suppliers required")

    async def get(name, supplier):
        try:
            pending = supplier() if inspect.iscoroutinefunction(supplier) else asyncio.to_thread(supplier)
            return name, await asyncio.wait_for(pending, timeout_seconds)
        except Exception:
            return name, None

    supplied = dict(await asyncio.gather(*(get(k, v) for k, v in suppliers.items())))
    return capture_current_record(**{**kwargs, **supplied})

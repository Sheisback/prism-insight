"""Synthetic-path research only. No production orders or historical adapter.

Ticks must already be calendar/continuity/provenance checked by a future adapter.
Instant simulated fills do NOT validate concurrent workers or broker recovery.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite


@dataclass(frozen=True)
class Tick:
    at: datetime
    close: float
    next_open: float


def simulate(*, entry, stop, entry_at, exit_at, exit_price, ticks,
             trailing=False, cost_bps=10):
    def valid_price(p):
        return isinstance(p, (int, float)) and not isinstance(p, bool) and isfinite(p) and p > 0

    def aware(t):
        return isinstance(t, datetime) and t.utcoffset() is not None

    if not all(valid_price(p) for p in (entry, stop, exit_price)) or stop >= entry:
        raise ValueError("INVALID_PRICE_OR_R")
    if not aware(entry_at) or not aware(exit_at) or exit_at <= entry_at:
        raise ValueError("INVALID_CLOCK")
    if cost_bps not in (10, 25) or not isinstance(trailing, bool):
        raise ValueError("UNREGISTERED_PROFILE")
    ordered = sorted(ticks, key=lambda t: t.at)
    seen = {}
    for t in ordered:
        if not aware(t.at) or not valid_price(t.close) or not valid_price(t.next_open):
            raise ValueError("INVALID_TICK")
        if t.at in seen and t != seen[t.at]:
            raise ValueError("CONFLICTING_TICK")
        seen[t.at] = t
    r = entry - stop
    c = cost_bps / 10000
    qty = .1 / (entry * (1 + c))
    principal = qty * entry
    spent = .1
    stage = 10
    high = entry
    active_stop = stop
    legs, decisions, stops = [], [], [stop]
    last_price, closed_at, exit_reason = exit_price, exit_at, "ORIGINAL_EXIT"
    worst = 0.0
    for t in seen.values():
        if not entry_at < t.at < exit_at:
            continue
        worst = min(worst, qty * t.close * (1-c) - spent)
        high = max(high, t.close)
        if trailing and high >= entry + r:
            active_stop = max(active_stop, high - r)
        stops.append(active_stop)
        # At a scheduled tick the known next-open gap also invalidates any add.
        if min(t.close, t.next_open) <= active_stop:
            last_price, closed_at, exit_reason = t.next_open, t.at, "MECHANICAL_STOP"
            break
        if t.at >= entry_at + timedelta(days=5) or stage == 100:
            continue
        target, multiple = {10: (30, .5), 30: (60, 1), 60: (100, 2)}[stage]
        threshold = entry + multiple * r
        cap = threshold + .5 * r
        if not threshold <= t.close <= cap:
            continue
        price = t.next_open
        if not threshold <= price <= cap or price <= max(entry, principal / qty):
            decisions.append("EXECUTION_PRICE_REJECTED")
            continue
        delta = max(0, target / 100 / price - qty)
        payment = delta * price * (1+c)
        proposed_spent = spent + payment
        proposed_qty = qty + delta
        # Includes entry costs already spent and estimated stop liquidation cost.
        risk = max(0, proposed_spent - proposed_qty * active_stop * (1-c))
        if proposed_spent > 1 + 1e-12 or risk > r / entry + 1e-12:
            decisions.append("CASH_OR_RISK_LIMIT")
            continue
        qty, spent = proposed_qty, proposed_spent
        principal += delta * price
        stage = target
        legs.append(target)
    pnl = qty * last_price * (1-c) - spent
    worst = min(worst, pnl)
    baseline = exit_price * (1-c) / (entry * (1+c)) - 1
    return dict(pnl=pnl, baseline_pnl=baseline, legs=legs, target=stage,
                stop_path=stops, spent=spent, decisions=decisions,
                exit_at=closed_at.isoformat(), exit_reason=exit_reason,
                observed_tick_mae_r=worst/(r/entry), historical_validation=False)

"""Offline arithmetic checks for batch-r-v1; NOT a historical-data adapter.

Caller must establish exact linkage, complete batch paths and price provenance.
No production caller, broker, filesystem or network dependencies.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite


@dataclass(frozen=True)
class Batch:
    ref: str
    decision: datetime
    signal_end: datetime
    signal_price: float
    execution_start: datetime
    execution_price: float


def replay(*, entry, stop, exit_price, entry_at, exit_at, entry_batch,
           batches, cost_bps=10):
    """Return normalized slot PnL for a validated synthetic/research path.

    An empty path is allowed for synthetic tests, never proof of missing adds.
    Times must be timezone-aware. Missing observations raise instead of PASS.
    """
    def positive(x):
        return isinstance(x, (int, float)) and not isinstance(x, bool) and isfinite(x) and x > 0

    def aware(t):
        return isinstance(t, datetime) and t.utcoffset() is not None

    if not all(positive(x) for x in (entry, stop, exit_price)) or stop >= entry:
        raise ValueError("MISSING_INITIAL_R_OR_PRICE")
    if not aware(entry_at) or not aware(exit_at) or exit_at <= entry_at:
        raise ValueError("INVALID_TRADE_CLOCK")
    if not entry_batch or cost_bps not in (10, 25):
        raise ValueError("UNREGISTERED_INPUT")
    cost = cost_bps / 10000
    qty = .1 / (entry * (1 + cost))
    spent = .1
    principal = qty * entry
    target = 10
    transitions = []
    reasons = []
    seen = {}
    previous_execution = entry_at
    for b in sorted(batches, key=lambda b: b.decision):
        if not b.ref or not all(aware(t) for t in (b.decision, b.signal_end, b.execution_start)):
            raise ValueError("INVALID_BATCH_CLOCK")
        if b.ref in seen:
            if seen[b.ref] != b:
                raise ValueError("CONFLICTING_BATCH")
            continue
        seen[b.ref] = b
        if b.ref == entry_batch or b.decision <= entry_at or b.decision >= exit_at:
            continue
        if not all(positive(p) for p in (b.signal_price, b.execution_price)):
            raise ValueError("MISSING_BATCH_PRICE")
        if b.signal_end > b.decision or b.execution_start <= b.decision:
            raise ValueError("LOOKAHEAD")
        if b.decision - b.signal_end > timedelta(minutes=10):
            raise ValueError("STALE_BATCH_PRICE")
        if b.decision <= previous_execution:
            raise ValueError("OVERLAPPING_BATCH")
        if b.execution_start >= exit_at:
            reasons.append("EXIT_FIRST")
            continue
        if target == 100:
            continue
        next_target, multiple = {10: (30, .5), 30: (60, 1), 60: (100, 2)}[target]
        threshold = entry + multiple * (entry - stop)
        if b.signal_price < threshold:
            continue
        price = b.execution_price
        if price < threshold or price <= max(entry, principal / qty):
            reasons.append("GAP_OR_NOT_PROFITABLE")
            continue
        delta = max(0, next_target / 100 / price - qty)
        payment = delta * price * (1 + cost)
        if spent + payment > 1 + 1e-12:
            reasons.append("CASH_LIMIT")
            continue
        qty += delta
        principal += delta * price
        spent += payment
        target = next_target
        previous_execution = b.execution_start
        transitions.append(target)
    baseline_qty = 1 / (entry * (1 + cost))
    baseline = baseline_qty * exit_price * (1 - cost) - 1
    experiment = qty * exit_price * (1 - cost) - spent
    return dict(baseline_pnl=baseline, experiment_pnl=experiment,
                difference=experiment - baseline, target=target,
                transitions=transitions, spent=spent, reasons=reasons,
                historical_validation=False)

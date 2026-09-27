"""Pure pivot/base detection and breakout triggers for re-entry v2 (design:
docs/REENTRY_V2_DESIGN_20260927_ko.md). No I/O; shared by replay and the live loop.

Simplified O'Neil pivot: the highest high of the last 7-65 completed sessions that
is at least 5 sessions old, with no higher high since, and a base no deeper than
35% (US) / 40% (KR). Buy zone: pivot to pivot +5%.

Bars are dicts {date, open, high, low, close, volume} sorted by date. Functions
that look at "day i" only use bars[:i] as completed history plus day i's own
prices where a trigger explicitly needs them, so replay has no look-ahead beyond
the simulated session.
"""
from __future__ import annotations

POLICY_VERSION = "pivot_reentry_v2"
BASE_MIN_BARS = 7
BASE_MAX_BARS = 65
PIVOT_MIN_AGE = 5
MAX_DEPTH = {"KR": 0.40, "US": 0.35}
READY_BAND = 0.05          # previous close within 5% below the pivot
BUY_ZONE = 0.05            # never pay more than pivot +5%
FOLLOW_THROUGH_VOLUME = 1.4
INTRADAY_VOLUME = 1.0      # cumulative session volume >= one full-day 20-day average (lower bound)
STOP_CAP = 0.07
BREAKEVEN_AFTER = 0.10
HOLD_BARS = 40
WATCH_BARS = 30


def _avg(values):
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def find_pivot(bars, i, market):
    """Pivot as of the start of day i (uses bars[:i] only). None when there is no base."""
    window = bars[max(0, i - BASE_MAX_BARS):i]
    if len(window) < BASE_MIN_BARS + PIVOT_MIN_AGE:
        return None
    core = window[:-PIVOT_MIN_AGE]
    k = max(range(len(core)), key=lambda j: (core[j]["high"], -j))
    pivot = core[k]["high"]
    after = window[k + 1:]
    if any(b["high"] > pivot for b in after):
        return None                      # a newer high means no base has formed yet
    low = min(b["low"] for b in after)
    depth = 1 - low / pivot
    if depth > MAX_DEPTH[market]:
        return None
    return {"pivot": pivot, "pivot_date": core[k]["date"], "base_low": low, "depth": round(depth, 4),
            "base_bars": len(after)}


def trend_ok(bars, i):
    """Existing trend-gate semantics on completed bars: not T1 (close<MA50), not T2."""
    closes = [b["close"] for b in bars[:i]]
    if len(closes) < 55:
        return None
    close, ma50 = closes[-1], _avg(closes[-50:])
    ma20, ma20_prev = _avg(closes[-20:]), _avg(closes[-25:-5])
    t1 = close < ma50
    t2 = ma20 < ma20_prev and close <= ma20 * 0.95
    return not (t1 or t2)


def avg_volume(bars, i, n=20):
    prior = bars[max(0, i - n):i]
    return _avg(b["volume"] for b in prior) if len(prior) == n else None


def evaluate_day(bars, i, market):
    """State and trigger for day i. Returns dict with status and optional entry."""
    base = find_pivot(bars, i, market)
    trend = trend_ok(bars, i)
    out = {"date": bars[i]["date"], "base": base, "trend_ok": trend}
    if base is None or trend is not True:
        out["status"] = "NO_BASE" if base is None else "TREND_BLOCKED"
        return out
    pivot, prev, today = base["pivot"], bars[i - 1], bars[i]
    vol_avg = avg_volume(bars, i)
    ceiling = pivot * (1 + BUY_ZONE)
    # Follow-through: yesterday closed above the pivot (as of yesterday) on >=1.4x volume.
    prev_base = find_pivot(bars, i - 1, market)
    prev_avg = avg_volume(bars, i - 1)
    if prev_base and prev_avg and prev["close"] > prev_base["pivot"] and \
            prev["close"] <= prev_base["pivot"] * (1 + BUY_ZONE) and prev["volume"] >= FOLLOW_THROUGH_VOLUME * prev_avg:
        if today["open"] <= prev_base["pivot"] * (1 + BUY_ZONE):
            out.update(status="TRIGGERED", trigger="FOLLOW_THROUGH", entry=today["open"], pivot=prev_base["pivot"])
            return out
    ready = pivot * (1 - READY_BAND) <= prev["close"] <= pivot
    out["status"] = "READY" if ready else "WATCHING"
    if not ready:
        return out
    if today["open"] > ceiling:
        out["status"] = "CHASE_SKIPPED"
        return out
    # Intraday breakout proxy: the session crossed the pivot and its volume reached one
    # full-day average (the live loop requires cumulative volume >= that average).
    if today["high"] > pivot and vol_avg and today["volume"] >= INTRADAY_VOLUME * vol_avg:
        out.update(status="TRIGGERED", trigger="INTRADAY_BREAKOUT", entry=max(today["open"], pivot), pivot=pivot)
    return out


def simulate(bars, i, entry, *, intraday=True):
    """Managed exit from day i: 7% stop, breakeven after +10% close, MA20 trend exit, 40-bar horizon."""
    if i < 20:
        return {"status": "MISSING", "reason": "insufficient_history"}
    stop, mfe, mae = entry * (1 - STOP_CAP), 0.0, 0.0
    for k, bar in enumerate(bars[i:i + HOLD_BARS], start=1):
        mfe, mae = max(mfe, bar["high"] / entry - 1), min(mae, bar["low"] / entry - 1)
        if bar["low"] <= stop:
            price = stop if (k == 1 and intraday) or bar["open"] > stop else bar["open"]
            return {"status": "CLOSED", "exit_reason": "stop" if stop < entry else "breakeven_stop",
                    "exit_index": i + k - 1, "ret": round(price / entry - 1, 6), "bars": k,
                    "mfe": round(mfe, 6), "mae": round(mae, 6)}
        ma20 = _avg(b["close"] for b in bars[i + k - 20:i + k])
        if k >= 3 and bar["close"] < ma20:
            return {"status": "CLOSED", "exit_reason": "trend_exit_ma20", "exit_index": i + k - 1,
                    "ret": round(bar["close"] / entry - 1, 6), "bars": k, "mfe": round(mfe, 6), "mae": round(mae, 6)}
        if bar["close"] >= entry * (1 + BREAKEVEN_AFTER):
            stop = max(stop, entry)
    held = bars[i:i + HOLD_BARS]
    if len(held) < HOLD_BARS:
        return {"status": "PENDING", "bars": len(held)}
    return {"status": "CLOSED", "exit_reason": "horizon", "exit_index": i + HOLD_BARS - 1,
            "ret": round(held[-1]["close"] / entry - 1, 6), "bars": HOLD_BARS, "mfe": round(mfe, 6), "mae": round(mae, 6)}


def run_watch(bars, start_index, market, *, market_ok=None, watch_bars=WATCH_BARS):
    """Scan a watch from start_index for up to watch_bars sessions; first trigger wins.

    market_ok(date) -> bool|None gates triggers on the previous session's market state.
    Also returns the READY_OPEN timing control (open of the first READY day).
    """
    ready_control = None
    events = {"chase_skipped": 0, "market_blocked": 0}
    for i in range(start_index, min(len(bars), start_index + watch_bars)):
        if i < 56:
            continue
        day = evaluate_day(bars, i, market)
        if day["status"] == "CHASE_SKIPPED":
            events["chase_skipped"] += 1
        if day["status"] in {"READY", "TRIGGERED"} and ready_control is None:
            ready_control = {"index": i, "entry": bars[i]["open"]}
        if day["status"] != "TRIGGERED":
            continue
        gate = market_ok(bars[i - 1]["date"]) if market_ok else True
        if gate is False:
            events["market_blocked"] += 1
            continue
        trade = simulate(bars, i, day["entry"], intraday=day["trigger"] == "INTRADAY_BREAKOUT")
        return {"status": "TRIGGERED", "day": day, "index": i, "trade": trade, "ready_control": ready_control,
                "events": events, "market_ok": gate}
    ended = min(len(bars), start_index + watch_bars) - 1
    complete = start_index + watch_bars <= len(bars)
    return {"status": "EXPIRED" if complete else "PENDING", "index": ended, "ready_control": ready_control,
            "events": events}

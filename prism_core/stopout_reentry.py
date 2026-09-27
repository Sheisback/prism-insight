"""Pure research-only re-entry watch after a stop-out. Never a BUY or order signal.

A position that was stopped out is watched on completed daily bars for a
pre-registered window. Two technical events are recognised:

* RECLAIM  - close back above the original entry/pivot and MA50 on rising volume.
* PULLBACK - after a reclaim, a low-volume pullback that holds the breakout
  zone and then turns up above the previous bar high.

A hypothetical trade is replayed from the NEXT bar open after an event with a
capped stop, so the same code serves historical replay and forward SHADOW.
Missing or ambiguous input is reported, never repaired into a signal.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date

POLICY_VERSION = "stopout_reentry_v1"
WINDOW_BARS = 20            # completed bars after the stop-out date to reclaim
PULLBACK_WINDOW = 15        # completed bars after the reclaim to complete a pullback
RECLAIM_VOLUME_MULT = 1.5   # event bar volume vs prior 20-bar average
PULLBACK_MIN = 0.03         # low must retrace >=3% from the post-reclaim high
PULLBACK_MAX = 0.12         # a deeper retrace is a failed move, not a pullback
RECLAIM_FAIL = 0.05         # close >5% under the reclaim level invalidates
DEEP_FAILURE = 0.15         # close >15% under the original entry invalidates
STOP_CAP = 0.07             # hypothetical stop never wider than 7%
BUY_ZONE = 0.05             # O'Neil buy zone: no entry >5% above the reclaim level
BREAKEVEN_AFTER = 0.10      # close >=10% above entry lifts the stop to breakeven
HOLD_BARS = 20              # hypothetical trade horizon
MIN_PRE_ENTRY_BARS = 55     # MA50 slope + 20-bar pivot need history
ACTIVE = {"WATCHING", "RECLAIMED", "MISSING"}
TERMINAL = {"INVALIDATED", "EXPIRED", "SIGNALLED"}


def ref(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]


def watch_id(market, account_key, ticker, entry_date, exit_date):
    return ref(POLICY_VERSION, market, account_key, ticker, str(entry_date)[:10], str(exit_date)[:10])


def normalize(rows):
    """Validate OHLCV rows ({date, open, high, low, close, volume}) and sort by date."""
    out, seen = [], set()
    for row in rows:
        day = date.fromisoformat(str(row["date"])[:10]).isoformat()
        if day in seen:
            raise ValueError("duplicate_bar")
        seen.add(day)
        o, h, low, c, v = (float(row[k]) for k in ("open", "high", "low", "close", "volume"))
        if not all(math.isfinite(x) for x in (o, h, low, c, v)) or min(o, h, low, c) <= 0 or v < 0:
            raise ValueError("invalid_price")
        if h < max(o, c) or low > min(o, c):
            raise ValueError("inconsistent_bar")
        out.append({"date": day, "open": o, "high": h, "low": low, "close": c, "volume": v})
    out.sort(key=lambda r: r["date"])
    return out


def enroll(market, account_key, ticker, entry_date, entry_price, exit_date, exit_price, bars,
           trigger_type=None, exit_kind=None, source="STOP_EXIT"):
    """Freeze reference levels from bars strictly before the entry date."""
    entry_day, exit_day = str(entry_date)[:10], str(exit_date)[:10]
    entry_price, exit_price = float(entry_price), float(exit_price)
    if not (entry_price > 0 and exit_price > 0) or exit_day < entry_day:
        raise ValueError("invalid_trade")
    pre = [b for b in normalize(bars) if b["date"] < entry_day]
    watch = {
        "watch_id": watch_id(market, account_key, ticker, entry_day, exit_day),
        "policy_version": POLICY_VERSION, "market": market, "account_key": account_key,
        "ticker": ticker, "entry_date": entry_day, "entry_price": entry_price,
        "exit_date": exit_day, "exit_price": exit_price, "trigger_type": trigger_type,
        "exit_kind": exit_kind, "source": source, "status": "WATCHING", "events": [],
    }
    if len(pre) < 20:
        watch.update(status="MISSING", reason="insufficient_pre_entry_bars")
        return watch
    watch["pivot"] = max(b["high"] for b in pre[-20:])
    watch["reclaim_level"] = max(entry_price, watch["pivot"])
    watch["pivot_asof"] = pre[-1]["date"]
    return watch


def _avg(values):
    return sum(values) / len(values)


def evaluate(watch, bars, asof=None):
    """Replay every completed bar after the stop-out and return the new watch state.

    ``asof`` excludes that date and later (the forming session). The result is a
    pure function of the frozen watch and bars, so reruns on the same bars are
    idempotent and emitted events keep stable ids.
    """
    out = {k: v for k, v in watch.items() if k not in {"reason", "trade"}}
    out["events"] = []
    if watch.get("status") == "MISSING" and "reclaim_level" not in watch:
        out.update(status="MISSING", reason=watch.get("reason", "missing_reference"))
        return out
    try:
        rows = normalize(bars)
    except ValueError as exc:
        out.update(status="MISSING", reason=str(exc))
        return out
    if asof:
        rows = [b for b in rows if b["date"] < str(asof)[:10]]
    if sum(b["date"] < watch["entry_date"] for b in rows) < MIN_PRE_ENTRY_BARS:
        out.update(status="MISSING", reason="insufficient_history")
        return out
    level, entry = watch["reclaim_level"], watch["entry_price"]
    after = [i for i, b in enumerate(rows) if b["date"] > watch["exit_date"]]
    if not after:
        out.update(status="WATCHING", elapsed_bars=0, asof=rows[-1]["date"])
        return out
    status, reclaim_i, reclaim_n, high_i, pb = "WATCHING", None, 0, None, None
    elapsed = 0
    for n, i in enumerate(after, start=1):
        bar, prev = rows[i], rows[i - 1]
        closes = [b["close"] for b in rows[: i + 1]]
        ma50, ma50_prev = _avg(closes[-50:]), _avg(closes[-55:-5])
        avg_vol = _avg([b["volume"] for b in rows[i - 20:i]])
        elapsed = n
        if bar["close"] < entry * (1 - DEEP_FAILURE):
            status = "INVALIDATED"
            out["reason"] = "deep_failure"
            break
        if reclaim_i is None:
            # Before a reclaim the stock may still sit in the downtrend it was skipped or
            # stopped for; recovery above MA50 is part of the reclaim, not a precondition.
            if n > WINDOW_BARS:
                status = "EXPIRED"
                out["reason"] = "window_elapsed_without_reclaim"
                break
            if bar["close"] > level and bar["close"] > ma50 and bar["close"] > prev["close"] and avg_vol > 0 \
                    and bar["volume"] >= RECLAIM_VOLUME_MULT * avg_vol:
                reclaim_i, reclaim_n, high_i, status = i, n, i, "RECLAIMED"
                out["events"].append(_event(out, "RECLAIM", bar, rows, i, avg_vol))
            continue
        if bar["close"] < ma50 and ma50 < ma50_prev:
            status = "INVALIDATED"
            out["reason"] = "trend_broken_ma50"
            break
        if bar["close"] < level * (1 - RECLAIM_FAIL):
            status = "INVALIDATED"
            out["reason"] = "reclaim_failed"
            break
        if pb is None:
            if bar["high"] > rows[high_i]["high"]:
                high_i = i
            elif bar["low"] <= rows[high_i]["high"] * (1 - PULLBACK_MIN):
                pb = {"start": i, "low": bar["low"]}
                if pb["low"] < rows[high_i]["high"] * (1 - PULLBACK_MAX):
                    status = "INVALIDATED"
                    out["reason"] = "pullback_too_deep"
                    break
            if n - reclaim_n > PULLBACK_WINDOW:
                status = "EXPIRED"
                out["reason"] = "window_elapsed_without_pullback"
                break
            continue
        pb["low"] = min(pb["low"], bar["low"])
        if pb["low"] < rows[high_i]["high"] * (1 - PULLBACK_MAX):
            status = "INVALIDATED"
            out["reason"] = "pullback_too_deep"
            break
        pull_vol = _avg([b["volume"] for b in rows[high_i + 1:i]])
        if bar["close"] > prev["high"] and pull_vol < avg_vol:
            status = "SIGNALLED"
            out["events"].append(_event(out, "PULLBACK", bar, rows, i, avg_vol,
                                        pullback_low=pb["low"], pullback_high=rows[high_i]["high"],
                                        pullback_depth=round(pb["low"] / rows[high_i]["high"] - 1, 6)))
            break
        if n - reclaim_n > PULLBACK_WINDOW:
            status = "EXPIRED"
            out["reason"] = "window_elapsed_in_pullback"
            break
    out.update(status=status, elapsed_bars=elapsed, asof=rows[after[min(elapsed, len(after)) - 1]]["date"])
    for event in out["events"]:
        event["trade"] = simulate(rows, event)
    return out


def _event(watch, kind, bar, rows, i, avg_vol, **extra):
    swing = min(b["low"] for b in rows[max(0, i - 4): i + 1])
    event = {
        "event_id": ref(watch["watch_id"], kind, bar["date"]), "kind": kind, "date": bar["date"],
        "close": bar["close"], "volume_ratio": round(bar["volume"] / avg_vol, 4) if avg_vol else None,
        "reclaim_level": watch["reclaim_level"], "swing_low": extra.pop("pullback_low", swing),
        "bars_after_exit": sum(b["date"] > watch["exit_date"] for b in rows[: i + 1]),
    }
    event.update(extra)
    return event


def simulate(rows, event):
    """Hypothetical trade: next-bar open entry, capped stop, managed exit, fixed horizon.

    * No chase: RECLAIM entries must open within the 5% buy zone above the
      reclaim level, PULLBACK entries at or below the pullback high.
    * Stop is the tighter of the 7% cap and the swing low; a gap through the
      stop exits at the open.
    * After a close >=10% above entry the stop moves to breakeven; from the
      third bar a close below MA20 exits at that close (trend exit).
    PENDING until the horizon or an exit is observed.
    """
    idx = next((k for k, b in enumerate(rows) if b["date"] > event["date"]), None)
    if idx is None:
        return {"status": "PENDING", "reason": "entry_bar_not_completed"}
    if idx < 19:
        return {"status": "MISSING", "reason": "insufficient_history_for_ma20"}
    entry = rows[idx]["open"]
    ceiling = event["reclaim_level"] * (1 + BUY_ZONE) if event["kind"] == "RECLAIM" else event.get("pullback_high")
    trade = {"entry_date": rows[idx]["date"], "entry": entry}
    if ceiling is not None and entry > ceiling:
        trade.update(status="SKIPPED", reason="entry_above_buy_zone", ceiling=round(ceiling, 6),
                     chase_pct=round(entry / ceiling - 1, 6))
        return trade
    stop = max(entry * (1 - STOP_CAP), event["swing_low"])
    if stop >= entry:
        stop = entry * (1 - STOP_CAP)
    trade.update(stop=round(stop, 6), stop_pct=round(stop / entry - 1, 6))
    path, mfe, mae = {}, 0.0, 0.0
    for k, bar in enumerate(rows[idx: idx + HOLD_BARS], start=1):
        mfe = max(mfe, bar["high"] / entry - 1)
        mae = min(mae, bar["low"] / entry - 1)
        if bar["low"] <= stop:
            exit_price = min(bar["open"], stop) if k > 1 else stop
            reason = "stop" if stop < entry else "breakeven_stop"
            trade.update(status="CLOSED", exit_reason=reason, exit_date=bar["date"],
                         ret=round(exit_price / entry - 1, 6), bars=k)
            break
        ma20 = _avg([b["close"] for b in rows[idx + k - 20: idx + k]])
        if k >= 3 and bar["close"] < ma20:
            trade.update(status="CLOSED", exit_reason="trend_exit_ma20", exit_date=bar["date"],
                         ret=round(bar["close"] / entry - 1, 6), bars=k)
            break
        if bar["close"] >= entry * (1 + BREAKEVEN_AFTER):
            stop = max(stop, entry)
        if k in (1, 5, 10, 20):
            path[str(k)] = round(bar["close"] / entry - 1, 6)
    else:
        held = rows[idx: idx + HOLD_BARS]
        if len(held) == HOLD_BARS:
            trade.update(status="CLOSED", exit_reason="horizon", exit_date=held[-1]["date"],
                         ret=round(held[-1]["close"] / entry - 1, 6), bars=HOLD_BARS)
        else:
            trade.update(status="PENDING", bars=len(held), mark=round(held[-1]["close"] / entry - 1, 6))
    trade.update(path=path, mfe=round(mfe, 6), mae=round(mae, 6))
    return trade


def hold_counterfactual(watch, bars, horizon=WINDOW_BARS):
    """What holding the original position without the stop would have returned."""
    rows = [b for b in normalize(bars) if b["date"] > watch["exit_date"]][:horizon]
    if len(rows) < horizon:
        return {"status": "PENDING", "bars": len(rows)}
    entry = watch["entry_price"]
    return {"status": "CLOSED", "ret": round(rows[-1]["close"] / entry - 1, 6),
            "max_close_ret": round(max(b["close"] for b in rows) / entry - 1, 6),
            "recovered_entry": any(b["close"] > entry for b in rows)}

"""Pure decision-time input features for SHADOW measurement. Never a prompt input or gate.

Computed only from the daily frame the BUY path already fetched for its trend
facts, plus the decision's own scenario. Anything not derivable is MISSING with a
reason; nothing is estimated from neighbouring data.

Features answer the review of 2026-09-27: inputs the BUY rubric asks for but the
agent reported as unavailable (time-of-day volume, accumulation in the US,
extension at the decision price). Their value is measured later against the
decision's forward returns; they are not evidence of an LLM effect.
"""
from __future__ import annotations

import math
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

CONTRACT_VERSION = "decision_inputs_v1"
SESSIONS = {"KR": ("Asia/Seoul", time(9, 0), time(15, 30)), "US": ("America/New_York", time(9, 30), time(16, 0))}
_ALIASES = {"open": ("Open", "open", "시가"), "high": ("High", "high", "고가"), "low": ("Low", "low", "저가"),
            "close": ("Close", "close", "종가"), "volume": ("Volume", "volume", "거래량")}


def bars_from_frame(frame, limit=80):
    """Frame (any provider column case) -> sorted [{date, open, high, low, close, volume}]."""
    rows = []
    for index, row in frame.tail(limit).iterrows():
        label = index.date() if hasattr(index, "date") else date.fromisoformat(str(index)[:10])
        values = {}
        for field, names in _ALIASES.items():
            raw = next((row[name] for name in names if name in row), None)
            values[field] = None if raw is None else float(raw)
        rows.append({"date": label.isoformat(), **values})
    rows.sort(key=lambda r: r["date"])
    return rows


def _valid(bar):
    return all(bar.get(k) is not None and math.isfinite(bar[k]) and bar[k] > 0
               for k in ("open", "high", "low", "close")) and bar.get("volume") is not None \
        and math.isfinite(bar["volume"]) and bar["volume"] >= 0


def elapsed_fraction(market, observed_at):
    zone, start, end = SESSIONS[market]
    local = observed_at.astimezone(ZoneInfo(zone))
    opened = datetime.combine(local.date(), start, ZoneInfo(zone))
    closed = datetime.combine(local.date(), end, ZoneInfo(zone))
    if local <= opened:
        return 0.0
    if local >= closed:
        return 1.0
    return (local - opened) / (closed - opened)


def _pct(a, b):
    return None if a is None or b in (None, 0) else round((a / b - 1) * 100, 4)


def compute(bars, *, market, observed_at, current_price=None, scenario=None):
    """Return {status, features, missing} for one decision."""
    features, missing = {"contract_version": CONTRACT_VERSION}, {}
    zone = SESSIONS[market][0]
    today = observed_at.astimezone(ZoneInfo(zone)).date().isoformat()
    rows = [b for b in bars if _valid(b)]
    if len(rows) != len(bars):
        missing["invalid_bars"] = len(bars) - len(rows)
    forming = rows[-1] if rows and rows[-1]["date"] == today and elapsed_fraction(market, observed_at) < 1 else None
    completed = [b for b in rows if b["date"] < today or (b["date"] == today and forming is None)]
    fraction = elapsed_fraction(market, observed_at)
    features.update(session_elapsed_fraction=round(fraction, 4), forming_bar_present=forming is not None)
    if len(completed) < 26:
        missing["completed_bars"] = len(completed)
        return {"status": "MISSING", "features": features, "missing": missing}
    last, prior20 = completed[-1], completed[-21:-1]
    avg20 = sum(b["volume"] for b in completed[-20:]) / 20
    features["rvol_last_completed"] = round(last["volume"] / (sum(b["volume"] for b in prior20) / 20), 4) \
        if sum(b["volume"] for b in prior20) > 0 else None
    # Linear time scaling overstates early-session volume (U-shaped intraday profile);
    # it is recorded as an approximation and bucketed, never used as a signal.
    if forming is not None and 0.05 <= fraction < 1 and avg20 > 0:
        features["rvol_time_scaled_linear"] = round(forming["volume"] / (avg20 * fraction), 4)
    else:
        missing["rvol_time_scaled_linear"] = "no_forming_bar" if forming is None else "session_fraction_out_of_range"
    true_ranges = []
    for prev, bar in zip(completed[-21:-1], completed[-20:]):
        true_ranges.append(max(bar["high"] - bar["low"], abs(bar["high"] - prev["close"]), abs(bar["low"] - prev["close"])))
    atr20_pct = sum(true_ranges) / 20 / last["close"] * 100
    features["atr20_pct"] = round(atr20_pct, 4)
    price = current_price if current_price and current_price > 0 else (forming or last)["close"]
    if price:
        move = _pct(price, last["close"])
        features.update(price_basis=float(price), move_vs_prev_close_pct=move,
                        move_atr_multiple=round(move / atr20_pct, 4) if move is not None and atr20_pct else None,
                        dist_ma20_pct=_pct(price, sum(b["close"] for b in completed[-20:]) / 20),
                        dist_20d_high_pct=_pct(price, max(b["high"] for b in completed[-20:])))
    else:
        missing["price_basis"] = "no_decision_price"
    if forming is not None:
        gap = _pct(forming["open"], last["close"])
        features.update(gap_open_pct=gap, gap_atr_multiple=round(gap / atr20_pct, 4) if gap is not None and atr20_pct else None)
    else:
        missing["gap_open_pct"] = "no_forming_bar"
    up = sum(b["volume"] for p, b in zip(completed[-21:-1], completed[-20:]) if b["close"] > p["close"])
    down = sum(b["volume"] for p, b in zip(completed[-21:-1], completed[-20:]) if b["close"] < p["close"])
    features["up_down_volume_ratio_20"] = round(up / down, 4) if down > 0 else None
    window = list(zip(completed[-26:-1], completed[-25:]))
    features["accumulation_days_25"] = sum(b["close"] >= p["close"] * 1.002 and b["volume"] > p["volume"] for p, b in window)
    features["distribution_days_25"] = sum(b["close"] <= p["close"] * 0.998 and b["volume"] > p["volume"] for p, b in window)
    scenario = scenario or {}
    for key in ("buy_score", "effective_score", "min_score", "momentum_signal_count", "additional_confirmation_count"):
        value = scenario.get(key)
        features["scenario_" + key] = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    rvol = features.get("rvol_time_scaled_linear") or 0
    features["rubric_probe"] = {
        # Would the rubric's volume momentum item be satisfiable with time-scaled volume?
        "volume_item_time_scaled": bool(rvol >= 2.0),
        "volume_item_last_completed": bool((features.get("rvol_last_completed") or 0) >= 2.0),
        "accumulation_ratio_ge_1_5": bool((features.get("up_down_volume_ratio_20") or 0) >= 1.5),
        "extended_move_ge_2_atr": bool((features.get("move_atr_multiple") or 0) >= 2.0),
    }
    return {"status": "OK" if not missing else "PARTIAL", "features": features, "missing": missing}

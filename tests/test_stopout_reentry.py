from datetime import date, timedelta

import pytest

from prism_core import stopout_reentry as R


def _bars(closes, volumes=None, start=date(2026, 1, 1), opens=None, lows=None, highs=None):
    rows, day = [], start
    for i, close in enumerate(closes):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        o = opens[i] if opens and opens[i] is not None else close
        low = lows[i] if lows and lows[i] is not None else min(o, close) * 0.995
        high = highs[i] if highs and highs[i] is not None else max(o, close) * 1.005
        rows.append({"date": day.isoformat(), "open": o, "high": high, "low": low, "close": close,
                     "volume": (volumes[i] if volumes else 1000)})
        day += timedelta(days=1)
    return rows


def _setup(after_closes, after_volumes=None, **kw):
    """60 rising base bars, entry at bar 60 (100), stop-out at bar 62 (93)."""
    base = [80 + i * 0.3 for i in range(60)]           # MA50 rising, pivot ~97.7
    closes = base + [100, 96, 93] + after_closes
    volumes = [1000] * 63 + (after_volumes or [1000] * len(after_closes))
    bars = _bars(closes, volumes, **kw)
    watch = R.enroll("KR", "acct", "000001", bars[60]["date"], 100, bars[62]["date"], 93, bars)
    return watch, bars


def test_enroll_freezes_pivot_before_entry_and_requires_history():
    watch, bars = _setup([94])
    assert watch["pivot"] == pytest.approx(max(b["high"] for b in bars[40:60]))
    assert watch["reclaim_level"] == 100
    short = R.enroll("KR", "a", "x", bars[10]["date"], 100, bars[12]["date"], 93, bars)
    assert short["status"] == "MISSING"
    assert R.evaluate(short, bars)["status"] == "MISSING"


def test_rejects_inconsistent_bars():
    watch, bars = _setup([94])
    bad = [dict(b) for b in bars]
    bad[-1]["low"] = bad[-1]["close"] * 1.1
    assert R.evaluate(watch, bad) == {**R.evaluate(watch, bad), "status": "MISSING", "reason": "inconsistent_bar"}
    dup = bars + [dict(bars[-1])]
    assert R.evaluate(watch, dup)["reason"] == "duplicate_bar"


def test_deep_failure_invalidates():
    watch, bars = _setup([90, 84])
    out = R.evaluate(watch, bars)
    assert out["status"] == "INVALIDATED" and out["reason"] == "deep_failure"
    assert out["events"] == []


def test_reclaim_requires_volume_then_pullback_signal():
    after = [95, 97, 99, 102, 104, 106, 103, 101.5, 104]
    vols = [1000, 1000, 1000, 2000, 1500, 1200, 600, 600, 900]
    watch, bars = _setup(after, vols)
    out = R.evaluate(watch, bars)
    kinds = [e["kind"] for e in out["events"]]
    assert kinds == ["RECLAIM", "PULLBACK"]
    assert out["status"] == "SIGNALLED"
    reclaim, pullback = out["events"]
    assert reclaim["date"] == bars[66]["date"] and reclaim["volume_ratio"] == pytest.approx(2.0)
    assert pullback["pullback_depth"] < -R.PULLBACK_MIN
    # Events after the last completed bar are not yet tradable.
    assert pullback["trade"]["status"] == "PENDING"


def test_reclaim_without_volume_is_not_a_signal():
    watch, bars = _setup([95, 97, 99, 102, 104], [1000] * 5)
    out = R.evaluate(watch, bars)
    assert out["status"] == "WATCHING" and out["events"] == []


def test_asof_excludes_forming_bar_and_ids_are_stable():
    after = [95, 97, 99, 102]
    watch, bars = _setup(after, [1000, 1000, 1000, 2000])
    forming = R.evaluate(watch, bars, asof=bars[-1]["date"])
    assert forming["events"] == []
    first = R.evaluate(watch, bars)
    again = R.evaluate(first, bars)
    assert [e["event_id"] for e in first["events"]] == [e["event_id"] for e in again["events"]]


def test_window_expires_without_reclaim():
    watch, bars = _setup([95] * (R.WINDOW_BARS + 1))
    out = R.evaluate(watch, bars)
    assert out["status"] == "EXPIRED" and out["reason"] == "window_elapsed_without_reclaim"


def test_reclaim_failure_invalidates():
    watch, bars = _setup([95, 97, 99, 102, 94], [1000, 1000, 1000, 2000, 1000])
    out = R.evaluate(watch, bars)
    assert out["status"] == "INVALIDATED" and out["reason"] == "reclaim_failed"
    assert [e["kind"] for e in out["events"]] == ["RECLAIM"]


def _event(kind="RECLAIM", level=100.0, swing=95.0, **extra):
    return {"kind": kind, "date": extra.pop("date", "2026-03-02"), "reclaim_level": level, "swing_low": swing, **extra}


def _trade_bars(closes, opens=None, lows=None):
    # 20 flat bars for MA20, then the event day 2026-03-02, then the trade.
    base = _bars([100] * 20, start=date(2026, 2, 2))
    event = _bars([100], start=date(2026, 3, 2))
    rest = _bars(closes, start=date(2026, 3, 3), opens=opens, lows=lows)
    return base + event + rest


def test_simulate_skips_chase_above_buy_zone():
    trade = R.simulate(_trade_bars([110], opens=[106]), _event())
    assert trade["status"] == "SKIPPED" and trade["reason"] == "entry_above_buy_zone"


def test_simulate_gap_through_stop_exits_at_open():
    bars = _trade_bars([101, 90], opens=[100, 89], lows=[99.5, 88])
    trade = R.simulate(bars, _event(swing=90))
    assert trade["stop"] == pytest.approx(93.0)
    assert trade["exit_reason"] == "stop" and trade["ret"] == pytest.approx(-0.11)


def test_simulate_breakeven_and_trend_exit():
    rising = [101, 105, 111, 112]
    bars = _trade_bars(rising + [99.8], opens=[100] + [None] * 4, lows=[None] * 4 + [99.5])
    trade = R.simulate(bars, _event())
    # The bar opens below the lifted stop, so the gap exits at the open.
    assert trade["exit_reason"] == "breakeven_stop" and trade["ret"] == pytest.approx(-0.002)
    bars = _trade_bars([101, 102, 103, 99], opens=[100, None, None, None])
    trade = R.simulate(bars, _event())
    assert trade["exit_reason"] in {"trend_exit_ma20", "stop"}


def test_simulate_horizon_and_pending():
    bars = _trade_bars([100 + i * 0.5 for i in range(R.HOLD_BARS)], opens=[100] + [None] * (R.HOLD_BARS - 1))
    trade = R.simulate(bars, _event())
    assert trade["status"] == "CLOSED" and trade["exit_reason"] == "horizon"
    trade = R.simulate(bars[:-5], _event())
    assert trade["status"] == "PENDING"


def test_hold_counterfactual():
    watch, bars = _setup([95] * R.WINDOW_BARS)
    out = R.hold_counterfactual(watch, bars)
    assert out["status"] == "CLOSED" and out["ret"] == pytest.approx(-0.05) and not out["recovered_entry"]
    assert R.hold_counterfactual(watch, bars[:-3])["status"] == "PENDING"


def test_simulate_requires_ma20_history():
    bars = _bars([100] * 10, start=date(2026, 3, 2))
    assert R.simulate(bars, _event(date=bars[2]["date"]))["status"] == "MISSING"


def test_pullback_deeper_than_max_invalidates():
    after = [95, 97, 99, 102, 104, 106, 92]
    vols = [1000, 1000, 1000, 2000, 1500, 1200, 600]
    watch, bars = _setup(after, vols)
    out = R.evaluate(watch, bars)
    assert out["status"] == "INVALIDATED" and out["reason"] in {"pullback_too_deep", "reclaim_failed"}
    after = [95, 97, 99, 102, 110, 125, 108]
    watch, bars = _setup(after, vols)
    out = R.evaluate(watch, bars)
    assert out["status"] == "INVALIDATED" and out["reason"] == "pullback_too_deep"


def test_downtrend_before_reclaim_keeps_watching_and_reclaim_needs_ma50():
    base = [120 - i * 0.4 for i in range(60)]           # falling MA50 around ~108
    closes = base + [100, 96, 93, 94, 95]
    bars = _bars(closes, [1000] * 63 + [3000, 3000])
    watch = R.enroll("KR", "a", "x", bars[60]["date"], 100, bars[62]["date"], 93, bars)
    out = R.evaluate(watch, bars)
    assert out["status"] == "WATCHING" and out["events"] == []
    # Above the entry but still under MA50: no reclaim.
    bars = _bars(closes[:63] + [101, 102], [1000] * 63 + [3000, 3000])
    watch = R.enroll("KR", "a", "x", bars[60]["date"], 100, bars[62]["date"], 93, bars)
    assert R.evaluate(watch, bars)["events"] == []

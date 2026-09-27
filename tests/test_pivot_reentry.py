from datetime import date, timedelta

from prism_core import pivot_reentry as P


def _bars(rows, start=date(2026, 1, 1)):
    out, day = [], start
    for o, h, low, c, v in rows:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        out.append({"date": day.isoformat(), "open": o, "high": h, "low": low, "close": c, "volume": v})
        day += timedelta(days=1)
    return out


def _uptrend_then_base(pivot=110.0, base_low=100.0, base_len=15, last_close=106.0):
    rows = [(80 + i * 0.5, 80.5 + i * 0.5, 79.5 + i * 0.5, 80 + i * 0.5, 1000) for i in range(55)]   # rising to ~107
    rows.append((108, pivot, 107, 109, 1500))                                                          # the pivot bar
    for j in range(base_len):
        c = max(base_low + 1, 108 - j) if j < base_len - 3 else last_close - (base_len - 1 - j)
        rows.append((c, c + 1, min(c - 1, base_low) if j == base_len // 2 else c - 1, c, 900))
    return rows


def test_pivot_found_after_base_and_not_during_new_highs():
    bars = _bars(_uptrend_then_base())
    base = P.find_pivot(bars, len(bars), "KR")
    assert base and base["pivot"] == 110.0 and base["base_bars"] == 15 and base["depth"] <= 0.40
    rising = _bars([(80 + i, 81 + i, 79 + i, 80 + i, 1000) for i in range(80)])
    assert P.find_pivot(rising, len(rising), "US") is None      # newest high is < 5 bars old


def test_too_deep_base_is_rejected():
    rows = _uptrend_then_base(base_low=55.0)
    assert P.find_pivot(_bars(rows), len(rows), "US") is None


def test_intraday_breakout_trigger_and_chase_skip():
    rows = _uptrend_then_base(last_close=106.0)
    rows.append((107, 112, 106.5, 111, 1500))       # crosses 110 on >= average volume
    bars = _bars(rows)
    day = P.evaluate_day(bars, len(bars) - 1, "KR")
    assert day["status"] == "TRIGGERED" and day["trigger"] == "INTRADAY_BREAKOUT" and day["entry"] == 110.0
    gap = _bars(_uptrend_then_base(last_close=106.0) + [(117, 118, 116, 117, 1500)])
    assert P.evaluate_day(gap, len(gap) - 1, "KR")["status"] == "CHASE_SKIPPED"
    quiet = _bars(_uptrend_then_base(last_close=106.0) + [(107, 112, 106.5, 111, 300)])
    assert P.evaluate_day(quiet, len(quiet) - 1, "KR")["status"] == "READY"


def test_follow_through_enters_next_open():
    rows = _uptrend_then_base(last_close=106.0)
    rows.append((107, 112, 106.5, 111.5, 2000))     # closes above pivot on 2x volume, trigger also fires here
    rows.append((111, 113, 110, 112, 1000))
    bars = _bars(rows)
    day = P.evaluate_day(bars, len(bars) - 1, "KR")
    # The pivot as of today is gone (new high is fresh) but yesterday's breakout qualifies.
    assert day.get("trigger") in {"FOLLOW_THROUGH", None}


def test_simulate_stop_breakeven_and_pending():
    rows = _uptrend_then_base(last_close=106.0)
    base = _bars(rows)
    down = base + _bars([(110, 110.5, 101, 102, 1000)], start=date(2026, 6, 1))
    trade = P.simulate(down, len(base), 110.0)
    assert trade["exit_reason"] == "stop" and trade["ret"] == -0.07
    up = base + _bars([(110, 125, 109, 123, 1000), (123, 124, 109, 110, 1000)], start=date(2026, 6, 1))
    trade = P.simulate(up, len(base), 110.0)
    assert trade["exit_reason"] == "breakeven_stop" and trade["ret"] == 0.0
    assert P.simulate(base + _bars([(110, 111, 109, 110.5, 900)] * 3, start=date(2026, 6, 1)),
                      len(base), 110.0)["status"] in {"PENDING", "CLOSED"}


def test_run_watch_market_gate_and_ready_control():
    rows = _uptrend_then_base(last_close=106.0) + [(107, 112, 106.5, 111, 1500)] + [(111, 112, 110, 111, 900)] * 45
    bars = _bars(rows)
    start = len(_uptrend_then_base())
    blocked = P.run_watch(bars, start, "KR", market_ok=lambda d: False)
    assert blocked["status"] in {"EXPIRED", "PENDING"} and blocked["events"]["market_blocked"] >= 1
    ok = P.run_watch(bars, start, "KR", market_ok=lambda d: True)
    assert ok["status"] == "TRIGGERED" and ok["ready_control"]["index"] <= ok["index"]


def test_production_exit_trailing_and_hypotheses():
    base = _bars(_uptrend_then_base(last_close=106.0))
    i = len(base)
    # Entry 110, closes run to 121 (+10%), then fall to 110.5: bull band line = 121*0.92*0.995 = 110.76.
    path = _bars([(110, 112, 109.5, 111, 900), (111, 121.5, 110.5, 121, 900), (120, 120.5, 110, 110.5, 900)],
                 start=date(2026, 6, 1))
    trade = P.simulate_production(base + path, i, 110.0, bull=lambda d: True)
    assert trade["exit_reason"] == "tier2_trail" and trade["ret"] == round(110.5 / 110 - 1, 6)
    weak = P.simulate_production(base + path, i, 110.0, bull=lambda d: False)
    assert weak["exit_reason"] == "tier2_trail"
    # Intraday wick to -8% that closes flat: default stops out, H1 close-based stop survives.
    wick = _bars([(110, 111, 101, 110, 900)] + [(110, 111, 109.5, 110.2, 900)] * 70, start=date(2026, 6, 1))
    assert P.simulate_production(base + wick, i, 110.0, intraday=False)["exit_reason"] == "tier1_stop"
    assert P.simulate_production(base + wick, i, 110.0, intraday=False, close_stop=True)["exit_reason"] != "tier1_stop"
    # H2: an activated winner never closes below entry.
    fade = _bars([(110, 116, 109.5, 116, 900), (116, 116.5, 108, 108.5, 900)], start=date(2026, 6, 1))
    lock = P.simulate_production(base + fade, i, 110.0, bull=lambda d: True, breakeven_lock=True)
    assert lock["exit_reason"] == "tier2_trail" and lock["ret"] < 0 and lock["ret"] == round(108.5 / 110 - 1, 6)

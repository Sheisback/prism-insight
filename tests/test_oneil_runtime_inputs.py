"""Real production gate and input bridge, isolated from network and orders."""
from copy import deepcopy

import pytest

from prism_core.oneil_adaptive_policy import _time
from prism_core.oneil_current_capture import capture_current_record
from prism_core.oneil_runtime_inputs import current_gates, quote_input, IntradayProvider, market_snapshot_from_frames
from test_oneil_current_capture import arguments, decision


def fixtures():
    args = arguments()
    response = dict(symbol="TEST", currency="USD", regularMarketPrice=104,
                    regularMarketTime=_time(args["now"]).timestamp())
    quote = quote_input(plan=args["plan"], position_id=args["position_id"], response=response, now=args["now"])
    scenario = dict(decision="entry", buy_score=8, target_price=130, stop_loss=100,
                    max_portfolio_size=10)
    portfolio = dict(observed_at=args["now"], source_ref="portfolio:1", account_key="demo",
                     positions=[dict(position_id=args["position_id"], symbol="TEST", account_key="demo",
                                     source_decision_ref=args["plan"]["source_decision_ref"])],
                     slots_used=1, max_slots=10)
    market = dict(observed_at=args["now"], source_ref="market:1", regime="strong_bull",
                  market_pulse="UPTREND", pilot_reexposure_active=False)
    gates_args = dict(plan=args["plan"], position_id=args["position_id"], scenario=scenario,
                      quote=quote, portfolio=portfolio, market=market, now=args["now"])
    return args, response, gates_args


def test_real_quote_gate_capture_policy_chain():
    args, _, gate_args = fixtures()
    original = deepcopy(gate_args)
    gates = current_gates(**gate_args)
    assert all(gates[k] for k in ("admission", "RR", "risk", "sector", "slot"))
    record = capture_current_record(**{**args, "quote": gate_args["quote"], "gates": gates})
    assert record["status"] == "OK"
    assert decision(args, record)["action"] == "ADD"
    assert gate_args == original


@pytest.mark.parametrize("field,value", [("regularMarketTime", None), ("regularMarketTime", True),
                                         ("regularMarketTime", 1), ("currency", "KRW"),
                                         ("symbol", "OTHER"), ("regularMarketPrice", float("nan"))])
def test_quote_never_relabels_unknown_or_stale(field, value):
    args, response, _ = fixtures()
    response[field] = value
    with pytest.raises((ValueError, TypeError)):
        quote_input(plan=args["plan"], position_id=args["position_id"], response=response, now=args["now"])


def test_independent_gate_failures_do_not_forge_risk_sector_slot():
    _, _, kwargs = fixtures()
    kwargs["scenario"]["buy_score"] = 0
    gates = current_gates(**kwargs)
    assert not gates["admission"]
    assert gates["risk"] and gates["RR"] and gates["sector"] and gates["slot"]
    kwargs["scenario"]["target_price"] = 105
    assert current_gates(**kwargs)["RR"] is False
    kwargs["scenario"]["stop_loss"] = 80
    assert current_gates(**kwargs)["risk"] is False
    kwargs["portfolio"]["slots_used"] = 11
    kwargs["portfolio"]["positions"].extend(
        dict(position_id=f"other:{i}", symbol=f"OTHER{i}", account_key="demo") for i in range(10))
    assert current_gates(**kwargs)["slot"] is False
    kwargs["portfolio"]["positions"][0]["account_key"] = "other"
    with pytest.raises(ValueError, match="OWNERSHIP"):
        current_gates(**kwargs)


@pytest.mark.parametrize("snapshot,field,value", [("market", "observed_at", "2026-09-25T13:30:00Z"),
    ("market", "regime", None), ("market", "market_pulse", None),
    ("market", "pilot_reexposure_active", None), ("portfolio", "source_ref", None),
    ("portfolio", "slots_used", -1)])
def test_raw_snapshot_unknown_is_not_a_fresh_boolean(snapshot, field, value):
    _, _, kwargs = fixtures()
    kwargs[snapshot][field] = value
    with pytest.raises(ValueError):
        current_gates(**kwargs)


def test_pilot_freeze_and_scenario_cap_remain_enforced():
    _, _, kwargs = fixtures()
    kwargs["market"]["pilot_reexposure_active"] = True
    kwargs["scenario"]["max_portfolio_size"] = 1
    gates = current_gates(**kwargs)
    assert not gates["admission"] and gates["slot"]


def test_existing_campaign_can_grow_at_full_slots_but_not_over_cap():
    _, _, kwargs = fixtures()
    kwargs["scenario"]["max_portfolio_size"] = 1
    assert current_gates(**kwargs)["slot"]
    kwargs["portfolio"]["positions"].append(dict(position_id="other", symbol="OTHER", account_key="demo"))
    kwargs["portfolio"]["slots_used"] = 2
    assert not current_gates(**kwargs)["slot"]
    kwargs["portfolio"]["positions"].pop(0)
    kwargs["portfolio"]["slots_used"] = 1
    with pytest.raises(ValueError, match="OWNERSHIP"):
        current_gates(**kwargs)


def test_slot_count_must_match_raw_position_rows():
    _, _, kwargs = fixtures()
    kwargs["portfolio"]["slots_used"] = 2
    with pytest.raises(ValueError, match="CAP_INVALID"):
        current_gates(**kwargs)


def test_duplicate_campaign_ownership_is_rejected():
    _, _, kwargs = fixtures()
    kwargs["portfolio"]["positions"] *= 2
    kwargs["portfolio"]["slots_used"] = 2
    with pytest.raises(ValueError, match="OWNERSHIP"):
        current_gates(**kwargs)


def test_new_phase_uses_real_empty_portfolio_and_normal_sector_slot_rules():
    _, _, kwargs = fixtures()
    kwargs["portfolio"].update(positions=[], slots_used=0, scenario_sectors=[], max_same_sector=3,
                               sector_concentration_ratio=.3, minimum_holdings_for_ratio=4)
    kwargs["scenario"]["sector"] = "Technology"
    kwargs["scenario"]["decision"] = "Enter"
    gate = current_gates(**kwargs, phase="NEW")
    assert gate["sector"] and gate["slot"] and gate["admission"]
    kwargs["portfolio"].update(positions=[dict(symbol=f"OTHER{i}") for i in range(3)],
                               slots_used=3, scenario_sectors=["Technology"] * 3)
    assert not current_gates(**kwargs, phase="NEW")["sector"]
    kwargs["portfolio"]["positions"][0]["symbol"] = "TEST"
    with pytest.raises(ValueError, match="ALREADY_HELD"):
        current_gates(**kwargs, phase="NEW")


def test_original_adjusted_score_is_authoritative_not_raw_score():
    _, _, kwargs = fixtures()
    kwargs["scenario"]["_decision_context"] = {"adjusted_score": 0}
    assert not current_gates(**kwargs)["admission"]
    kwargs["scenario"]["buy_score"] = 0
    kwargs["scenario"]["_decision_context"]["adjusted_score"] = 8
    assert current_gates(**kwargs)["admission"]


def test_intraday_reuses_only_complete_history_chunks(monkeypatch):
    import tools.build_oneil_adaptive_inputs as producer
    from test_oneil_input_collection import source
    supplied = source()
    requests = []
    historical = dict(supplied["datasets"][0]["request"], end="2026-09-24T20:00:00Z")
    current = supplied["datasets"][0]["request"]

    def fetcher(request):
        requests.append(request)
        return deepcopy(supplied["datasets"][0]["response"])

    def collect(symbol, as_of, calendar_name, fetcher):
        fetcher(historical)
        return dict(supplied, datasets=[dict(supplied["datasets"][0], response=fetcher(current))])

    monkeypatch.setattr(producer, "collect_source", collect)
    provider = IntradayProvider(fetcher)
    first = provider("TEST", supplied["as_of"], "NYSE")
    second = provider("TEST", supplied["as_of"], "NYSE")
    assert first == second and first["kind"] == "LIVE_CAPTURE"
    assert first["usable_for_prospective"]
    assert len(requests) == 3


def market_frames():
    import pandas as pd
    import pandas_market_calendars as calendars
    days = calendars.get_calendar("NYSE").schedule("2025-01-01", "2026-09-25").index[-251:]
    frame = pd.DataFrame({"Close": [100 + i * .1 for i in range(len(days))],
                          "Volume": [1000000] * len(days)}, index=days)
    return {"^GSPC": frame, "^IXIC": frame.copy(), "^VIX": frame.assign(Close=15)}


def test_real_market_regime_pulse_compute_preserves_completed_source_date():
    frames = market_frames()
    result = market_snapshot_from_frames(frames, now="2026-09-25T14:00:00Z", pilot_flag=True)
    assert result["regime"] in {"strong_bull", "moderate_bull"}
    assert result["market_pulse"] in {"UPTREND", "UNDER_PRESSURE", "CORRECTION"}
    assert all(value.startswith("2026-09-24") for value in result["source_asof"].values())
    assert result["observed_at"] == "2026-09-25T14:00:00Z"
    assert type(result["pilot_reexposure_active"]) is bool


@pytest.mark.parametrize("kind", ["stale", "empty", "duplicate", "nan"])
def test_market_missing_never_defaults_sideways(kind):
    frames = market_frames()
    frame = frames["^GSPC"]
    if kind == "stale":
        frames["^GSPC"] = frame.iloc[:-4]
    elif kind == "empty":
        frames["^GSPC"] = frame.iloc[:0]
    elif kind == "duplicate":
        import pandas as pd
        frames["^GSPC"] = pd.concat([frame, frame.tail(1)])
    else:
        frames["^GSPC"].iloc[-3, 0] = float("nan")
    with pytest.raises(ValueError):
        market_snapshot_from_frames(frames, now="2026-09-25T14:00:00Z", pilot_flag=True)


@pytest.mark.parametrize("symbol", ["^GSPC", "^IXIC", "^VIX"])
@pytest.mark.parametrize("kind", ["internal_gap", "off_calendar", "same_date"])
def test_market_session_continuity_is_required(symbol, kind):
    import pandas as pd
    frames = market_frames()
    frame = frames[symbol]
    if kind == "internal_gap":
        frame = frame.drop(frame.index[-10])
    else:
        extra = frame.iloc[-10:-9].copy()
        extra.index = [pd.Timestamp("2026-09-19") if kind == "off_calendar"
                       else extra.index[0] + pd.Timedelta(hours=1)]
        frame = pd.concat([frame, extra])
    frames[symbol] = frame
    if symbol == "^VIX" and kind == "off_calendar":
        result = market_snapshot_from_frames(frames, now="2026-09-25T14:00:00Z", pilot_flag=True)
        assert result["alignment"]["^VIX"]["excluded_dates"] == ["2026-09-19"]
        return
    with pytest.raises(ValueError, match="SESSION_GAP_OR_AMBIGUITY"):
        market_snapshot_from_frames(frames, now="2026-09-25T14:00:00Z", pilot_flag=True)


def test_truncated_bullish_thirty_rows_never_fall_back_to_twenty_day_regime():
    frames = market_frames()
    frames["^GSPC"] = frames["^GSPC"].tail(31)
    with pytest.raises(ValueError, match="SHORT"):
        market_snapshot_from_frames(frames, now="2026-09-25T14:00:00Z", pilot_flag=True)


def test_intraday_cache_preserves_other_symbols_and_is_bounded(monkeypatch):
    import tools.build_oneil_adaptive_inputs as producer
    requests = []

    def collect(symbol, as_of, calendar_name, fetcher):
        request = dict(ticker=symbol, start="2026-09-01T00:00:00Z", end="2026-09-02T00:00:00Z")
        fetcher(request)
        return {}

    monkeypatch.setattr(producer, "collect_source", collect)
    monkeypatch.setattr(producer, "build_packet", lambda *a, **k: {"intraday": {}})
    def fetcher(request):
        requests.append(request)
        return {"status": "received"}
    provider = IntradayProvider(fetcher)
    for symbol in ("AAA", "BBB", "AAA", "BBB"):
        provider(symbol, "2026-09-25T14:00:00Z", "NYSE")
    assert len(requests) == 2
    for suffix in "CDEFGHIJK":
        provider(suffix, "2026-09-25T14:00:00Z", "NYSE")
    assert len(provider.cache) == 10
    provider("K", "2026-09-28T14:00:00Z", "NYSE")
    assert len(provider.cache) == 10
    assert ("K", "NYSE", "2026-09-25") not in provider.cache


def test_provider_vix_holiday_extras_are_audited_not_used_as_equity_sessions():
    import pandas as pd
    frames = market_frames()
    frames["^VIX"]["Volume"] = 0
    baseline = market_snapshot_from_frames(frames, now="2026-09-25T14:00:00Z", pilot_flag=True)
    extras = pd.DataFrame({"Close": [99, 99], "Volume": [0, 0]},
                          index=pd.to_datetime(["2026-05-25", "2026-09-07"]))
    frames["^VIX"] = pd.concat([frames["^VIX"], extras])
    result = market_snapshot_from_frames(frames, now="2026-09-25T14:00:00Z", pilot_flag=True)
    assert result["regime"] == baseline["regime"]
    assert result["alignment"]["^VIX"]["excluded_dates"] == ["2026-05-25", "2026-09-07"]
    assert result["source_ref"] != baseline["source_ref"]


def test_market_pulse_uses_both_us_indexes(monkeypatch):
    """S&P 500 >=10% drawdown alone reads UNDER_PRESSURE; both indexes -> CORRECTION."""
    frames = market_frames()
    spx = frames["^GSPC"].copy()
    spx.iloc[-5:, 0] = spx["Close"].iloc[-6] * .88
    frames["^GSPC"] = spx
    monkeypatch.delenv("US_MARKET_PULSE_INDEX_MODE", raising=False)
    one = market_snapshot_from_frames(frames, now="2026-09-25T14:00:00Z", pilot_flag=False)
    assert one["market_pulse"] == "UNDER_PRESSURE"
    frames["^IXIC"] = spx.copy()
    both = market_snapshot_from_frames(frames, now="2026-09-25T14:00:00Z", pilot_flag=False)
    assert both["market_pulse"] == "CORRECTION"
    frames["^IXIC"] = market_frames()["^IXIC"]
    monkeypatch.setenv("US_MARKET_PULSE_INDEX_MODE", "spx")
    rollback = market_snapshot_from_frames(frames, now="2026-09-25T14:00:00Z", pilot_flag=False)
    assert rollback["market_pulse"] == "CORRECTION"

"""Read-only runtime inputs for the separate adaptive lane; never places orders.

Snapshots contain raw portfolio/market facts, not caller-supplied gate booleans.
Their observation times must be retained by the source. This module deliberately
does not relabel a batch regime or an undated database row as a current snapshot.
"""

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import importlib.util
import multiprocessing
from pathlib import Path
import re
import sys

from prism_core.oneil_adaptive_policy import _hash, _num, _time, _validate


def _root_module(name, relative):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / relative)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _identity(plan, position_id):
    return dict(symbol=plan["symbol"], position_id=position_id,
                source_decision_ref=plan["source_decision_ref"],
                price_basis_ref=plan["setup"]["price_basis_ref"])


def quote_input(*, plan, position_id, response, now):
    """Bind provider price to its real exchange timestamp, not retrieval time."""
    _validate(plan)
    if response.get("symbol") != plan["symbol"] or response.get("currency") != "USD":
        raise ValueError("QUOTE_IDENTITY_OR_CURRENCY")
    price = _num(response["regularMarketPrice"], True)
    timestamp = response["regularMarketTime"]
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        raise ValueError("QUOTE_TIME_MISSING")
    observed = datetime.fromtimestamp(timestamp, timezone.utc)
    if not 0 <= (_time(now) - observed).total_seconds() <= 120:
        raise ValueError("QUOTE_STALE_OR_FUTURE")
    return dict(_identity(plan, position_id), price=str(price), observed_at=observed.isoformat(),
                source_ref=_hash(["yfinance-regular-market", response]))


def _quote_child(symbol, connection):
    import contextlib
    import io
    import logging
    import tempfile
    logging.disable(logging.CRITICAL)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), tempfile.TemporaryDirectory() as cache:
            import yfinance as yf
            yf.set_tz_cache_location(cache)
            info = yf.Ticker(symbol).get_info()
            result = {key: info.get(key) for key in
                      ("symbol", "currency", "regularMarketPrice", "regularMarketTime", "exchange")}
    except Exception:
        result = {}
    connection.send(result)
    connection.close()


def fetch_quote(symbol, timeout=15):
    """Subprocess deadline bounds all provider calls, including metadata fetches."""
    if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", symbol) or not 0 < timeout <= 40:
        raise ValueError("QUOTE_REQUEST_INVALID")
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_quote_child, args=(symbol, send), daemon=True)
    process.start()
    send.close()
    try:
        return receive.recv() if receive.poll(timeout) else {}
    except EOFError:
        return {}
    finally:
        receive.close()
        process.join(.1)
        if process.is_alive():
            process.terminate()
            process.join(1)
        if process.is_alive():
            process.kill()
            process.join(1)


def current_gates(*, plan, position_id, scenario, quote, portfolio, market, now, phase="ADD"):
    """Recompute existing underwriting, slot and held-name sector rules.

    risk is the existing stop-width/arithmetic gate, not broker buying power.
    The adaptive evaluator independently clips original campaign risk. Broker
    account cash and confirmed ownership remain execution-boundary obligations.
    """
    identity = _identity(plan, position_id)
    if any(quote.get(k) != v for k, v in identity.items()):
        raise ValueError("QUOTE_BINDING")
    for snapshot in (portfolio, market):
        if not snapshot.get("source_ref") or not 0 <= (_time(now) - _time(snapshot["observed_at"])).total_seconds() <= 120:
            raise ValueError("CURRENT_SNAPSHOT_UNAVAILABLE")
    held = [row for row in portfolio["positions"]
            if row.get("position_id") == position_id and row.get("symbol") == plan["symbol"]
            and row.get("account_key") == portfolio["account_key"]]
    if phase not in {"NEW", "ADD"}:
        raise ValueError("UNKNOWN_CAMPAIGN_PHASE")
    if phase == "ADD" and (len(held) != 1 or held[0].get("source_decision_ref") != plan["source_decision_ref"]):
        raise ValueError("POSITION_OWNERSHIP_UNAVAILABLE")
    if phase == "NEW" and any(row.get("symbol") == plan["symbol"] for row in portfolio["positions"]):
        raise ValueError("INITIAL_POSITION_ALREADY_HELD")
    if type(market.get("pilot_reexposure_active")) is not bool:
        raise ValueError("PILOT_STATE_UNAVAILABLE")
    if market.get("market_pulse") not in {"UPTREND", "UNDER_PRESSURE", "CORRECTION"}:
        raise ValueError("PULSE_UNAVAILABLE")
    slots = _num(portfolio["slots_used"])
    hard_max = _num(portfolio["max_slots"], True)
    if slots < (1 if phase == "ADD" else 0) or int(slots) != slots or slots != len(portfolio["positions"]) or int(hard_max) != hard_max:
        raise ValueError("PORTFOLIO_CAP_INVALID")
    # Same fallback/cap as US _scenario_slot_limit. No new slot allowance.
    try:
        requested = int(float(scenario.get("max_portfolio_size")))
    except (TypeError, ValueError, OverflowError):
        requested = int(hard_max)
    limit = min(requested, int(hard_max)) if requested > 0 else int(hard_max)
    module = _root_module("_oneil_root_buy_gate", "cores/buy_gate.py")
    if module.normalize_regime(market.get("regime")) is None:
        raise ValueError("REGIME_UNAVAILABLE")
    context = scenario.get("_decision_context") or {}
    normalized_decision = str(context.get("decision") or scenario.get("decision") or "").strip().lower()
    if normalized_decision in {"진입", "매수", "enter", "entry", "buy", "yes"}:
        normalized_decision = "entry"
    evaluated_scenario = dict(scenario, decision=normalized_decision)
    score_override = context.get("adjusted_score")
    if score_override is not None:
        score_override = float(_num(score_override))
    result = module.evaluate_production_buy_gate(
        deepcopy(evaluated_scenario), current_price=float(_num(quote["price"], True)),
        market_regime=market["regime"], market_pulse=market["market_pulse"],
        distribution_days=market.get("distribution_days"),
        score_override=score_override,
        is_add=phase == "ADD", pilot_budget_available=False,
    )
    codes = {row["code"] for row in result["hard_findings"]}
    risk_codes = {"invalid_current_price", "missing_stop", "invalid_stop", "stop_exceeds_regime_limit",
                  "risk_arithmetic_mismatch", "stop_below_volatility_noise_floor", "missing_regime_rule"}
    rr_codes = {"invalid_current_price", "missing_target", "invalid_target", "missing_stop", "invalid_stop",
                "rr_below_floor", "rr_arithmetic_mismatch", "missing_regime_rule"}
    sector_ok = True
    if phase == "NEW":
        sector = scenario.get("sector")
        sectors = portfolio["scenario_sectors"]
        if not isinstance(sectors, list) or len(sectors) > len(portfolio["positions"]):
            raise ValueError("SECTOR_COUNTS_UNAVAILABLE")
        if sector and str(sector).lower() != "unknown":
            same = sum(1 for s in sectors if s and str(s).lower() == str(sector).lower())
            sector_ok = same < _num(portfolio["max_same_sector"], True) and not (
                len(sectors) >= _num(portfolio["minimum_holdings_for_ratio"], True)
                and Decimal(same) / len(sectors) >= _num(portfolio["sector_concentration_ratio"], True))
    return dict(identity, observed_at=now, source_ref=_hash([quote, portfolio, market, scenario, result, phase]),
                quote_source_ref=quote["source_ref"], price=quote["price"],
                admission=result["allowed"] and normalized_decision == "entry" and not market["pilot_reexposure_active"],
                risk=not bool(codes & risk_codes), RR=not bool(codes & rr_codes),
                # This lane grows the verified existing campaign; unlike the
                # legacy pyramid path it creates no additional holding row.
                # Its own row is already included in the portfolio count.
                sector=sector_ok, slot=slots - (1 if phase == "ADD" else 0) < limit, market_pulse=market["market_pulse"],
                regime=result["effective_regime"], findings=sorted(codes),
                market_source_ref=market["source_ref"],
                market_source_asof=deepcopy(market.get("source_asof")),
                portfolio_source_ref=portfolio["source_ref"])


def market_snapshot_from_frames(frames, *, now, pilot_flag=None):
    """Recompute existing regime/pulse on latest completed exchange sessions.

    observed_at is computation time; source_asof retains each input's session.
    A missing latest completed session is an error, never a sideways fallback.
    """
    import pandas as pd
    import pandas_market_calendars as calendars
    from datetime import timedelta
    current = _time(now)
    schedule = calendars.get_calendar("NYSE").schedule(current.date() - timedelta(days=10), current.date())
    completed = schedule[schedule["market_close"] <= pd.Timestamp(current)]
    expected = completed.index[-1].date()
    cleaned = {}
    source_asof = {}
    alignment = {}
    for symbol in ("^GSPC", "^IXIC", "^VIX"):
        frame = frames[symbol].copy()
        if isinstance(frame.columns, pd.MultiIndex) or frame.index.has_duplicates:
            raise ValueError("MARKET_FRAME_AMBIGUOUS")
        frame = frame.rename(columns={name: str(name).lower() for name in frame.columns}).sort_index()
        if frame.columns.has_duplicates or not {"close", "volume"} <= set(frame.columns):
            raise ValueError("MARKET_COLUMNS_MISSING")
        frame = frame[[day.date() <= expected for day in frame.index]]
        if len(frame) < (200 if symbol == "^GSPC" else 30) or frame.index[-1].date() != expected:
            raise ValueError("MARKET_HISTORY_STALE_OR_SHORT")
        dates = [day.date() for day in frame.index]
        expected_dates = list(calendars.get_calendar("NYSE").schedule(dates[0], expected).index.date)
        if symbol == "^VIX":
            # The provider emits extra holiday records for VIX. The existing
            # regime only consumes its latest close; align to equity sessions,
            # never count those records as sessions or conceal missing sessions.
            if len(dates) != len(set(dates)):
                raise ValueError("MARKET_SESSION_GAP_OR_AMBIGUITY")
            keep = [day in expected_dates for day in dates]
            alignment[symbol] = dict(
                calendar="NYSE", excluded_dates=[str(day) for day in dates if day not in expected_dates],
                original_source_hash=_hash(frame[["close", "volume"]].to_json(date_format="iso")),
            )
            frame = frame[keep]
            dates = [day.date() for day in frame.index]
        if dates != expected_dates:
            raise ValueError("MARKET_SESSION_GAP_OR_AMBIGUITY")
        for value in frame["close"]:
            _num(value, True)
        if symbol == "^GSPC":
            for value in frame["volume"]:
                _num(value, True)
        cleaned[symbol] = frame
        source_asof[symbol] = frame.index[-1].isoformat()
    prefetch = _root_module("_oneil_us_prefetch", "prism-us/cores/data_prefetch.py")
    policy = _root_module("_oneil_regime_policy", "cores/regime_policy.py")
    pulse_module = _root_module("_oneil_market_pulse", "cores/market_pulse.py")
    regime = prefetch._compute_us_regime(cleaned["^GSPC"], cleaned["^IXIC"], cleaned["^VIX"])
    pulse = pulse_module.MarketPulse()
    bars = policy._df_to_bars(cleaned["^GSPC"], "close", "volume", pulse_module.DailyBar)
    states = [pulse.feed(bar) for bar in bars]
    snapshot = dict(observed_at=now, source_asof=source_asof, alignment=alignment,
                    regime=regime["market_regime"], market_pulse=states[-1],
                    distribution_days=int(pulse.distribution_days),
                    pilot_reexposure_active=policy.is_pilot_window(
                        policy._sessions_since_correction_exit(states), flag_on=pilot_flag))
    snapshot["source_ref"] = _hash([snapshot, {key: frame[["close", "volume"]].to_json(date_format="iso")
                                             for key, frame in cleaned.items()}])
    return snapshot


def _market_child(connection):
    import contextlib
    import io
    import logging
    import tempfile
    logging.disable(logging.CRITICAL)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), tempfile.TemporaryDirectory() as cache:
            import yfinance as yf
            yf.set_tz_cache_location(cache)
            frames = {symbol: yf.Ticker(symbol).history(period="2y", interval="1d", auto_adjust=False,
                      repair=False, actions=False, timeout=10, raise_errors=True)
                      for symbol in ("^GSPC", "^IXIC", "^VIX")}
            result = market_snapshot_from_frames(frames, now=datetime.now(timezone.utc).isoformat())
    except Exception:
        result = None
    connection.send(result)
    connection.close()


def fetch_market_snapshot(timeout=40):
    """Bounded public-data computation, without logs/cache/config mutations."""
    if not 0 < timeout <= 60:
        raise ValueError("MARKET_TIMEOUT_INVALID")
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_market_child, args=(send,), daemon=True)
    process.start()
    send.close()
    try:
        return receive.recv() if receive.poll(timeout) else None
    except EOFError:
        return None
    finally:
        receive.close()
        process.join(.1)
        if process.is_alive():
            process.terminate()
            process.join(1)
        if process.is_alive():
            process.kill()
            process.join(1)


class IntradayProvider:
    """Reuse completed-history chunks during one session; refresh current chunk.

    At most 10 symbol/session caches of 12 chunks. No disk state or background loop. Each fetch has
    its own subprocess deadline in the existing collector. Warm-up is explicitly
    slower than a quote; callers must run this outside the protection path.
    """

    def __init__(self, fetcher=None):
        self.fetcher = fetcher
        self.cache = {}

    def __call__(self, symbol, as_of, calendar_name):
        from tools.build_oneil_adaptive_inputs import build_calendar, build_packet, collect_source
        from tools.collect_trend_replay_data import fetch
        calendar = build_calendar(calendar_name, as_of)
        session = (symbol, calendar_name, calendar["sessions"][-1]["trade_date"])
        # Discard older sessions for this symbol, preserving other active symbols.
        for old in list(self.cache):
            if old[:2] == session[:2] and old != session:
                del self.cache[old]
        if session not in self.cache:
            if len(self.cache) >= 10:
                del self.cache[next(iter(self.cache))]
            self.cache[session] = {}
        cache = self.cache[session]
        open_at = _time(calendar["sessions"][-1]["open_at"])

        def cached(request):
            key = _hash(request)
            historical = _time(request["end"]) <= open_at
            if historical and key in cache:
                return deepcopy(cache[key])
            value = (self.fetcher or fetch)(request)
            if historical and value.get("status") == "received" and len(cache) < 12:
                cache[key] = deepcopy(value)
            return value

        source = collect_source(symbol, as_of, calendar_name, fetcher=cached)
        return build_packet(source, kind="LIVE_CAPTURE")["intraday"]

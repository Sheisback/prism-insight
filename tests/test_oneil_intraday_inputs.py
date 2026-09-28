"""Synthetic schedule/price inputs, not strategy performance evidence."""
from copy import deepcopy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from prism_core.oneil_intraday_inputs import build_intraday_inputs
from test_oneil_adaptive_policy import evidence, evaluate


def fixture():
    dates = evidence()["volume"]["expected_prior_trade_dates"] + ["2026-09-25"]
    sessions, bars = [], []
    for day in dates:
        opened = datetime.fromisoformat(day + "T09:30:00").replace(tzinfo=ZoneInfo("America/New_York"))
        sessions.append({"trade_date": day, "open_at": opened.isoformat(),
                         "close_at": (opened + timedelta(minutes=390)).isoformat()})
        for i in range(2):
            bars.append({"provider_timestamp": (opened + timedelta(minutes=i * 5)).isoformat(),
                         "open": 102, "high": 105, "low": 101, "close": 104,
                         "volume": 75 if day == dates[-1] else 50,
                         "dividends": 0, "stock_splits": 0})
        if day != dates[-1]:
            # Final regular bar = completed daily close; rising closes -> above SMA20.
            close = 80 + len(sessions)
            bars.append({"provider_timestamp": (opened + timedelta(minutes=385)).isoformat(),
                         "open": close, "high": close, "low": close, "close": close,
                         "volume": 50, "dividends": 0, "stock_splits": 0})
    return dict(symbol="TEST", bars=bars, calendar={"calendar_ref": "exchange-fixture",
                "sessions": sessions}, as_of="2026-09-25T13:40:00Z",
                retrieved_at="2026-09-25T13:40:30Z", price_basis_ref="unadjusted-v1",
                source_ref="provider-fixture", kind="RECONSTRUCTED_REPLAY")


def test_complete_prefix_direct_policy_and_reconstructed_label():
    result = build_intraday_inputs(**fixture())
    assert result["status"] == "OK"
    assert result["usable_for_prospective"] is False
    assert "quote" not in result
    assert result["volume"]["cumulative_volume"] == "150"
    facts = evidence()
    facts.update({key: result[key] for key in ("bars", "volume", "market_window")})
    assert evaluate(facts)["target_allocation"] == "1.0000"


@pytest.mark.parametrize("field,value", [("volume", -1), ("volume", True), ("volume", "NaN"),
                                        ("close", 0), ("high", 101), ("open", "Infinity")])
def test_invalid_numbers(field, value):
    args = fixture()
    args["bars"][0][field] = value
    assert build_intraday_inputs(**args)["status"] == "INVALID"


@pytest.mark.parametrize("change,reason", [
    ("gap", "REGULAR_PREFIX_GAP"), ("duplicate", "DUPLICATE_REGULAR_BAR"),
    ("split", "CORPORATE_ACTION_BASIS_UNVERIFIED"),
    ("dividend", "CORPORATE_ACTION_BASIS_UNVERIFIED"),
    ("unknown", "CORPORATE_ACTION_EVIDENCE_MISSING"),
    ("zero", "MISSING_VOLUME_DENOMINATOR")])
def test_missing_and_ambiguous(change, reason):
    args = fixture()
    if change == "gap":
        args["bars"].pop(0)
    elif change == "duplicate":
        args["bars"].append(deepcopy(args["bars"][0]))
    elif change in ("split", "dividend", "unknown"):
        args["bars"][0]["stock_splits" if change != "dividend" else "dividends"] = (
            None if change == "unknown" else 1)
    else:
        for row in args["bars"][:-2]:
            row["volume"] = 0
    assert build_intraday_inputs(**args)["reason_codes"] == [reason]


def test_ignored_forming_prepost_and_out_of_order():
    args = fixture()
    original = build_intraday_inputs(**args)
    extra = deepcopy(args["bars"][-1])
    for timestamp in ("2026-09-25T13:40:00Z", "2026-09-25T13:25:00Z"):
        args["bars"].append({**extra, "provider_timestamp": timestamp, "volume": "NaN"})
    args["bars"].reverse()
    assert build_intraday_inputs(**args)["input_hash"] == original["input_hash"]


@pytest.mark.parametrize("change", ["basis", "calendar_ref", "unordered", "duplicate_day",
                                    "naive", "offset", "partial", "before_retrieval"])
def test_clock_basis_calendar(change):
    args = fixture()
    if change == "basis":
        args["price_basis_ref"] = ""
    elif change == "calendar_ref":
        args["calendar"]["calendar_ref"] = ""
    elif change == "unordered":
        args["calendar"]["sessions"].reverse()
    elif change == "duplicate_day":
        args["calendar"]["sessions"][0] = args["calendar"]["sessions"][1]
    elif change == "naive":
        args["as_of"] = "2026-09-25T13:40:00"
    elif change == "offset":
        args["calendar"]["sessions"][0]["open_at"] = "2026-08-27T09:30:00-05:00"
    elif change == "partial":
        args["as_of"] = "2026-09-25T13:39:00Z"
    else:
        args["retrieved_at"] = "2026-09-25T13:39:00Z"
    assert build_intraday_inputs(**args)["status"] != "OK"


@pytest.mark.parametrize("start,retrieved,ok", [(None, "13:40:30", False),
    ("13:39:59", "13:40:30", False), ("13:40:00", "13:42:01", False),
    ("13:40:31", "13:40:30", False), ("13:40:00", "13:40:30", True)])
def test_live_clock(start, retrieved, ok):
    args = fixture()
    args.update(kind="LIVE_CAPTURE", retrieved_at=f"2026-09-25T{retrieved}Z",
                retrieval_started_at=None if start is None else f"2026-09-25T{start}Z")
    result = build_intraday_inputs(**args)
    assert (result["status"] == "OK") is ok
    assert result["usable_for_prospective"] is ok


def test_early_close_and_equal_elapsed_guard():
    args = fixture()
    args["calendar"]["sessions"][0]["close_at"] = "2026-08-27T13:00:00-04:00"
    assert build_intraday_inputs(**args)["status"] == "OK"
    args.update(as_of="2026-09-25T17:05:00Z", retrieved_at="2026-09-25T17:05:30Z")
    assert build_intraday_inputs(**args)["reason_codes"] == ["PRIOR_SESSION_TOO_SHORT"]


def test_dst_aware_calendar_and_holiday_gap():
    args = fixture()
    # Calendar explicitly omits Labor Day; no weekday-based synthetic replacement.
    out = build_intraday_inputs(**args)
    assert "2026-09-07" not in out["volume"]["expected_prior_trade_dates"]
    # Move the first session across DST while preserving its local opening time.
    session = args["calendar"]["sessions"][0]
    session.update(trade_date="2026-03-06", open_at="2026-03-06T09:30:00-05:00",
                   close_at="2026-03-06T16:00:00-05:00")
    for index in range(2):
        args["bars"][index]["provider_timestamp"] = f"2026-03-06T09:{30 + index * 5}:00-05:00"
    assert build_intraday_inputs(**args)["status"] == "OK"


def test_current_zero_volume_is_observed_not_missing():
    args = fixture()
    for bar in args["bars"][-2:]:
        bar["volume"] = 0
    result = build_intraday_inputs(**args)
    assert result["status"] == "OK"
    assert result["volume"]["cumulative_volume"] == "0"


def test_market_close_retrospective_and_one_bar_prefix():
    args = fixture()
    for session in args["calendar"]["sessions"]:
        opened = datetime.fromisoformat(session["open_at"])
        session["close_at"] = (opened + timedelta(minutes=10)).isoformat()
    assert build_intraday_inputs(**args)["status"] == "OK"
    args["as_of"] = "2026-09-25T13:35:00Z"
    assert len(build_intraday_inputs(**args)["bars"]) == 1


def test_nonprefix_volume_cannot_leak_into_matched_comparison():
    args = fixture()
    extra = {**args["bars"][0], "provider_timestamp": "2026-08-27T09:40:00-04:00",
             "volume": 99999999}
    args["bars"].append(extra)
    out = build_intraday_inputs(**args)
    assert out["volume"]["samples"][0]["cumulative_volume"] == "100"
    # Even outside the prefix, a known action makes common basis unverified.
    extra["stock_splits"] = 2
    assert build_intraday_inputs(**args)["status"] == "MISSING"

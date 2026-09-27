from copy import deepcopy
from datetime import timedelta
import json

import pytest

from test_oneil_intraday_inputs import fixture
from tools.build_oneil_adaptive_inputs import (
    build_calendar,
    build_packet,
    collect_source,
    digest,
)
from tools.collect_trend_replay_data import stamp


def source():
    f = fixture()
    calendar = dict(f["calendar"], name="NYSE")
    response = dict(
        status="received",
        exchange="NYQ",
        raw_rows=f["bars"],
        retrieval_started_at="2026-09-25T13:40:00Z",
        retrieved_at=f["retrieved_at"],
    )
    request = dict(
        market="US",
        ticker="TEST",
        interval="5m",
        start="2026-08-27T00:00:00Z",
        end=f["as_of"],
    )
    return dict(
        symbol="TEST",
        as_of=f["as_of"],
        calendar=calendar,
        datasets=[
            dict(request=request, response=response, response_sha256=digest(response))
        ],
    )


def test_real_calendar_excludes_holiday_and_handles_dst():
    cal = build_calendar("NYSE", "2026-09-25T14:00:00Z")
    assert len(cal["sessions"]) == 21
    dates = [r["trade_date"] for r in cal["sessions"]]
    assert "2026-09-07" not in dates and dates[0] == "2026-08-27"
    dst = build_calendar("NYSE", "2026-11-13T15:00:00Z")
    assert {stamp(r["open_at"]).hour for r in dst["sessions"]} == {13, 14}
    with pytest.raises(ValueError):
        build_calendar("NYSE", "2026-09-27T14:00:00Z")


def test_collect_uses_bounded_requests_and_no_trading_calls():
    requests = []

    def fetcher(request):
        requests.append(request)
        return dict(status="empty", raw_rows=[])

    result = collect_source("TEST", "2026-09-25T14:00:00Z", "NYSE", fetcher)
    assert 1 <= len(requests) <= 12
    assert all(
        stamp(r["end"]) - stamp(r["start"]) <= timedelta(days=5) for r in requests
    )
    assert all(r["interval"] == "5m" and r["ticker"] == "TEST" for r in requests)
    assert len(result["datasets"]) == len(requests)


def test_fixed_packet_reproducibility_and_no_fake_setup_or_quote():
    s = source()
    before = deepcopy(s)
    out = build_packet(s)
    assert s == before and out == build_packet(s)
    assert out["intraday"]["status"] == "OK" and out["matched_rvol"] == "1.5"
    assert out["setup"]["status"] == "MISSING"
    assert not out["evaluation_ready"] and not out["intraday"]["usable_for_prospective"]
    assert "quote" not in out["intraday"]
    assert "FRESH_QUOTE_NOT_SUPPLIED" in out["missing"]
    assert "CURRENT_ADMISSION_GATES_NOT_SUPPLIED" in out["missing"]
    assert not out["policy_executed"] and not out["broker_execution"]


def test_provider_identity_and_hash_are_not_silently_repaired():
    s = source()
    s["datasets"][0]["response"]["exchange"] = "NMS"
    with pytest.raises(ValueError, match="HASH"):
        build_packet(s)
    s["datasets"][0]["response_sha256"] = digest(s["datasets"][0]["response"])
    out = build_packet(s)
    assert out["intraday"]["status"] == "MISSING"
    assert out["matched_rvol"] is None
    s = source()
    s["datasets"][0]["request"]["ticker"] = "OTHER"
    with pytest.raises(ValueError, match="IDENTITY"):
        build_packet(s)


def test_live_collection_does_not_supply_missing_admission():
    out = build_packet(source(), kind="LIVE_CAPTURE")
    assert out["intraday"]["usable_for_prospective"]
    assert not out["evaluation_ready"]
    assert out["setup"]["status"] == "MISSING"
    assert json.loads(json.dumps(out)) == out


def test_live_current_fetch_may_reuse_earlier_history_without_backdating():
    s = source()
    old = deepcopy(s["datasets"][0])
    old["response"]["raw_rows"] = old["response"]["raw_rows"][:-2]
    old["request"]["end"] = "2026-09-24T20:00:00Z"
    old["response"]["retrieval_started_at"] = "2026-09-25T13:20:00Z"
    old["response"]["retrieved_at"] = "2026-09-25T13:20:30Z"
    old["response_sha256"] = digest(old["response"])
    current = deepcopy(s["datasets"][0])
    current["response"]["raw_rows"] = current["response"]["raw_rows"][-2:]
    current["request"]["start"] = "2026-09-25T13:30:00Z"
    current["response_sha256"] = digest(current["response"])
    s["datasets"] = [old, current]
    assert build_packet(s, kind="LIVE_CAPTURE")["intraday"]["usable_for_prospective"]
    current["response"]["retrieval_started_at"] = "2026-09-25T13:39:59Z"
    current["response_sha256"] = digest(current["response"])
    assert not build_packet(s, kind="LIVE_CAPTURE")["intraday"][
        "usable_for_prospective"
    ]

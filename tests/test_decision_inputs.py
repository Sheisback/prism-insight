from datetime import date, datetime, timedelta, timezone

import pandas as pd

from observability import decision_inputs as D
from prism_core import decision_input_features as F


def _frame(n=40, today=None, forming_volume=None, columns=("Open", "High", "Low", "Close", "Volume")):
    days, day = [], date(2026, 7, 1)
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    if today:
        days = days[:-1] + [today]
    rows = []
    for i, d in enumerate(days):
        close = 100 + i
        rows.append({columns[0]: close - 0.5, columns[1]: close + 1, columns[2]: close - 1, columns[3]: close,
                     columns[4]: 1000 + (i % 2) * 500})
    if forming_volume is not None:
        rows[-1][columns[4]] = forming_volume
    return pd.DataFrame(rows, index=pd.to_datetime(days))


def test_completed_only_features_and_missing_forming_bar():
    now = datetime(2026, 9, 28, 1, 0, tzinfo=timezone.utc)  # 10:00 KST, session open
    bars = F.bars_from_frame(_frame(40))
    out = F.compute(bars, market="KR", observed_at=now, current_price=140.0)
    f = out["features"]
    assert out["status"] == "PARTIAL" and "rvol_time_scaled_linear" in out["missing"]
    assert f["forming_bar_present"] is False and f["atr20_pct"] > 0
    assert f["accumulation_days_25"] + f["distribution_days_25"] <= 25
    assert f["up_down_volume_ratio_20"] is None  # no down closes


def test_forming_bar_time_scaled_rvol_and_gap():
    now = datetime(2026, 9, 28, 1, 0, tzinfo=timezone.utc)  # 10:00 KST = 2/13 of session
    frame = _frame(40, today=date(2026, 9, 28), forming_volume=1000)
    out = F.compute(F.bars_from_frame(frame), market="KR", observed_at=now, current_price=140.0,
                    scenario={"buy_score": 6, "momentum_signal_count": 1, "decision": "x"})
    f = out["features"]
    assert f["forming_bar_present"] is True
    frac = F.elapsed_fraction("KR", now)
    assert abs(frac - 60 / 390) < 1e-6
    assert f["rvol_time_scaled_linear"] > 2 and f["rubric_probe"]["volume_item_time_scaled"] is True
    assert f["scenario_buy_score"] == 6 and f["scenario_momentum_signal_count"] == 1
    assert "gap_open_pct" in f


def test_us_lowercase_columns_and_after_close_counts_today_completed():
    now = datetime(2026, 9, 28, 21, 0, tzinfo=timezone.utc)  # 17:00 ET
    frame = _frame(40, today=date(2026, 9, 28), columns=("open", "high", "low", "close", "volume"))
    out = F.compute(F.bars_from_frame(frame), market="US", observed_at=now, current_price=None)
    assert out["features"]["forming_bar_present"] is False
    assert out["features"]["price_basis"] == 139.0


def test_too_few_bars_is_missing():
    out = F.compute(F.bars_from_frame(_frame(10)), market="KR", observed_at=datetime.now(timezone.utc))
    assert out["status"] == "MISSING"


def test_peer_summary():
    packet = {"ready": True, "period": "2025/12", "price_basis": "전일종가",
              "peers": [{"per": 10, "pbr": 1.0}, {"per": 20, "pbr": 2.0}, {"per": 30, "pbr": None}, {"per": -5, "pbr": 3.0}]}
    out = D.peer_valuation_summary(packet)
    assert out["peer_median_per"] == 25 and out["peer_median_pbr"] == 2.5
    assert out["per_discount_vs_median_pct"] == 60.0
    assert D.peer_valuation_summary({"ready": False, "skip_reason": "x"})["status"] == "MISSING"


class _Agent:
    pass


def test_emit_is_fail_open_idempotent_and_skips_isolated(monkeypatch):
    sent = []
    monkeypatch.setattr(D, "emit_event", lambda event, **kw: sent.append((event, kw)) or {"ok": 1})
    agent = _Agent()
    D.capture_frame(agent, "AAPL", _frame(40), market="US")
    agent._report_meta = {}
    now = datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)
    payload = D.emit_decision_inputs(agent, market="US", ticker="AAPL", decision_id="d-1", scenario={},
                                     current_price=140.0, decision="no_entry", source="t", now=now,
                                     earnings_lookup=lambda t, d: {"status": "OK", "next_earnings_date": "2026-10-30"})
    assert payload["trading_impact"] == "none" and payload["earnings"]["status"] == "OK"
    assert payload["peer_valuation"]["status"] == "MISSING"
    D.emit_decision_inputs(agent, market="US", ticker="AAPL", decision_id="d-1", scenario={}, current_price=140.0,
                           decision="no_entry", source="t", now=now, earnings_lookup=lambda t, d: {})
    assert sent[0][1]["event_id"] == sent[1][1]["event_id"] and sent[0][1]["decision_id"] == "d-1"
    agent._no_order_effects = object()
    assert D.emit_decision_inputs(agent, market="US", ticker="AAPL", decision_id="d-2", scenario={},
                                  current_price=1, decision="x", source="t") is None
    broken = _Agent()
    broken._decision_input_bars = {"AAPL": {"market": "US", "bars": "garbage"}}
    assert D.emit_decision_inputs(broken, market="US", ticker="AAPL", decision_id="d-3", scenario={},
                                  current_price=1, decision="x", source="t", earnings_lookup=lambda t, d: {}) is None


def test_disabled_flag(monkeypatch):
    monkeypatch.setenv("DECISION_INPUT_SHADOW_ENABLED", "false")
    agent = _Agent()
    D.capture_frame(agent, "X", _frame(40), market="KR")
    assert not hasattr(agent, "_decision_input_bars")


def test_payload_survives_event_sanitizer(monkeypatch):
    import json

    from observability.events import build_event
    captured = {}
    monkeypatch.setattr(D, "emit_event", lambda event, **kw: captured.update(kw) or {"ok": 1})
    agent = _Agent()
    D.capture_frame(agent, "AAPL", _frame(40, today=date(2026, 9, 28), forming_volume=900), market="US")
    D.emit_decision_inputs(agent, market="US", ticker="AAPL", decision_id="d", scenario={}, current_price=140.0,
                           decision="x", source="t", now=datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc),
                           earnings_lookup=lambda t, d: {"status": "OK"})
    event = build_event("decision_inputs.shadow_captured", service="s", attributes=captured["attributes"])
    assert "[REDACTED]" not in json.dumps(event["attributes"])
    assert event["attributes"]["features"]["market_elapsed_fraction"] > 0

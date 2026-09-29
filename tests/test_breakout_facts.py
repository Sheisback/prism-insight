"""52-week-high facts for rule 2a, and the 'today's high is not resistance' prompt rule.

2026-09-29 KR afternoon: the report called HPSP's still-open intraday high (61,500)
"단기 저항선", BUY took it as the nearest major resistance and priced R/R at 0.11.
The confirmed-resistance replay also found 2a-eligible names (DELL, CRWD) whose
target skipped 2a. These tests hold the facts and the prompt lines in place.
"""
import importlib.util
from datetime import date, datetime, time, timedelta
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from prism_core.breakout_facts import compute_high52_facts, render_high52_facts  # noqa: E402

CLOSE = time(15, 30)


def _days(n, end=date(2026, 9, 29)):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def test_todays_open_bar_is_excluded_from_the_52_week_high():
    days = _days(21)
    highs = [100] * 20 + [130]  # today spikes above everything
    facts = compute_high52_facts(days, highs, price=128, now_local=datetime(2026, 9, 29, 14, 50), session_close=CLOSE)
    assert facts["high"] == 100 and facts["sessions"] == 20
    assert facts["near_high"] is True and facts["within_chase"] is False  # 28% above: chase limit fails


def test_after_close_today_counts_as_a_completed_session():
    days = _days(21)
    facts = compute_high52_facts(days, [100] * 20 + [130], price=128,
                                 now_local=datetime(2026, 9, 29, 16, 0), session_close=CLOSE)
    assert facts["high"] == 130 and facts["sessions"] == 21


def test_hpsp_shape_is_far_below_its_52_week_high():
    days = _days(60)
    highs = [92000] + [60000] * 58 + [61500]
    facts = compute_high52_facts(days, highs, price=60900, now_local=datetime(2026, 9, 29, 15, 18), session_close=CLOSE)
    assert facts["high"] == 92000 and round(facts["ratio"] * 100, 1) == 66.2
    line = render_high52_facts(facts)
    assert "92,000" in line and "(a) 95% 이상: 아니오" in line and "(c) 최고가 대비 +5% 이하: 예" in line


def test_breakout_candidate_meets_a_and_c():
    days = _days(30)
    facts = compute_high52_facts(days, [50] * 10 + [70] + [60] * 18 + [71], price=69.5,
                                 now_local=datetime(2026, 9, 29, 10, 0), session_close=CLOSE)
    assert facts["near_high"] is True and facts["within_chase"] is True
    assert "(a) 95% 이상: 예" in render_high52_facts(facts, decimals=2)


@pytest.mark.parametrize("bad", [None, float("nan"), 0])
def test_missing_high_inside_window_is_unknown(bad):
    days = _days(21)
    facts = compute_high52_facts(days, [100] * 10 + [bad] + [100] * 10, price=99,
                                 now_local=datetime(2026, 9, 29, 20, 0), session_close=CLOSE)
    assert facts["high"] is None and "미확정" in render_high52_facts(facts)


def _load(market, name):
    path = ROOT / ("prism-us" if market == "US" else "") / "cores/agents" / name
    spec = importlib.util.spec_from_file_location(f"bf_{market}_{name[:-3]}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("market", ["KR", "US"])
@pytest.mark.parametrize("language", ["ko", "en"])
def test_buy_prompt_rejects_todays_high_as_resistance_and_sell_is_unchanged(market, language):
    module = _load(market, "trading_agents.py")
    prefix = "create_us_" if market == "US" else "create_"
    buy = getattr(module, prefix + "trading_scenario_agent")(language=language).instruction
    sell = getattr(module, prefix + "sell_decision_agent")(language=language).instruction
    marker = "진행 중인 당일 봉의 장중 고가는 주요 저항이 아닙니다" if language == "ko" \
        else "today's still-open session is not a major resistance"
    assert buy.count(marker) == 1 and marker not in sell
    assert "52주 확정 최고가" in buy


@pytest.mark.parametrize("market", ["KR", "US"])
def test_report_prompt_rejects_todays_high_as_support_or_resistance(market):
    source = (ROOT / ("prism-us" if market == "US" else "") / "cores/agents/stock_price_agents.py").read_text()
    assert source.count("진행 중인 당일 봉의 장중 고가·저가는 아직 검증되지 않은 가격이므로") == 1
    assert source.count("today's still-open session as a major resistance or support level") == 1

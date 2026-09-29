"""Deterministic 52-week-high facts for the BUY prompt's O'Neil 2a target rule.

Rule 2a (entry x 1.20) needs (a) price >= 95% of the 52-week high and (c) price
no more than 5% above it. The agent had to dig both numbers out of report
prose, and missed 2a on eligible names (DELL 2026-09-03, CRWD 2026-09-14 in the
confirmed-resistance replay). Only (a) and (c) are computed here; (b) overhead
resistance and (d) the trend gate stay with the report and the trend facts.

The high uses completed sessions only: today's still-open bar is not a tested
level. No I/O, no score, no gate.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time
from typing import Any, Sequence

BREAKOUT_FACTS_VERSION = "breakout-facts-v1"
LOOKBACK_SESSIONS = 250
NEAR_HIGH = 0.95
MAX_CHASE = 1.05


def _valid(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def compute_high52_facts(
    dates: Sequence[date],
    highs: Sequence[Any],
    *,
    price: Any,
    now_local: datetime,
    session_close: time,
) -> dict:
    days = list(dates)
    partial = bool(days) and days[-1] == now_local.date() and now_local.time() < session_close
    confirmed = [_valid(h) for h in (list(highs)[:-1] if partial else list(highs))][-LOOKBACK_SESSIONS:]
    current = _valid(price)
    result = {"version": BREAKOUT_FACTS_VERSION, "sessions": len(confirmed),
              "high": None, "ratio": None, "near_high": None, "within_chase": None}
    values = [h for h in confirmed if h is not None]
    if not values or current is None or len(values) < len(confirmed):
        return result  # a missing high inside the window makes the 52-week high unknown
    high = max(values)
    result.update(high=high, ratio=current / high,
                  near_high=current >= high * NEAR_HIGH, within_chase=current <= high * MAX_CHASE)
    return result


def render_high52_facts(facts: dict, *, decimals: int = 0) -> str:
    if facts["high"] is None:
        return (f"- 52주 확정 최고가: 계산 불가(확정 {facts['sessions']}거래일 중 결측 또는 가격 없음) "
                "→ 2a (a)·(c) 미확정")
    yes = lambda flag: "예" if flag else "아니오"  # noqa: E731
    return (
        f"- 52주 확정 최고가(당일 봉 제외, 확정 {facts['sessions']}거래일): {facts['high']:,.{decimals}f} "
        f"| 현재가/최고가 {facts['ratio'] * 100:.1f}% → 2a (a) 95% 이상: {yes(facts['near_high'])}, "
        f"(c) 최고가 대비 +5% 이하: {yes(facts['within_chase'])} "
        "((b) 상단 저항·(d) 추세 게이트는 보고서·추세 팩트로 판정)"
    )

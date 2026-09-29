"""Deterministic 20-session volume facts for BUY momentum signal 1.

Signal 1 is "volume >= 200% of the 20-day average, today or within the last
three sessions". Every KR/US batch runs while today's bar is still open, and
the BUY agent was told never to compare an unfinished bar with full sessions.
It therefore dropped today on every run: on 2026-09-28 MDB traded 7.4x its
20-day average by 14:30 ET and the message said "at most 0.67x".

Volume is cumulative within a session, so a partial bar is a lower bound of
the final bar. A partial bar that already reaches the threshold proves the
condition; one that does not is undetermined, never "weak". No I/O, no score.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time
from typing import Any, Sequence

VOLUME_SURGE_FACTS_VERSION = "volume-surge-facts-v1"
SURGE_THRESHOLD = 2.0
AVERAGE_WINDOW = 20
RECENT_CONFIRMED_SESSIONS = 3


def _valid(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _ratio(volumes: list[float | None], index: int, window: int) -> tuple[float | None, float | None]:
    """Ratio of volumes[index] to the mean of the `window` rows before it."""
    if index < window:
        return None, None
    prior = volumes[index - window:index]
    if any(v is None for v in prior):
        return None, None
    average = sum(prior) / window
    current = volumes[index]
    if current is None or average <= 0:
        return None, average
    return current / average, average


def compute_volume_surge_facts(
    dates: Sequence[date],
    volumes: Sequence[Any],
    *,
    now_local: datetime,
    session_close: time,
    window: int = AVERAGE_WINDOW,
    recent: int = RECENT_CONFIRMED_SESSIONS,
    threshold: float = SURGE_THRESHOLD,
) -> dict:
    """Classify today's bar and the last `recent` completed sessions.

    `dates` are exchange-local session dates in ascending order and
    `now_local` is the exchange-local clock. The last bar is partial when it
    is dated today and the regular session has not closed yet.
    """
    vols = [_valid(v) for v in volumes]
    days = list(dates)
    partial = bool(days) and days[-1] == now_local.date() and now_local.time() < session_close
    confirmed_count = len(days) - 1 if partial else len(days)
    result: dict[str, Any] = {
        "version": VOLUME_SURGE_FACTS_VERSION,
        "threshold": threshold,
        "window": window,
        "captured_at": now_local.strftime("%Y-%m-%d %H:%M"),
        "partial_session": None,
        "confirmed_sessions": [],
    }

    if partial:
        index = len(days) - 1
        ratio, average = _ratio(vols, index, window)
        if ratio is None:
            status = "missing"
        elif ratio >= threshold:
            status = "met_lower_bound"
        else:
            status = "pending"
        result["partial_session"] = {
            "date": days[index].isoformat(), "volume": vols[index],
            "average": average, "ratio": ratio, "status": status,
        }

    for index in range(max(0, confirmed_count - recent), confirmed_count):
        ratio, average = _ratio(vols, index, window)
        status = "missing" if ratio is None else ("met" if ratio >= threshold else "not_met")
        result["confirmed_sessions"].append({
            "date": days[index].isoformat(), "volume": vols[index],
            "average": average, "ratio": ratio, "status": status,
        })
    result["confirmed_sessions"].reverse()  # newest first

    statuses = [s["status"] for s in result["confirmed_sessions"]]
    if result["partial_session"]:
        statuses.append(result["partial_session"]["status"])
    if any(s in ("met", "met_lower_bound") for s in statuses):
        result["signal1"] = "met"
    elif len(result["confirmed_sessions"]) < recent or any(s in ("missing", "pending") for s in statuses):
        result["signal1"] = "undetermined"
    else:
        result["signal1"] = "not_met"
    return result


def render_volume_surge_facts(facts: dict, *, unit: str = "주") -> str:
    """Korean facts lines for the BUY prompt's trend-facts block."""
    pct = f"{facts['threshold'] * 100:.0f}%"
    lines = [f"- 거래량(모멘텀 신호 1 판정용 · 기준=해당 봉 이전 확정 {facts['window']}거래일 평균 · {facts['version']}):"]
    part = facts.get("partial_session")
    if part:
        head = f"  · 당일 {part['date']} 장중 누적(미완성봉, {facts['captured_at']} 조회)"
        if part["status"] == "missing":
            lines.append(f"{head}: 계산 불가(거래량 또는 직전 {facts['window']}거래일 이력 부족) → 미확정")
        elif part["status"] == "met_lower_bound":
            lines.append(
                f"{head}: {part['volume']:,.0f}{unit} = 평균 {part['average']:,.0f}{unit}의 {part['ratio']:.2f}배 "
                f"→ 장중 값만으로 이미 {pct} 이상이므로 충족 확정(거래량은 마감까지 줄지 않습니다)"
            )
        else:
            lines.append(
                f"{head}: {part['volume']:,.0f}{unit} = 평균 {part['average']:,.0f}{unit}의 {part['ratio']:.2f}배(하한값) "
                f"→ 아직 {pct} 미만: 충족으로 세지 않되, 마감 전 값이므로 거래량 부진 근거로도 쓰지 마십시오"
            )
    confirmed = []
    for s in facts["confirmed_sessions"]:
        confirmed.append(f"{s['date']} 계산 불가" if s["ratio"] is None else f"{s['date']} {s['ratio']:.2f}배")
    lines.append("  · 확정 세션(최근순): " + (" / ".join(confirmed) if confirmed else "없음"))
    verdict = {"met": "충족", "not_met": "미충족", "undetermined": "미확정(충족으로 세지 않음)"}[facts["signal1"]]
    lines.append(f"  · 신호 1 판정: {verdict} — 거래량 수치는 이 줄을 인용하고, 당일 대량 거래를 생략하지 마십시오")
    return "\n".join(lines)

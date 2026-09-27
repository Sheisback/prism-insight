"""Order-free replay of the re-entry v2 LLM recheck on past pivot triggers.

    python tools/replay_reentry_llm_recheck.py --market KR --triggers pivotp_KR.jsonl \
        --bars bars_KR.json --bench bench_KR.json --bench-code 1001 \
        --reports-root /root/prism-insight --out recheck_KR.jsonl

For every closed pivot trigger from tools/replay_pivot_reentry.py, the BUY agent (current
instruction plus a re-entry recheck section) sees: the latest report written on or before
the trigger day, deterministic technical and market facts as of the trigger, the pivot
breakout and the original skip/stop reason. Codex runs WITHOUT MCP tools, so it cannot
read post-trigger prices. The question is whether its approvals separate winning from
losing breakouts; outcomes come from the replay's production-exit simulation.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from prism_core.decision_input_features import compute, render_facts_block  # noqa: E402
from tools.replay_pivot_reentry import load_bars  # noqa: E402

REPORT_DIRS = {"KR": "reports", "US": "prism-us/reports"}
TRANSLATED = re.compile(r"_(en|ja|zh|es)\.md$")
RECHECK_KO = """

## 재진입 재점검 모드 (이번 요청에만 적용)

이 종목은 과거에 분석되어 보류·차단되었거나 손절된 종목입니다. 오늘 장중에 베이스의 피벗(저항선)을
거래량을 동반해 돌파했습니다. 입력의 보고서는 트리거 이전에 작성된 가장 최근 보고서이며 작성일을 확인하십시오.
- 원래 보류·차단·손절 사유가 현재 기술적 사실(추세, 위치, 거래량)과 시장 상태로 해소됐는지 재점검하십시오.
- 재무 F1~F4는 보고서 기준 판단을 유지합니다. 보고서 이후의 새 정보는 없으므로 추정하지 마십시오.
- 진입 가격은 입력의 돌파 가격 기준입니다. 추격(피벗 +5% 초과)은 이미 배제됐습니다.
- 돌파 직후 되돌림을 기다려야 한다고 판단하면 미진입으로 하고 rejection_reason에 구체적으로 적으십시오.
- 출력 JSON 형식과 채점 규칙은 기존과 동일합니다.
"""


def latest_report(root, market, ticker, day):
    folder = Path(root) / REPORT_DIRS[market]
    stamp = day.replace("-", "")
    best = None
    for path in folder.glob(f"{ticker}_*.md"):
        if TRANSLATED.search(path.name):
            continue
        match = re.search(r"_(\d{8})_(morning|afternoon)", path.name)
        if not match or match.group(1) > stamp:
            continue
        key = (match.group(1), match.group(2) == "afternoon")
        if best is None or key > best[0]:
            best = (key, path)
    return best[1] if best else None


def market_facts(bench_bars, day):
    from cores.market_pulse import DailyBar, MarketPulse
    pulse, state = MarketPulse(), None
    for bar in bench_bars:
        if bar["date"] >= day:
            break
        state = pulse.feed(DailyBar(date=bar["date"], close=bar["close"],
                                    volume=bar["volume"] if bar["volume"] > 1 else None))
    return {"state": state, "distribution_days": int(getattr(pulse, "distribution_days", 0) or 0)}


def technical_block(bars, i, entry, pivot, market, bench_bars):
    """Facts as of the trigger: completed bars before day i plus the breakout price."""
    closes = [b["close"] for b in bars[:i]]

    def ma(n):
        return sum(closes[-n:]) / n if len(closes) >= n else None

    def pct(a, b):
        return None if a is None or not b else (a / b - 1) * 100

    ma20, ma50, ma200 = ma(20), ma(50), ma(200)
    ma20_prev = sum(closes[-25:-5]) / 20 if len(closes) >= 25 else None
    bench = [b for b in bench_bars if b["date"] < bars[i]["date"]]
    rs = None
    if len(closes) > 61 and len(bench) > 61:
        rs = (closes[-1] / closes[-61] - 1 - (bench[-1]["close"] / bench[-61]["close"] - 1)) * 100
    mkt = market_facts(bench_bars, bars[i]["date"])
    t1 = ma50 is not None and closes[-1] < ma50
    t2 = ma20 is not None and ma20_prev is not None and ma20 < ma20_prev and closes[-1] <= ma20 * 0.95

    def fmt(v, suffix="%"):
        return "결측" if v is None else f"{v:+.2f}{suffix}"

    lines = [
        f"### 📉 개별 추세 팩트 (재진입 트리거일 {bars[i]['date']} 기준, 직전 확정일 {bars[i - 1]['date']})",
        f"- 직전 종가 {closes[-1]:,.2f}: MA20 대비 {fmt(pct(closes[-1], ma20))}, MA50 대비 {fmt(pct(closes[-1], ma50))}, "
        f"MA200 대비 {fmt(pct(closes[-1], ma200))}",
        f"- MA20 기울기: {'상승' if ma20 and ma20_prev and ma20 > ma20_prev else '하락'} / T1_hit: {t1} / T2_hit: {t2}",
        f"- RS(60일, 종목-지수): {fmt(rs, '%p')}",
        f"- Market Pulse(지수 재생): {mkt['state']} | 분산일 {mkt['distribution_days']}",
        "",
        "### 🚀 재진입 트리거",
        f"- 피벗(베이스 저항선) {pivot:,.2f} 돌파, 진입 기준가 {entry:,.2f} (피벗 대비 {fmt(pct(entry, pivot))})",
        f"- 돌파일 거래량: 20일 평균의 {bars[i]['volume'] / (sum(b['volume'] for b in bars[i - 20:i]) / 20):.2f}배",
    ]
    result = compute(bars[:i], market=market, observed_at=_as_dt(bars[i - 1]["date"]), current_price=entry)
    facts, _ = render_facts_block(result, None, None, market=market, language="ko")
    return "\n".join(lines) + "\n\n" + facts


def _as_dt(day):
    from datetime import datetime, timezone
    return datetime.fromisoformat(day + "T23:00:00").replace(tzinfo=timezone.utc)


def instruction(market):
    os.environ["PRISM_BUY_DECISION_FACTS"] = "true"
    if market == "KR":
        from cores.agents.trading_agents import create_trading_scenario_agent
        return create_trading_scenario_agent(language="ko").instruction + RECHECK_KO
    import importlib.util
    spec = importlib.util.spec_from_file_location("us_agents_recheck", ROOT / "prism-us/cores/agents/trading_agents.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.create_us_trading_scenario_agent(language="ko").instruction + RECHECK_KO


async def run(records, market, bars_by_ticker, bench, reports_root, out, concurrency):
    from cores.llm.codex_oauth_fast_backend import generate_codex_fast_async
    from cores.utils import parse_llm_json
    from prism_core.codex_config import resolve_buy_codex_settings
    settings = resolve_buy_codex_settings()
    system = instruction(market)
    semaphore = asyncio.Semaphore(concurrency)

    async def one(rec):
        async with semaphore:
            row = {k: rec.get(k) for k in ("source", "ticker", "date", "trigger_date", "entry", "pivot", "reason",
                                           "score", "min_score", "excess")}
            row["ret"] = rec["trade"]["ret"]
            try:
                bars = bars_by_ticker[rec["ticker"]]
                i = rec["trigger_index"]
                report = latest_report(reports_root, market, rec["ticker"], rec["trigger_date"])
                if report is None:
                    row["status"] = "NO_REPORT"
                    return row
                row["report"] = report.name
                stamp = re.search(r"_(\d{8})_(morning|afternoon)", report.name).group(1)
                row["report_age_days"] = (_as_dt(rec["trigger_date"]) - _as_dt(
                    f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}")).days
                facts = technical_block(bars, i, rec["entry"], rec["pivot"], market, bench)
                original = (f"### 원래 판단\n- 출처: {rec['source']} / 원래 판단일 {rec['date']}\n"
                            f"- 원래 점수/최소점수: {rec.get('score')}/{rec.get('min_score')}\n"
                            f"- 원래 사유: {str(rec.get('reason') or '')[:500]}\n")
                user = (f"재진입 재점검 요청입니다.\n\n{original}\n{facts}\n### Report Content:\n"
                        f"{report.read_text(encoding='utf-8')}\n")
                result = await generate_codex_fast_async(system_prompt=system, user_prompt=user, model=settings.model,
                                                         reasoning_effort=settings.reasoning_effort,
                                                         timeout=settings.timeout, mcp_profile=None,
                                                         require_mcp_calls=False)
                scenario = parse_llm_json(result.text, context="reentry recheck replay") or {}
                decision = str(scenario.get("decision", "")).strip().lower()
                row.update(status="OK", decision=scenario.get("decision"), approved=decision in {"진입", "enter", "entry"},
                           buy_score=scenario.get("buy_score"), rejection_reason=scenario.get("rejection_reason"))
            except Exception as error:  # noqa: BLE001 - one failure never stops the batch
                row.update(status="ERROR", error=f"{type(error).__name__}: {str(error)[:200]}")
            with open(out, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            return row

    return await asyncio.gather(*(one(r) for r in records))


def _stats(values):
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0}
    return {"n": len(values), "mean": round(statistics.mean(values), 4), "median": round(statistics.median(values), 4),
            "win": round(sum(v > 0 for v in values) / len(values), 3)}


def summarize(rows):
    ok = [r for r in rows if r.get("status") == "OK"]
    return {"completed": len(ok), "no_report": sum(r.get("status") == "NO_REPORT" for r in rows),
            "errors": sum(r.get("status") == "ERROR" for r in rows),
            "approved_excess": _stats([r["excess"] for r in ok if r["approved"]]),
            "rejected_excess": _stats([r["excess"] for r in ok if not r["approved"]]),
            "approved_ret": _stats([r["ret"] for r in ok if r["approved"]]),
            "rejected_ret": _stats([r["ret"] for r in ok if not r["approved"]]),
            "all_excess": _stats([r["excess"] for r in ok])}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=["KR", "US"], required=True)
    parser.add_argument("--triggers", required=True)
    parser.add_argument("--bars", required=True)
    parser.add_argument("--bench", required=True)
    parser.add_argument("--bench-code", required=True)
    parser.add_argument("--reports-root", default=str(ROOT))
    parser.add_argument("--out", required=True)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    records = [json.loads(line) for line in open(args.triggers, encoding="utf-8")]
    records = [r for r in records if r.get("status") == "TRIGGERED" and (r.get("trade") or {}).get("status") == "CLOSED"]
    bars = {t: load_bars(v) for t, v in json.loads(Path(args.bars).read_text()).items()}
    bench = load_bars(json.loads(Path(args.bench).read_text())["bars"][args.bench_code])
    if args.dry_run:
        found = [latest_report(args.reports_root, args.market, r["ticker"], r["trigger_date"]) for r in records]
        print(json.dumps({"triggers": len(records), "with_report": sum(f is not None for f in found)}))
        if records and found[0]:
            print(technical_block(bars[records[0]["ticker"]], records[0]["trigger_index"], records[0]["entry"],
                                  records[0]["pivot"], args.market, bench))
        return 0
    rows = asyncio.run(run(records, args.market, bars, bench, args.reports_root, args.out, args.concurrency))
    summary = json.dumps(summarize(rows), ensure_ascii=False, indent=1)
    Path(args.out + ".summary.json").write_text(summary + "\n", encoding="utf-8")
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())

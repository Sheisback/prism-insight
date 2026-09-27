"""Paired, order-free BUY replay: the same past decision with and without the facts block.

    python tools/replay_buy_decision_facts.py --market KR --limit 24 --out runtime/replay_kr.jsonl
    python tools/replay_buy_decision_facts.py --market KR --limit 3 --dry-run   # build prompts only

Arm A = current BUY instruction without the decision_inputs contract and prompt block.
Arm B = the same plus both. Everything else is identical and taken from the decision
time: the stored report markdown, the stored trend facts and the decision price.
Codex runs WITHOUT MCP tools so neither arm can read post-decision prices. Past
holdings and journal lessons are not reconstructed (identical in both arms).
Each arm's scenario then goes through the production deterministic buy gate with
the stored regime. Outcomes are the performance tracker's 7/14/30-day returns.
No orders, no DB writes, no channel sends.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from observability.reentry_shadow import session_date  # noqa: E402
from prism_core.decision_input_features import compute, render_facts_block  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
SQL = {
    "KR": """SELECT w.id, w.ticker, w.company_name, w.analyzed_date, w.current_price, w.buy_score, w.min_score,
                    w.decision, w.trigger_type, w.trigger_mode, w.scenario, p.tracked_7d_return, p.tracked_14d_return,
                    p.tracked_30d_return
             FROM watchlist_history w JOIN analysis_performance_tracker p ON p.watchlist_id = w.id
             WHERE substr(w.analyzed_date, 1, 10) BETWEEN ? AND ? AND p.tracked_7d_return IS NOT NULL
             ORDER BY w.analyzed_date DESC""",
    "US": """SELECT w.id, w.ticker, w.company_name, w.analyzed_date, w.current_price, w.buy_score, w.min_score,
                    w.decision, w.trigger_type, w.trigger_mode, w.scenario, p.return_7d, p.return_14d, p.return_30d
             FROM us_watchlist_history w JOIN us_analysis_performance_tracker p
               ON p.decision_id = json_extract(w.scenario, '$._decision_id')
             WHERE substr(w.analyzed_date, 1, 10) BETWEEN ? AND ? AND p.return_7d IS NOT NULL
             ORDER BY w.analyzed_date DESC""",
}
REPORT_DIR = {"KR": Path("reports"), "US": Path("prism-us/reports")}


def select_decisions(db_path, market, since, until, limit, near=3, reports_root=ROOT):
    """Pre-registered sample: decisions within `near` points of the minimum, newest first."""
    with sqlite3.connect("file:" + str(db_path) + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows, seen = [], set()
        for row in conn.execute(SQL[market], (since, until)):
            scenario = json.loads(row["scenario"] or "{}")
            decision_id = scenario.get("_decision_id") or ""
            facts = scenario.get("_deterministic_trend_facts")
            if not decision_id.startswith("report:") or not facts or decision_id in seen:
                continue
            if row["buy_score"] is None or row["min_score"] is None or row["min_score"] - row["buy_score"] > near:
                continue
            report = Path(reports_root) / REPORT_DIR[market] / (decision_id[len("report:"):].rsplit(".", 1)[0] + ".md")
            if not report.exists():
                continue
            seen.add(decision_id)
            rows.append({"id": row["id"], "ticker": str(row["ticker"]), "company": row["company_name"],
                         "decided_at": row["analyzed_date"], "price": row["current_price"],
                         "stored_decision": row["decision"], "stored_score": row["buy_score"],
                         "min_score": row["min_score"], "trigger_type": row["trigger_type"],
                         "trigger_mode": row["trigger_mode"], "decision_id": decision_id, "report": str(report),
                         "trend_facts": facts, "regime": scenario.get("_deterministic_market_regime")
                         or scenario.get("market_regime"),
                         "outcome": {"7d": row[11], "14d": row[12], "30d": row[13]}})
            if len(rows) >= limit:
                break
        return rows


def bars_before(market, ticker, session):
    """Daily bars strictly before the decision session (the decision-day bar was unfinished)."""
    import pandas as pd
    start = (pd.Timestamp(session) - pd.Timedelta(days=150))
    if market == "KR":
        from cores.market_data.kis_source import KisSource
        frame = KisSource().price_history(ticker, start.strftime("%Y%m%d"),
                                         (pd.Timestamp(session) - pd.Timedelta(days=1)).strftime("%Y%m%d"),
                                         adjusted=True)
    else:
        import yfinance as yf
        with contextlib.redirect_stdout(sys.stderr):
            frame = yf.download(ticker, start=start.strftime("%Y-%m-%d"), end=session, auto_adjust=False,
                                progress=False, multi_level_index=False)
    from prism_core.decision_input_features import bars_from_frame
    return [b for b in bars_from_frame(frame, limit=120) if b["date"] < session]


def us_earnings_asof(ticker, session):
    try:
        import yfinance as yf
        with contextlib.redirect_stdout(sys.stderr):
            dates = yf.Ticker(ticker).get_earnings_dates(limit=16)
        day = datetime.fromisoformat(session).date()
        upcoming = sorted(d.date() for d in dates.index if d.date() >= day)
        if not upcoming:
            return {"status": "MISSING", "reason": "no_upcoming_date"}
        return {"status": "OK", "next_earnings_date": upcoming[0].isoformat(),
                "calendar_days_to_earnings": (upcoming[0] - day).days, "source": "yfinance_earnings_dates"}
    except Exception as error:  # noqa: BLE001
        return {"status": "MISSING", "reason": type(error).__name__}


def facts_block(item, market, language):
    session = session_date(item["decided_at"], market)
    decided = datetime.fromisoformat(item["decided_at"][:19]).replace(tzinfo=KST)
    bars = bars_before(market, item["ticker"], session)
    result = compute(bars, market=market, observed_at=decided, current_price=item["price"])
    earnings = us_earnings_asof(item["ticker"], session) if market == "US" else None
    block, flags = render_facts_block(result, None, earnings, market=market, language=language)
    return block, flags, result


def user_prompt(item, market, report, facts=""):
    trigger = (f"\n### 📡 Trigger Info (Apply Trigger-Based Entry Criteria)\n- **Triggered By**: {item['trigger_type']}\n"
               f"- **Trigger Mode**: {item['trigger_mode'] or 'unknown'}\n") if item["trigger_type"] else ""
    head = ("This is an AI analysis report for a stock. Please generate a trading scenario based on this report."
            if market == "KR" else
            "This is an AI analysis report for a US stock. Please generate a trading scenario based on this report.")
    return (f"{head}\n\n### Current Portfolio Status:\nCurrent holdings: 0/10 (replay: past holdings not reconstructed)\n"
            f"{trigger}\n### Trading Value Analysis:\n\n{item['trend_facts']}\n{facts}\n### Report Content:\n{report}\n")


def instruction(market, language, with_facts):
    previous = os.environ.get("PRISM_BUY_DECISION_FACTS")
    os.environ["PRISM_BUY_DECISION_FACTS"] = "true" if with_facts else "false"
    try:
        if market == "KR":
            from cores.agents.trading_agents import create_trading_scenario_agent
            return create_trading_scenario_agent(language=language).instruction
        import importlib.util
        spec = importlib.util.spec_from_file_location("us_trading_agents_replay",
                                                      ROOT / "prism-us/cores/agents/trading_agents.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.create_us_trading_scenario_agent(language=language).instruction
    finally:
        if previous is None:
            os.environ.pop("PRISM_BUY_DECISION_FACTS", None)
        else:
            os.environ["PRISM_BUY_DECISION_FACTS"] = previous


def gate(scenario, item):
    from cores.buy_gate import evaluate_production_buy_gate
    decision = str(scenario.get("decision", "")).strip().lower()
    entering = decision in {"진입", "enter", "entry", "buy"}
    if not entering:
        return {"entering": False, "allowed": False, "reason": "llm_no_entry"}
    result = evaluate_production_buy_gate(scenario, current_price=item["price"], market_regime=item["regime"],
                                          trend_facts=str(item["trend_facts"]))
    return {"entering": True, "allowed": bool(result.get("allowed")), "reason": result.get("reason")}


async def run_arm(system, user, settings):
    from cores.llm.codex_oauth_fast_backend import generate_codex_fast_async
    from cores.utils import parse_llm_json
    result = await generate_codex_fast_async(system_prompt=system, user_prompt=user, model=settings.model,
                                             reasoning_effort=settings.reasoning_effort, timeout=settings.timeout,
                                             mcp_profile=None, require_mcp_calls=False)
    return parse_llm_json(result.text, context="decision facts replay") or {}


def summarize(scenario):
    keys = ("decision", "buy_score", "effective_score", "min_score", "momentum_signal_count",
            "additional_confirmation_count", "rejection_reason")
    return {k: scenario.get(k) for k in keys}


async def replay(items, market, language, out, concurrency):
    from prism_core.codex_config import resolve_buy_codex_settings
    settings = resolve_buy_codex_settings()
    systems = {False: instruction(market, language, False), True: instruction(market, language, True)}
    semaphore = asyncio.Semaphore(concurrency)

    async def one(item):
        async with semaphore:
            record = {k: item[k] for k in ("id", "ticker", "company", "decided_at", "decision_id", "stored_decision",
                                           "stored_score", "min_score", "trigger_type", "regime", "outcome")}
            try:
                block, flags, _ = await asyncio.to_thread(facts_block, item, market, language)
                report = Path(item["report"]).read_text(encoding="utf-8")
                record["facts_flags"], record["facts_block"] = flags, block
                for arm, with_facts in (("A", False), ("B", True)):
                    scenario = await run_arm(systems[with_facts],
                                             user_prompt(item, market, report, block if with_facts else ""), settings)
                    record[arm] = {**summarize(scenario), "gate": gate(scenario, item)}
            except Exception as error:  # noqa: BLE001 - one failure never stops the batch
                record["error"] = f"{type(error).__name__}: {str(error)[:200]}"
            with open(out, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            return record

    return await asyncio.gather(*(one(item) for item in items))


def report_summary(records):
    done = [r for r in records if "A" in r and "B" in r]
    flips = [r for r in done if r["A"]["gate"]["allowed"] != r["B"]["gate"]["allowed"]]
    llm_flips = [r for r in done if r["A"]["gate"]["entering"] != r["B"]["gate"]["entering"]]

    def delta(key):
        values = [(r["B"].get(key) or 0) - (r["A"].get(key) or 0) for r in done
                  if isinstance(r["A"].get(key), (int, float)) and isinstance(r["B"].get(key), (int, float))]
        return round(sum(values) / len(values), 3) if values else None

    return {"completed": len(done), "errors": len(records) - len(done),
            "final_entry_A": sum(r["A"]["gate"]["allowed"] for r in done),
            "final_entry_B": sum(r["B"]["gate"]["allowed"] for r in done),
            "llm_entry_A": sum(r["A"]["gate"]["entering"] for r in done),
            "llm_entry_B": sum(r["B"]["gate"]["entering"] for r in done),
            "mean_delta_buy_score": delta("buy_score"), "mean_delta_momentum": delta("momentum_signal_count"),
            "mean_delta_confirmation": delta("additional_confirmation_count"),
            "final_flips": [{"ticker": r["ticker"], "decided_at": r["decided_at"][:10],
                             "A": r["A"]["gate"]["allowed"], "B": r["B"]["gate"]["allowed"],
                             "outcome": r["outcome"]} for r in flips],
            "llm_flips": [{"ticker": r["ticker"], "A": r["A"]["decision"], "B": r["B"]["decision"],
                           "outcome": r["outcome"]} for r in llm_flips]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=["KR", "US"], required=True)
    parser.add_argument("--db", default=str(ROOT / "stock_tracking_db.sqlite"))
    parser.add_argument("--reports-root", default=str(ROOT), help="repository holding reports/ (production checkout)")
    parser.add_argument("--since", default=(datetime.now(timezone.utc) - timedelta(days=40)).date().isoformat())
    parser.add_argument("--until", default=(datetime.now(timezone.utc) - timedelta(days=9)).date().isoformat())
    parser.add_argument("--limit", type=int, default=24)
    parser.add_argument("--language", default="ko")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--out", default=str(ROOT / "runtime/replay_buy_decision_facts.jsonl"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    items = select_decisions(args.db, args.market, args.since, args.until, args.limit, reports_root=args.reports_root)
    if args.dry_run:
        for item in items[:3]:
            block, flags, _ = facts_block(item, args.market, args.language)
            print(json.dumps({"ticker": item["ticker"], "decided_at": item["decided_at"], "flags": flags},
                             ensure_ascii=False))
            print(block)
        print(json.dumps({"selected": len(items)}))
        return 0
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    records = asyncio.run(replay(items, args.market, args.language, args.out, args.concurrency))
    print(json.dumps(report_summary(records), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

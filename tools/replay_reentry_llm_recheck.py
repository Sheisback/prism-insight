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

from observability.reentry_recheck_inputs import (  # noqa: E402
    _as_dt, archived_report, latest_report, recheck_instruction, strip_embedded_images, technical_block,
)
from tools.replay_pivot_reentry import load_bars  # noqa: E402


def instruction(market):
    os.environ["PRISM_BUY_DECISION_FACTS"] = "true"
    return recheck_instruction(market, ROOT)


async def run(records, market, bars_by_ticker, bench, reports_root, out, concurrency, archive_db=None):
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
                if report is None and archive_db:
                    report = archived_report(archive_db, market, rec["ticker"], rec["trigger_date"])
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
                        f"{strip_embedded_images(report.read_text(encoding='utf-8'))}\n")
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
    parser.add_argument("--archive-db", help="report_archive database used when the report file is gone")
    parser.add_argument("--out", required=True)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    records = [json.loads(line) for line in open(args.triggers, encoding="utf-8")]
    records = [r for r in records if r.get("status") == "TRIGGERED" and (r.get("trade") or {}).get("status") == "CLOSED"]
    bars = {t: load_bars(v) for t, v in json.loads(Path(args.bars).read_text()).items()}
    bench = load_bars(json.loads(Path(args.bench).read_text())["bars"][args.bench_code])
    if args.dry_run:
        found = [latest_report(args.reports_root, args.market, r["ticker"], r["trigger_date"])
                 or (archived_report(args.archive_db, args.market, r["ticker"], r["trigger_date"]) if args.archive_db else None)
                 for r in records]
        print(json.dumps({"triggers": len(records), "with_report": sum(f is not None for f in found)}))
        if records and found[0]:
            print(technical_block(bars[records[0]["ticker"]], records[0]["trigger_index"], records[0]["entry"],
                                  records[0]["pivot"], args.market, bench))
        return 0
    rows = asyncio.run(run(records, args.market, bars, bench, args.reports_root, args.out, args.concurrency,
                           archive_db=args.archive_db))
    summary = json.dumps(summarize(rows), ensure_ascii=False, indent=1)
    Path(args.out + ".summary.json").write_text(summary + "\n", encoding="utf-8")
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())

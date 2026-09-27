"""Exit-rule hypotheses on the trades the system actually bought (research only).

    python tools/replay_exit_rules.py --db stock_tracking_db.sqlite --market KR \
        --bars bars_KR.json --bench bench_KR.json --bench-code 1001

Same entries (buy date / buy price from trade history), different exits:
  BASE   production rules as fixed on 2026-09-27 (intraday -7% hard stop, MA50 while
         losing, trailing after +5% with the regime band)
  H1     close-based -7% stop with a -10% intraday catastrophe floor
  H2     BASE + breakeven lock once trailing is active
  H1H2   both
Pre-registered on 2026-09-27 before looking at results. The realized DB result is
shown beside BASE to check calibration. Day-0 intraday order is unknown, so a
day-0 low below the stop is treated as a stop for intraday rules (pessimistic).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from observability.reentry_shadow import session_date  # noqa: E402
from prism_core import pivot_reentry as P  # noqa: E402
from tools.replay_pivot_reentry import bench_return, bull_fn, load_bars  # noqa: E402

TRADES_SQL = {
    "KR": "SELECT ticker, buy_date, buy_price, sell_date, profit_rate FROM trading_history",
    "US": "SELECT ticker, buy_date, buy_price, sell_date, profit_rate FROM us_trading_history",
}
VARIANTS = {"BASE": {}, "H1": {"close_stop": True}, "H2": {"breakeven_lock": True},
            "H1H2": {"close_stop": True, "breakeven_lock": True}}


def _stats(values):
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0}
    out = {"n": len(values), "mean": round(statistics.mean(values), 4), "median": round(statistics.median(values), 4),
           "win": round(sum(v > 0 for v in values) / len(values), 3)}
    if len(values) > 2:
        out["mean_wo_best"] = round(statistics.mean(sorted(values)[:-1]), 4)
    return out


def replay(db_path, market, bars_by_ticker, bench_bars):
    bull = bull_fn(bench_bars)
    bench_by_date = {b["date"]: b for b in bench_bars}
    rows = []
    with sqlite3.connect("file:" + str(db_path) + "?mode=ro", uri=True) as conn:
        for ticker, buy_date, buy_price, sell_date, realized in conn.execute(TRADES_SQL[market]):
            bars = bars_by_ticker.get(str(ticker))
            if not bars or not buy_price:
                continue
            day = session_date(buy_date, market)
            i = next((k for k, b in enumerate(bars) if b["date"] == day), None)
            if i is None or not (bars[i]["low"] * 0.97 <= buy_price <= bars[i]["high"] * 1.03):
                continue
            row = {"ticker": str(ticker), "buy_date": day, "realized": realized / 100.0}
            for name, kwargs in VARIANTS.items():
                trade = P.simulate_production(bars, i, float(buy_price), intraday=True, bull=bull, **kwargs)
                if trade.get("status") != "CLOSED":
                    row[name] = None
                    continue
                bench = bench_return(bench_by_date, bars, i, trade["exit_index"], entry_at_open=False)
                row[name] = {"ret": trade["ret"], "excess": None if bench is None else trade["ret"] - bench,
                             "reason": trade["exit_reason"], "bars": trade["bars"]}
            rows.append(row)
    return rows


def summarize(rows):
    complete = [r for r in rows if all(r.get(v) for v in VARIANTS)]
    out = {"trades": len(rows), "complete": len(complete),
           "realized": _stats([r["realized"] for r in complete])}
    for name in VARIANTS:
        out[name] = {"ret": _stats([r[name]["ret"] for r in complete]),
                     "excess": _stats([r[name]["excess"] for r in complete])}
        if name != "BASE":
            diffs = [r[name]["ret"] - r["BASE"]["ret"] for r in complete]
            out[name]["paired_vs_base"] = {**_stats(diffs), "better": sum(d > 0 for d in diffs),
                                           "worse": sum(d < 0 for d in diffs)}
    reasons = {}
    for r in complete:
        reasons[r["BASE"]["reason"]] = reasons.get(r["BASE"]["reason"], 0) + 1
    out["base_exit_reasons"] = reasons
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--market", choices=["KR", "US"], required=True)
    parser.add_argument("--bars", required=True)
    parser.add_argument("--bench", required=True)
    parser.add_argument("--bench-code", required=True)
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    bars = {t: load_bars(v) for t, v in json.loads(Path(args.bars).read_text()).items()}
    bench = load_bars(json.loads(Path(args.bench).read_text())["bars"][args.bench_code])
    rows = replay(args.db, args.market, bars, bench)
    if args.out:
        Path(args.out).write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    print(json.dumps(summarize(rows), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

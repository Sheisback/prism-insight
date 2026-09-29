#!/usr/bin/env python3
"""Offline replay for docs/entry-quality-experiments/confirmed-resistance-target-v1.md.

Research only: never imported by the trading path, never places orders.

  extract  (db-server, read-only): decisions in the preregistered window + daily bars
           -> one JSON input file.
  replay   (anywhere): deterministic recomputation from that file -> JSON report.

The rule, sample and verdict thresholds are fixed by the preregistration; do not tune
them here. A changed rule is a new version with a new preregistration.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

RULE_VERSION = "confirmed-resistance-target-v1"
WINDOW = ("2026-09-01 00:00:00", "2026-09-29 23:59:59")
LOOKBACK = 250
ABOVE = 1.005
NEAR = 0.01
OUTCOME_SESSIONS = 10
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")

TABLES = {
    "KR": {"reject": "watchlist_history", "entries": ("trading_history", "stock_holdings")},
    "US": {"reject": "us_watchlist_history", "entries": ("us_trading_history", "us_stock_holdings")},
}


# ----------------------------------------------------------------------------- extract

def _decisions(conn, market):
    rows = []
    t = TABLES[market]
    cur = conn.execute(
        f"SELECT id, ticker, analyzed_date, current_price, decision, scenario FROM {t['reject']} "
        "WHERE analyzed_date BETWEEN ? AND ? ORDER BY id", WINDOW)
    for rid, ticker, at, price, decision, scenario in cur:
        rows.append({"market": market, "kind": "rejected", "source": f"{t['reject']}#{rid}", "ticker": ticker,
                     "recorded_at": at, "price": price, "decision": decision, "scenario": scenario})
    for table in t["entries"]:
        cur = conn.execute(
            f"SELECT id, ticker, buy_date, buy_price, scenario FROM {table} "
            "WHERE buy_date BETWEEN ? AND ? ORDER BY id", WINDOW)
        for rid, ticker, at, price, scenario in cur:
            rows.append({"market": market, "kind": "entered", "source": f"{table}#{rid}", "ticker": ticker,
                         "recorded_at": at, "price": price, "decision": "entry", "scenario": scenario})
    return rows


def _bars_kr(ticker, start, end):
    from cores.stock_chart import get_market_ohlcv_by_date
    df = get_market_ohlcv_by_date(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), ticker, adjusted=True)
    return [{"date": str(ix.date()), "high": float(r["High"]), "low": float(r["Low"]), "close": float(r["Close"])}
            for ix, r in df.iterrows()]


def _bars_us(ticker, start, end):
    import yfinance as yf
    df = yf.Ticker(ticker).history(start=start.strftime("%Y-%m-%d"), end=(end + timedelta(days=1)).strftime("%Y-%m-%d"))
    out = []
    for ix, r in df.iterrows():
        if any(math.isnan(float(r[c])) for c in ("High", "Low", "Close")):
            continue  # an unfinished/blank provider row is not a bar
        out.append({"date": str(ix.tz_convert(NY).date()), "high": float(r["High"]),
                    "low": float(r["Low"]), "close": float(r["Close"])})
    return out


def extract(db, out):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    decisions = [d for m in ("KR", "US") for d in _decisions(conn, m)]
    conn.close()
    start, end = datetime(2025, 7, 1), datetime.now()
    bars = {}
    for market, ticker in sorted({(d["market"], d["ticker"]) for d in decisions}):
        try:
            bars[f"{market}:{ticker}"] = (_bars_kr if market == "KR" else _bars_us)(ticker, start, end)
        except Exception as error:  # noqa: BLE001 - missing bars stay MISSING
            bars[f"{market}:{ticker}"] = {"error": type(error).__name__}
        time.sleep(0.35)
    payload = {"rule_version": RULE_VERSION, "window": WINDOW, "extracted_at": datetime.now(KST).isoformat(),
               "decisions": decisions, "bars": bars}
    Path(out).write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    print(f"decisions={len(decisions)} tickers={len(bars)} -> {out}")


# ----------------------------------------------------------------------------- replay

def session_date(market, recorded_at):
    stamp = datetime.fromisoformat(str(recorded_at)[:19]).replace(tzinfo=KST)
    return (stamp if market == "KR" else stamp.astimezone(NY)).date().isoformat()


def resistance_candidates(confirmed):
    """Every confirmed bar high in the last 250 sessions: prices that actually traded.

    Pivot-only candidates were dropped before any real-data run: in a fast decline
    every day's high is lower than the day before, so mid-crash supply was never a
    pivot and the nearest resistance jumped to the 52-week high.
    """
    highs = [b["high"] for b in confirmed[-LOOKBACK:]]
    return sorted(set(highs)), (max(highs) if highs else None)


def confirmed_target(entry, confirmed):
    cands, high52 = resistance_candidates(confirmed)
    if high52 and high52 * 0.95 <= entry <= high52 * 1.05:
        others = [c for c in cands if c != high52 and entry < c <= entry * 1.20]
        if not others:
            return {"target": entry * 1.20, "basis": "oneil_2a", "resistance": high52}
    above = [c for c in cands if c > entry * ABOVE]
    if not above:
        return {"target": None, "basis": "MISSING_no_confirmed_resistance", "resistance": None}
    r = min(above)
    return {"target": entry + 0.8 * (r - entry), "basis": "structural", "resistance": r}


def _num(value):
    try:
        v = float(str(value).replace(",", ""))
        return v if math.isfinite(v) and v > 0 else None
    except (TypeError, ValueError):
        return None


def outcome(entry, target, stop, after):
    if len(after) < OUTCOME_SESSIONS or not target or not stop:
        return {"status": "MISSING_immature" if len(after) < OUTCOME_SESSIONS else "MISSING_levels"}
    first = "none"
    for bar in after[:OUTCOME_SESSIONS]:
        hit_t, hit_s = bar["high"] >= target, bar["low"] <= stop
        if hit_t and hit_s:
            first = "ambiguous_same_bar"
            break
        if hit_s:
            first = "stop"
            break
        if hit_t:
            first = "target"
            break
    return {"status": "matured", "first": first,
            "close_return_pct": round((after[OUTCOME_SESSIONS - 1]["close"] / entry - 1) * 100, 3)}


def _gate(scenario, entry, target, stop):
    from cores.buy_gate import evaluate_production_buy_gate
    rr = (target - entry) / (entry - stop) if target and stop and entry > stop else None
    data = {**scenario, "decision": "entry", "target_price": target, "risk_reward_ratio": rr,
            # Keep the scenario's own arithmetic consistent with the replaced target.
            "expected_return_pct": (target / entry - 1) * 100 if target else None,
            "expected_loss_pct": (1 - stop / entry) * 100 if stop else None}
    policy = scenario.get("regime_entry_policy") or {}
    pilot = isinstance(policy, dict) and policy.get("mode") == "rebound_pilot" and policy.get("position_fraction") == 0.5
    result = evaluate_production_buy_gate(
        data, current_price=entry,
        market_regime=scenario.get("_deterministic_market_regime") or scenario.get("market_regime"),
        pilot_budget_available=pilot, score_override=scenario.get("buy_score"),
        trend_facts=str(scenario.get("_deterministic_trend_facts") or ""))
    return rr, bool(result.get("allowed")), [f.get("code") for f in result.get("findings", []) if f.get("hard", True)]


_RR_WORDS = ("R/R", "손익비", "risk/reward", "risk-reward")
_OTHER_WORDS = ("펀더멘털", "F1", "F2", "F3", "F4", "과열", "괴리", "순매도", "추세", "T1", "T2", "하락", "점수",
                "fundamental", "overextended", "trend", "score", "momentum", "모멘텀", "복합")


def replay(inp, out):
    payload = json.loads(Path(inp).read_text())
    rows = []
    for d in payload["decisions"]:
        row = {k: d[k] for k in ("market", "kind", "source", "ticker", "recorded_at")}
        try:
            scenario = json.loads(d["scenario"]) if isinstance(d["scenario"], str) else (d["scenario"] or {})
        except (TypeError, ValueError):
            scenario = {}
        levels = ((scenario.get("trading_scenarios") or {}).get("key_levels") or {})
        entry = _num(scenario.get("entry_price")) or _num(d["price"])
        stop = _num(scenario.get("stop_loss"))
        recorded_r = _num(levels.get("primary_resistance"))
        reason = str(scenario.get("rejection_reason") or "")
        bars = payload["bars"].get(f"{d['market']}:{d['ticker']}")
        row.update(entry=entry, stop=stop, recorded_target=_num(scenario.get("target_price")),
                   recorded_rr=_num(scenario.get("risk_reward_ratio")), recorded_resistance=recorded_r,
                   rr_rejection=any(w.lower() in reason.lower() for w in _RR_WORDS))
        if not isinstance(bars, list) or not entry or not stop:
            row["status"] = "MISSING_inputs"
            rows.append(row)
            continue
        day = session_date(d["market"], d["recorded_at"])
        confirmed = [b for b in bars if b["date"] < day]
        today = next((b for b in bars if b["date"] == day), None)
        after = [b for b in bars if b["date"] > day]
        cands, _ = resistance_candidates(confirmed)
        new = confirmed_target(entry, confirmed)
        row.update(session_date=day, day_high=today["high"] if today else None, new=new)
        row["artificial_resistance"] = bool(
            recorded_r and today and recorded_r <= today["high"] * 1.0001
            and not any(abs(recorded_r / c - 1) <= NEAR for c in cands))
        if new["target"]:
            rr, allowed, blocks = _gate(scenario, entry, new["target"], stop)
            row.update(new_rr=round(rr, 3) if rr else None, new_gate_allowed=allowed, new_gate_blocks=blocks)
            _, orig_allowed, orig_blocks = _gate(scenario, entry, row["recorded_target"], stop) \
                if row["recorded_target"] else (None, None, ["missing_target"])
            row.update(orig_gate_allowed=orig_allowed, orig_gate_blocks=orig_blocks)
        row["other_llm_reason"] = any(w.lower() in reason.lower() for w in _OTHER_WORDS)
        row["outcome_new"] = outcome(entry, new["target"], stop, after)
        row["status"] = "ok"
        rows.append(row)

    rr_rej = [r for r in rows if r["kind"] == "rejected" and r.get("rr_rejection") and r.get("status") == "ok"]
    m1 = sum(r["artificial_resistance"] for r in rr_rej)
    flips = [r for r in rows if r["kind"] == "rejected" and r.get("status") == "ok" and r.get("new_gate_allowed")]
    clean = [r for r in flips if not r["other_llm_reason"]]
    matured = [r for r in clean if r["outcome_new"]["status"] == "matured"]
    t_first = sum(r["outcome_new"]["first"] == "target" for r in matured)
    s_first = sum(r["outcome_new"]["first"] in ("stop", "ambiguous_same_bar") for r in matured)
    rets = sorted(r["outcome_new"]["close_return_pct"] for r in matured)
    median = (rets[len(rets) // 2] if len(rets) % 2 else (rets[len(rets) // 2 - 1] + rets[len(rets) // 2]) / 2) if rets else None

    def verdict(mat, med, tf, sf):
        share = m1 / len(rr_rej) if rr_rej else 0
        if share < 0.25:
            return "RETIRE"
        if len(clean) >= 5 and len(mat) >= 5 and tf >= sf and med is not None and med > 0:
            return "PROCEED_IMPLEMENT_FACTS"
        return "FACTS_ONLY"

    best = max(matured, key=lambda r: r["outcome_new"]["close_return_pct"], default=None)
    rest = [r for r in matured if r is not best]
    rrest = sorted(r["outcome_new"]["close_return_pct"] for r in rest)
    med_rest = (rrest[len(rrest) // 2] if len(rrest) % 2 else (rrest[len(rrest) // 2 - 1] + rrest[len(rrest) // 2]) / 2) if rrest else None
    entered = [r for r in rows if r["kind"] == "entered" and r.get("status") == "ok"]
    summary = {
        "rule_version": RULE_VERSION, "input_sha256": hashlib.sha256(Path(inp).read_bytes()).hexdigest(),
        "decisions": len(rows), "missing_inputs": sum(r.get("status") != "ok" for r in rows),
        "M1_rr_rejections": len(rr_rej), "M1_artificial": m1,
        "M1_share": round(m1 / len(rr_rej), 3) if rr_rej else None,
        "M2_flips_gate_allowed": len(flips), "M2_flips_without_other_llm_reason": len(clean),
        "M3_entered": len(entered),
        "M3_entered_blocked_by_new_target": sum(1 for r in entered if r.get("new_gate_allowed") is False),
        "M4_matured_clean_flips": len(matured), "M4_target_first": t_first, "M4_stop_or_ambiguous_first": s_first,
        "M4_median_10s_close_return_pct": median,
        "verdict": verdict(matured, median, t_first, s_first),
        "verdict_without_best_winner": verdict(rest, med_rest,
                                               sum(r["outcome_new"]["first"] == "target" for r in rest),
                                               sum(r["outcome_new"]["first"] in ("stop", "ambiguous_same_bar") for r in rest)),
    }
    Path(out).write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1, sort_keys=True))
    print(json.dumps(summary, ensure_ascii=False, indent=1))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract")
    e.add_argument("--db", default="stock_tracking_db.sqlite")
    e.add_argument("--out", required=True)
    r = sub.add_parser("replay")
    r.add_argument("--input", required=True)
    r.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "extract":
        extract(args.db, args.out)
    else:
        replay(args.input, args.out)


if __name__ == "__main__":
    sys.exit(main())

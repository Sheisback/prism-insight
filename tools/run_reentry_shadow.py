"""After-close re-entry SHADOW runner (KR/US). Research only; never orders or LLM calls.

Usage (db-server cron, after the market close):
    python tools/run_reentry_shadow.py --market KR
    python tools/run_reentry_shadow.py --market US
    python tools/run_reentry_shadow.py --market KR --dry-run   # no state/event writes
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from observability import reentry_shadow  # noqa: E402

HISTORY_DAYS = 210
CLOSE_BUFFER = {"KR": (16, 30, "Asia/Seoul", "XKRX"), "US": (17, 0, "America/New_York", "NYSE")}
KR_CACHE_DIR = ROOT / "runtime/reentry_kr_daily_cache"
log = logging.getLogger("reentry_shadow")


def completed_session(market, now=None):
    """Latest session whose close (plus buffer) has passed in the market's local time."""
    import pandas as pd
    import pandas_market_calendars as mcal

    hour, minute, tz, calendar = CLOSE_BUFFER[market]
    now = (now or datetime.now(ZoneInfo(tz))).astimezone(ZoneInfo(tz))
    end = now.date() if (now.hour, now.minute) >= (hour, minute) else now.date() - timedelta(days=1)
    days = mcal.get_calendar(calendar).valid_days(start_date=pd.Timestamp(end) - pd.Timedelta(days=15),
                                                 end_date=pd.Timestamp(end))
    if len(days) == 0:
        raise ValueError("missing_market_calendar")
    return days[-1].date().isoformat()


def _frame_rows(frame, end):
    import pandas as pd
    columns = ("Open", "High", "Low", "Close", "Volume")
    if frame.columns.duplicated().any() or frame.index.duplicated().any():
        raise ValueError("ambiguous_history")
    rows = []
    for day, row in frame.sort_index().iterrows():
        day = pd.Timestamp(day).date().isoformat()
        if day > end or any(pd.isna(row[k]) for k in columns):
            continue
        values = {k.lower(): float(row[k]) for k in columns}
        if not all(math.isfinite(v) for v in values.values()) or min(values[k] for k in ("open", "high", "low", "close")) <= 0:
            continue
        if values["volume"] <= 0:
            continue  # halted/empty sessions carry no traded price
        rows.append({"date": day, **values})
    return rows


def collect_us(tickers, completed):
    import pandas as pd
    import yfinance as yf

    start = (pd.Timestamp(completed) - pd.Timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    end = (pd.Timestamp(completed) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    symbols = list(dict.fromkeys(list(tickers) + ["SPY"]))
    with contextlib.redirect_stdout(sys.stderr):
        data = yf.download(symbols, start=start, end=end, interval="1d", auto_adjust=False, actions=False,
                           progress=False, threads=False, timeout=15, group_by="ticker")
    out = {}
    for symbol in symbols:
        try:
            frame = data[symbol] if isinstance(data.columns, pd.MultiIndex) else data
            rows = _frame_rows(frame, completed)
            if rows and rows[-1]["date"] == completed:
                out[symbol] = rows
        except (KeyError, TypeError, ValueError):
            continue
    spy = out.pop("SPY", [])
    out["__benchmark_rows"] = {t: spy for t in tickers} if spy else {}
    return out


def collect_kr(tickers, completed, *, source=None, master=None, cache_dir=None):
    """Sequential KIS daily reads with a per-session cache so reruns do not refetch."""
    import pandas as pd

    from observability.reentry_shadow import _atomic
    directory = Path(cache_dir) if cache_dir else KR_CACHE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    cache = directory / (completed + ".json")
    try:
        data = json.loads(cache.read_text()) if cache.exists() else {}
    except ValueError:
        data = {}
    if source is None:
        from cores.market_data.kis_source import KisSource
        source = KisSource()
    if master is None:
        from cores.kis_market_snapshot import fetch_kis_master_data
        master = fetch_kis_master_data(timeout=10)
    start = (pd.Timestamp(completed) - pd.Timedelta(days=HISTORY_DAYS)).strftime("%Y%m%d")
    end = completed.replace("-", "")
    benchmarks = {}
    for ticker in tickers:
        code = {"KOSPI": "1001", "KOSDAQ": "2001"}.get(master.markets.get(ticker))
        if code is None:
            continue
        benchmarks[ticker] = code
        for symbol, is_index in ((code, True), (ticker, False)):
            if symbol in data:
                continue
            try:
                frame = source.index_history(symbol, start, end) if is_index else \
                    source.price_history(symbol, start, end, adjusted=True)
                rows = _frame_rows(frame, completed)
                if rows and rows[-1]["date"] == completed:
                    data[symbol] = rows
                    _atomic(cache, data)
            except Exception:  # noqa: BLE001 - explicit missing input; provider detail stays out of events
                log.debug("KIS history unavailable for shadow symbol")
    out = {t: data[t] for t in tickers if t in data}
    out["__benchmark_rows"] = {t: data[c] for t, c in benchmarks.items() if c in data}
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=["KR", "US"], required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", default=str(reentry_shadow.DB_PATH))
    parser.add_argument("--state")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not reentry_shadow.enabled(args.market) and not args.dry_run:
        log.info("reentry shadow disabled for %s", args.market)
        return 0
    completed = completed_session(args.market)
    collector = collect_kr if args.market == "KR" else collect_us
    summary = reentry_shadow.run(args.market, completed, collector=collector, db_path=args.db,
                                 path=args.state, dry_run=args.dry_run)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

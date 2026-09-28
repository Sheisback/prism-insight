"""US eligibility metadata: liquidity-ordered, cached classification, KIS cap (#822 follow-up)."""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "prism-us"), str(ROOT)]


def _batch():
    with patch("dotenv.load_dotenv", return_value=False):
        import us_trigger_batch as batch
    return batch


INFO = {"quoteType": "EQUITY", "marketCap": 5e9, "currency": "USD", "exchange": "NMS",
        "sector": "Technology", "industry": "Software"}


def _run(monkeypatch, tmp_path, *, amounts, cache=None, screen_caps=None, budget=300, fetch_error=(),
         fetch_delay=0.0):
    batch = _batch()
    from prism_core import us_stock_universe as universe
    monkeypatch.setenv("US_SCREENING_UNIVERSE", "listed_common")
    monkeypatch.delenv("US_SCREENING_MIN_MARKET_CAP_USD", raising=False)
    path = tmp_path / "meta.json"
    if cache is not None:
        path.write_text(json.dumps(cache))
    monkeypatch.setenv("US_ELIGIBILITY_CACHE_PATH", str(path))
    monkeypatch.setattr(batch, "METADATA_BUDGET_SECONDS", budget)
    symbols = list(amounts)
    records = [universe.UniverseRecord(s, s + " Common Stock", "NASDAQ") for s in symbols]
    fetched = []

    def pair(trade_date, tickers):
        snap = pd.DataFrame({"Open": 10.0, "High": 11.0, "Low": 9.0, "Close": 10.5, "Volume": 1e7},
                            index=list(tickers))
        snap["Amount"] = [amounts[t] for t in tickers]
        return snap, snap.copy(), "20260911", {}

    def ticker(symbol):
        import time
        time.sleep(fetch_delay)
        fetched.append(symbol)
        if symbol in fetch_error:
            raise RuntimeError("provider")
        return SimpleNamespace(info=dict(INFO, shortName=symbol, longName=symbol + " Inc."))

    screen = None
    if screen_caps is not None:
        screen = pd.DataFrame({"Amount": 1e8, "ChangeRate": 1.0, "MarketCap": pd.Series(screen_caps)})
    with patch.object(universe, "fetch_universe", return_value=universe.UniverseResult(records, {"f": 1})), \
         patch.object(batch, "_kis_price_shortlist",
                      return_value=(symbols if screen is not None else None, screen, {"status": "TEST"})), \
         patch.object(batch, "get_batched_snapshot_pair", side_effect=pair), \
         patch("yfinance.Ticker", side_effect=ticker):
        result = batch._load_screening_inputs("20260914")
    saved = json.loads(path.read_text()) if path.exists() else {}
    return result[4], fetched, saved, list(result[1].index)


def _entry(symbol, days_old):
    stamp = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_old)
    return dict(INFO, shortName=symbol, longName=symbol + " Inc.", fetched_at=stamp.isoformat())


def test_fresh_cache_with_kis_cap_skips_the_provider(monkeypatch, tmp_path):
    diag, fetched, _, kept = _run(monkeypatch, tmp_path, amounts={"AAA": 2e8},
                                  cache={"AAA": _entry("AAA", 3)}, screen_caps={"AAA": 4e9})
    assert fetched == [] and kept == ["AAA"]
    assert diag["metadata_cache_hits"] == 1 and diag["metadata_status"] == "COMPLETE"


def test_expired_or_cap_stale_entries_are_refetched_and_saved(monkeypatch, tmp_path):
    cache = {"OLD": _entry("OLD", 8), "CAPSTALE": _entry("CAPSTALE", 2)}
    diag, fetched, saved, kept = _run(monkeypatch, tmp_path, amounts={"OLD": 2e8, "CAPSTALE": 1e8},
                                      cache=cache, screen_caps=None)
    assert sorted(fetched) == ["CAPSTALE", "OLD"] and sorted(kept) == ["CAPSTALE", "OLD"]
    for symbol in ("OLD", "CAPSTALE"):
        age = datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(saved[symbol]["fetched_at"])
        assert age < datetime.timedelta(minutes=5)


def test_budget_stop_keeps_cached_names(monkeypatch, tmp_path):
    diag, fetched, _, kept = _run(monkeypatch, tmp_path, amounts={"THIN": 6e7, "BIG": 9e8, "MID": 3e8},
                                  cache={"MID": _entry("MID", 1)}, screen_caps={"MID": 3e9}, budget=0)
    assert fetched == []                                   # budget already spent before any fetch
    assert kept == ["MID"]                                 # cached name still qualifies
    assert diag["exclusion_reasons"]["metadata_budget_exhausted"] == 2
    assert diag["metadata_status"] == "PARTIAL"


def test_provider_errors_are_unavailable_not_cached(monkeypatch, tmp_path):
    diag, _, saved, kept = _run(monkeypatch, tmp_path, amounts={"ERR": 2e8, "OK": 1e8},
                                screen_caps={"ERR": 2e9, "OK": 2e9}, fetch_error=("ERR",))
    assert kept == ["OK"] and "ERR" not in saved
    assert diag["exclusion_reasons"]["metadata_unavailable"] == 1


def test_empty_path_disables_the_cache(monkeypatch, tmp_path):
    batch = _batch()
    monkeypatch.setenv("US_ELIGIBILITY_CACHE_PATH", "")
    assert batch._eligibility_cache_path() is None
    assert batch._load_eligibility_cache(None) == {}
    batch._save_eligibility_cache(None, {"A": {}})         # no-op, no error


def test_budget_stop_drops_the_least_liquid_names_first(monkeypatch, tmp_path):
    batch = _batch()
    monkeypatch.setattr(batch, "METADATA_WORKERS", 1)
    diag, fetched, _, kept = _run(monkeypatch, tmp_path, amounts={"THIN": 6e7, "BIG": 9e8, "MID": 3e8},
                                  screen_caps={"THIN": 2e9, "BIG": 2e9, "MID": 2e9},
                                  budget=0.05, fetch_delay=0.1)
    assert fetched == ["BIG"] and kept == ["BIG"]            # the most liquid name is checked first
    assert diag["exclusion_reasons"]["metadata_budget_exhausted"] == 2

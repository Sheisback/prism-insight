"""Expanded US universe order: the snapshot budget must not drop NYSE large caps."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
RUN = r'''
import socket
import sys
from unittest.mock import patch
sys.path[:0] = ["prism-us", "."]
def forbidden(*a, **kw):
    raise AssertionError("network forbidden")
with patch.object(socket.socket, "connect", forbidden), patch("dotenv.load_dotenv", return_value=False):
    import us_trigger_batch as batch
    from prism_core import us_stock_universe as universe
    # Directory order: every NASDAQ row, then NYSE rows (as nasdaqtrader publishes).
    directory = ["AAPL", "ZNAS", "BNAS", "JPM", "XOM", "ANYSE"]

    with patch.object(batch, "get_major_tickers", return_value=["XOM", "JPM", "AAPL", "V"]):
        order = batch._prioritized_universe(directory)
    # Index members first, then A-Z; symbols outside the directory (V) are not added.
    assert order == ["AAPL", "JPM", "XOM", "ANYSE", "BNAS", "ZNAS"], order

    with patch.object(batch, "get_major_tickers", side_effect=RuntimeError("wiki down")):
        order = batch._prioritized_universe(directory)
    assert order == sorted(directory), order

    seen = {}
    def pair(trade_date, tickers):
        seen["tickers"] = list(tickers)
        raise RuntimeError("stop after order capture")
    records = [universe.UniverseRecord(s, s + " Common Stock", "NASDAQ") for s in directory]
    with patch.object(universe, "fetch_universe", return_value=universe.UniverseResult(records, {})), \
         patch.object(batch, "get_major_tickers", return_value=["JPM"]), \
         patch.object(batch, "get_batched_snapshot_pair", side_effect=pair):
        try:
            batch._load_screening_inputs("20260925")
        except RuntimeError as exc:
            assert "order capture" in str(exc)
    assert seen["tickers"][0] == "JPM" and seen["tickers"][1:] == sorted(set(directory) - {"JPM"}), seen
print("OK")
'''


def test_expanded_universe_puts_index_members_first_and_drops_exchange_order():
    env = dict(os.environ, US_SCREENING_UNIVERSE="listed_common", PRISM_DISABLE_SIGNAL_PUBLISH="1")
    result = subprocess.run([sys.executable, "-c", RUN], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout

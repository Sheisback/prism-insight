import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from prism_core.execution_service import ExecutionService
from prism_core.oneil_broker import OneilBroker
from prism_core.order_intents import IntentStore, OrderIntent


def intent(side="BUY", account="account"):
    return OrderIntent.create(market="US", account_id=account, symbol="AAPL", side=side,
        order_style="limit", source="oneil", source_position_id="position:1", quantity=2, limit_price="100", cash_amount="200")


class Response:
    def __init__(self, rows, *, output="output", continuation="", keys=("", ""), ok=True):
        self.body = SimpleNamespace(**{output: rows}, ctx_area_fk200=keys[0], ctx_area_nk200=keys[1])
        self.header = SimpleNamespace(tr_cont=continuation)
        self.ok = ok

    def isOK(self):
        return self.ok

    def getBody(self):
        return self.body

    def getHeader(self):
        return self.header


class Trader:
    account_name = "primary"
    account_key = "account"
    mode = "real"
    trenv = SimpleNamespace(my_acct="fixture", my_prod="01")

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []
        self.open = True

    def _request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.responses.pop(0)

    def is_market_open(self):
        return self.open

    async def async_buy_stock(self, **kwargs):
        self.calls.append(kwargs)
        return dict(success=True, order_no="123", quantity=2)

    async_sell_stock = async_buy_stock

    def cancel_order(self, **kwargs):
        self.calls.append(kwargs)
        return dict(success=True, order_no="456")


def broker(tmp_path, trader):
    store = IntentStore(tmp_path / "intents.db")
    return OneilBroker("primary", store, service_factory=lambda **kw: ExecutionService(trader, intent_store=kw["intent_store"]))


def fill(**changes):
    return dict(dict(odno="123", ord_dt="20260925", pdno="AAPL", sll_buy_dvsn_cd="02",
        ft_ord_qty="2", ft_ccld_qty="2", nccs_qty="0", ft_ccld_amt3="199.50",
        tr_crcy_cd="USD", ovrs_excg_cd="NASD"), **changes)


def test_exact_fill_not_acceptance_and_fee_remains_unknown(tmp_path):
    adapter = broker(tmp_path, Trader([Response([fill()]), Response([])]))
    result = asyncio.run(adapter.reconcile(intent(), "123", "20260925"))
    assert result["status"] == "FILLED" and result["filled_quantity"] == 2
    assert result["filled_notional"] == "199.50" and result["fees"] is None
    assert result["fees_status"] == "UNKNOWN" and not result["virtual"]


@pytest.mark.parametrize("change", [{"ord_dt": "20260924"}, {"odno": "124"}, {"pdno": "OTHER"},
    {"sll_buy_dvsn_cd": "01"}, {"ft_ccld_qty": ""}, {"ft_ccld_qty": "3"},
    {"tr_crcy_cd": "KRW"}, {"ft_ccld_amt3": "nan"}])
def test_mismatch_or_missing_never_implies_fill(tmp_path, change):
    adapter = broker(tmp_path, Trader([Response([fill(**change)]), Response([])]))
    assert asyncio.run(adapter.reconcile(intent(), "123", "20260925"))["status"] == "UNKNOWN"


def test_partial_requires_matching_pending_quantity(tmp_path):
    rows = [Response([fill(ft_ccld_qty="1", nccs_qty="1", ft_ccld_amt3="99")]),
            Response([dict(odno="123", pdno="AAPL", nccs_qty="1")])]
    adapter = broker(tmp_path, Trader(rows))
    assert asyncio.run(adapter.reconcile(intent(), "123", "20260925"))["status"] == "PARTIAL"
    adapter = broker(tmp_path, Trader([rows[0], Response([])]))
    assert asyncio.run(adapter.reconcile(intent(), "123", "20260925"))["status"] == "UNKNOWN"


def test_all_pages_and_continuation_failure_are_authoritative(tmp_path):
    trader = Trader([Response([], output="output1", continuation="M", keys=("a", "b")),
                     Response([dict(ovrs_pdno="AAPL", ovrs_cblc_qty="2")], output="output1"), Response([])])
    adapter = broker(tmp_path, trader)
    snapshot = asyncio.run(adapter.holdings("AAPL"))
    assert snapshot["quantity"] == 2 and snapshot["source_ref"]
    assert trader.calls[1][1]["request_cont"] == "N"
    assert trader.calls[1][0][2]["CTX_AREA_FK200"] == "a"
    adapter = broker(tmp_path, Trader([Response([], output="output1", continuation="M")]))
    assert asyncio.run(adapter.holdings("AAPL"))["status"] == "UNKNOWN"


def test_no_order_lookup_result_is_unknown_not_flat_filled(tmp_path):
    adapter = broker(tmp_path, Trader([Response([])]))
    assert asyncio.run(adapter.reconcile(intent(), "123", "20260925"))["status"] == "UNKNOWN"


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_actual_execution_service_claims_same_store_reservation(tmp_path, side):
    trader = Trader()
    adapter = broker(tmp_path, trader)
    order = intent(side)
    with sqlite3.connect(adapter.store.db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _, reservation = adapter.store.reserve_in_transaction(connection, order)

    async def run():
        async with adapter:
            return await adapter.submit(order, reservation, quote_validator=lambda p: True)
    result = asyncio.run(run())
    assert result["intent_status"] == "SUBMITTED"
    assert trader.calls[0]["regular_session_only"] is True
    assert trader.calls[0]["limit_price"] == 100
    if side == "BUY":
        assert trader.calls[0]["exact_quantity"] == 2 and trader.calls[0]["strict_budget"]
    else:
        assert trader.calls[0]["quantity"] == 2


def test_account_mismatch_and_closed_market_never_submit(tmp_path):
    trader = Trader()
    adapter = broker(tmp_path, trader)
    with pytest.raises(ValueError, match="SCOPE"):
        asyncio.run(adapter.submit(intent(account="other"), None, quote_validator=lambda p: True))
    trader.open = False
    with pytest.raises(ValueError, match="SESSION"):
        asyncio.run(adapter.submit(intent(), None, quote_validator=lambda p: True))
    assert not trader.calls


def test_us_factory_retains_originating_store_and_rejects_different_database(tmp_path, monkeypatch):
    import sys
    import types
    module = types.ModuleType("trading.us_stock_trading")
    module.AsyncUSTradingContext = lambda account_name: Trader()
    monkeypatch.setitem(sys.modules, "trading.us_stock_trading", module)
    store = IntentStore(tmp_path / "orders.db")
    service = ExecutionService.us(account_name="primary", intent_store=store)
    assert service._intent_store is store
    with pytest.raises(ValueError, match="originating"):
        ExecutionService.us(account_name="primary", intent_store=store, db_path=tmp_path / "other.db")


def test_cancel_requires_exact_pending_before_request(tmp_path):
    trader = Trader([Response([fill(ft_ccld_qty="0", nccs_qty="2", ft_ccld_amt3="0")]),
                     Response([dict(odno="123", pdno="AAPL", nccs_qty="2")])])
    adapter = broker(tmp_path, trader)
    ack = asyncio.run(adapter.cancel(intent(), "123", "20260925"))
    assert ack["success"] and trader.calls[-1]["quantity"] == 2
    # Cancellation acknowledgement is not returned as confirmed CANCELLED.
    assert "status" not in ack


def test_date_discovery_returns_only_exact_broker_row_date(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    date = datetime.now(ZoneInfo("America/New_York")).strftime("%Y%m%d")
    adapter = broker(tmp_path, Trader([Response([fill(ord_dt=date)])]))
    assert asyncio.run(adapter.discover_order_date(intent(), "123")) == date
    adapter = broker(tmp_path, Trader([Response([])]))
    assert asyncio.run(adapter.discover_order_date(intent(), "123")) is None
    adapter = broker(tmp_path, Trader([Response([fill(ord_dt=date), fill(ord_dt=date)])]))
    assert asyncio.run(adapter.discover_order_date(intent(), "123")) is None


@pytest.fixture
def real_us_module(monkeypatch):
    import builtins
    import importlib.util
    import io
    from pathlib import Path
    import sys
    import types
    original_open = builtins.open
    def opened(path, *args, **kwargs):
        if str(path).endswith("kis_devlp.yaml"):
            return io.StringIO("default_mode: demo\nauto_trading: true\n")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", opened)
    monkeypatch.setitem(sys.modules, "kis_auth", types.ModuleType("kis_auth"))
    spec = importlib.util.spec_from_file_location("oneil_test_real_us", Path(__file__).resolve().parents[1] / "prism-us/trading/us_stock_trading.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("side,opened,rejected", [("BUY", True, False), ("BUY", False, False),
    ("SELL", True, False), ("SELL", False, False), ("BUY", True, True), ("SELL", True, True)])
def test_real_us_module_never_queues_adaptive_orders(real_us_module, monkeypatch, side, opened, rejected):
    from unittest.mock import AsyncMock, MagicMock
    module = real_us_module
    trader = module.USStockTrading.__new__(module.USStockTrading)
    trader.buy_amount = 1000
    trader.auto_trading = True
    trader.mode = "demo"
    trader.trenv = SimpleNamespace(my_acct="fake", my_prod="01")
    trader._get_stock_lock = AsyncMock(return_value=asyncio.Lock())
    trader._semaphore = asyncio.Semaphore(1)
    trader._global_lock = asyncio.Lock()
    trader.get_current_price = MagicMock(return_value={"current_price": 100})
    trader.get_portfolio = MagicMock(return_value=[dict(ticker="AAPL", quantity=2)])
    trader.get_holding_quantity = MagicMock(return_value=2)
    trader.is_market_open = MagicMock(return_value=opened)
    trader.smart_buy = MagicMock(side_effect=AssertionError("reserved/queue route forbidden"))
    trader.smart_sell_all = MagicMock(side_effect=AssertionError("reserved/queue route forbidden"))
    trader._request = MagicMock(return_value=Response({"ODNO": "123"}))
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    def validate(price):
        if rejected:
            raise ValueError("known preflight rejection")
        return True
    if side == "BUY":
        result = asyncio.run(trader.async_buy_stock("AAPL", 200, "NASD", limit_price=100,
            strict_budget=True, exact_quantity=2, regular_session_only=True, quote_validator=validate))
    else:
        result = asyncio.run(trader.async_sell_stock("AAPL", "NASD", limit_price=100,
            quantity=2, regular_session_only=True, quote_validator=validate))
    assert result["success"] is (opened and not rejected)
    assert trader._request.call_count == int(opened and not rejected)
    if rejected:
        assert not result.get("outcome_unknown")
    if opened and not rejected:
        assert trader._request.call_args.args[2]["ORD_QTY"] == "2"
    trader.smart_buy.assert_not_called()
    trader.smart_sell_all.assert_not_called()

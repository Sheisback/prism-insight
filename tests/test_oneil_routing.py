import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

import prism_core.oneil_routing as routing
from prism_core.oneil_execution import OneilExecution
from test_oneil_current_capture import arguments


def config(tmp_path, mode="LIVE"):
    return dict(mode=mode, accounts=["primary"], live_db=str(tmp_path / "live.sqlite"))


@pytest.mark.parametrize("mode", ["OFF", "SHADOW"])
def test_nonlive_routes_do_not_touch_strategy_or_broker(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(routing, "load", lambda **kw: config(tmp_path, mode))
    result = asyncio.run(routing.route_initial(None, account={"name": "primary"}, ticker="TEST",
        company_name="Test", scenario={}, current_price=100, report_path="unused"))
    assert not result["handled"]
    assert not (tmp_path / "live.sqlite").exists()


def test_invalid_live_configuration_never_falls_through(monkeypatch):
    def bad():
        raise routing.ConfigurationError("bad")
    monkeypatch.setattr(routing, "load", bad)
    result = asyncio.run(routing.route_initial(None, account={"name": "primary"}, ticker="TEST",
        company_name="Test", scenario={}, current_price=100, report_path="unused"))
    assert result["handled"] and not result["strategy_recorded"]


def setup_route(tmp_path, monkeypatch, *, input_error=False):
    args = arguments()
    monkeypatch.setattr(routing, "load", lambda **kw: config(tmp_path))
    monkeypatch.setattr(routing, "require_live_approval", lambda *a, **kw: None)
    monkeypatch.setattr(routing, "_now", lambda: args["now"])
    import prism_core.oneil_batch_setup as setup
    import observability.scenario_shadow as capture
    monkeypatch.setattr(setup, "load_review_sidecar", lambda *a, **kw: {})
    monkeypatch.setattr(capture, "_adaptive_setup", lambda *a, **kw: dict(status="OK", plan=args["plan"]))
    import prism_core.oneil_runtime_inputs as inputs
    monkeypatch.setattr(inputs, "fetch_quote", lambda ticker: dict(symbol=ticker, currency="USD", exchange="NYQ"))
    events = []
    class Agent:
        max_slots = 10
        def __init__(self):
            import sqlite3
            self.conn = sqlite3.connect(":memory:")
            self.conn.row_factory = sqlite3.Row
            self.cursor = self.conn.cursor()
            self.conn.execute("CREATE TABLE us_stock_holdings(id INTEGER PRIMARY KEY, ticker, account_key, scenario)")

        async def _is_ticker_in_holdings(self, ticker):
            return False

        async def _buy_stock_with_position(self, *values, **kwargs):
            events.append(("strategy", values))
            self.conn.execute("INSERT INTO us_stock_holdings VALUES (1,?,?,?)", (values[0], "account", json.dumps(values[3])))
            self.conn.commit()
            return SimpleNamespace(success=True, legacy_holding_id=1)
    class Broker:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def holdings(self, ticker):
            return dict(status="OK", account_id="account", symbol=ticker, quantity=0,
                        open_orders_status="OK", open_orders_count=0, observed_at=args["now"], source_ref="broker:flat")
    async def envelope(agent, campaign):
        if input_error:
            raise ValueError("stale")
        from prism_core.oneil_current_capture import capture_current_record
        values = deepcopy(args)
        values["position_id"] = campaign["position_id"]
        for key in ("quote", "gates", "stop"):
            values[key]["position_id"] = campaign["position_id"]
        return capture_current_record(**values)
    import prism_core.oneil_dispatcher as dispatcher
    async def dispatched(execution, reserved, **kwargs):
        assert kwargs["authorize_add"]() is True
        state = execution.snapshot(reserved["campaign"]["campaign_id"])
        assert state["strategy_position_id"] == "legacy:US:1"
        events.append(("broker", reserved["intent"].quantity))
    monkeypatch.setattr(dispatcher, "dispatch_reserved", dispatched)
    kwargs = dict(account=dict(name="primary", account_key="account", buy_amount_usd=10000),
        ticker="TEST", company_name="Test", scenario=dict(_decision_id="d1", stop_loss=95),
        current_price=100, report_path="report.pdf", broker_factory=Broker, envelope_provider=envelope)
    return Agent(), kwargs, events


def test_qualified_first_order_materializes_strategy_before_dispatch(tmp_path, monkeypatch):
    agent, kwargs, events = setup_route(tmp_path, monkeypatch)
    result = asyncio.run(routing.route_initial(agent, **kwargs))
    assert result["handled"] and result["strategy_recorded"], result
    assert [e[0] for e in events] == ["strategy", "broker"]
    assert events[1][1] * 104 <= 5000
    marker = events[0][1][3]["_oneil_execution"]
    assert marker["campaign_id"] == result["campaign_id"]
    retry = asyncio.run(routing.route_initial(agent, **kwargs))
    assert retry["reason"] == "OWNED_CAMPAIGN_WORKER_MANAGED" and len(events) == 2


def test_unavailable_initial_stays_claimed_without_strategy_or_full_buy(tmp_path, monkeypatch):
    agent, kwargs, events = setup_route(tmp_path, monkeypatch, input_error=True)
    monkeypatch.setattr(OneilExecution, "attach_context", lambda *a: (_ for _ in ()).throw(AssertionError("context must be atomic")))
    result = asyncio.run(routing.route_initial(agent, **kwargs))
    assert result["handled"] and not result["strategy_recorded"] and not events
    execution = OneilExecution(config(tmp_path)["live_db"], mode="LIVE")
    campaign = execution.owner_by_symbol("account", "TEST")
    assert campaign["confirmed_quantity"] == 0
    assert campaign["context"]["scenario"]["_decision_id"] == "d1"
    assert campaign["context"]["exchange"] == "NYSE"


def test_owned_exit_latched_and_marked_before_legacy_strategy_finalization(tmp_path, monkeypatch):
    agent, kwargs, _ = setup_route(tmp_path, monkeypatch, input_error=True)
    result = asyncio.run(routing.route_initial(agent, **kwargs))
    stock = dict(ticker="TEST", account_key="account", current_price=99, scenario=json.dumps(dict(
        _oneil_execution=dict(campaign_id=result["campaign_id"]))))
    assert routing.route_exit(agent, stock, "original_stop")
    assert stock["_oneil_owned_exit"]
    execution = OneilExecution(config(tmp_path)["live_db"], mode="LIVE")
    assert execution.snapshot(result["campaign_id"])["status"] in {"EXIT_PENDING", "CLOSED"}


@pytest.mark.parametrize("status,price,explicit", [("EXIT_PENDING", 104, False),
    ("ACTIVE", 99, False), ("ACTIVE", 104, True)])
def test_add_source_failures_do_not_erase_exit_protection(tmp_path, monkeypatch, status, price, explicit):
    import sqlite3
    from prism_core.oneil_adaptive_policy import _time
    args = arguments()
    monkeypatch.setattr(routing, "_now", lambda: args["now"])
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE us_stock_holdings(id,ticker,scenario,stop_loss,account_key)")
    agent = SimpleNamespace(cursor=connection.cursor(), max_slots=10, MAX_SAME_SECTOR=3,
                            SECTOR_CONCENTRATION_RATIO=.3)
    campaign = dict(plan=args["plan"], context=dict(scenario={}, exchange="NYSE"),
                    position_id="owned:1", campaign_id="campaign:1", account_id="account",
                    strategy_position_id="legacy:US:1", current_stop=100, mode="LIVE", status=status)
    calls = []
    def unavailable(*args):
        calls.append(args)
        raise ValueError("provider unavailable")
    def quote(symbol):
        return dict(symbol=symbol, currency="USD", regularMarketPrice=price,
                    regularMarketTime=_time(args["now"]).timestamp())
    result = asyncio.run(routing.collect_initial_envelope(agent, campaign, market_provider=unavailable,
        intraday_provider=unavailable, quote_provider=quote, source="mechanical", protection_only=explicit))
    assert result["status"] == "MISSING" and result["tick"] is None
    assert result["protection"]["quote"]["price"] == str(price)
    assert result["protection"]["stop"]["current_stop"] == 100
    assert calls == [] and "PROTECTION_ONLY" in result["reason_codes"]


def test_initial_evaluation_clock_advances_after_broker_lookup(tmp_path, monkeypatch):
    agent, kwargs, events = setup_route(tmp_path, monkeypatch)
    clock = [arguments()["now"]]
    monkeypatch.setattr(routing, "_now", lambda: clock[0])
    original_envelope = kwargs["envelope_provider"]
    async def envelope(*args):
        result = await original_envelope(*args)
        clock[0] = "2026-09-25T13:41:01Z"
        return result
    kwargs["envelope_provider"] = envelope
    original = OneilExecution.evaluate
    clocks = []
    def evaluated(self, *args, **kw):
        clocks.append(kw["evaluation_at"])
        return original(self, *args, **kw)
    monkeypatch.setattr(OneilExecution, "evaluate", evaluated)
    result = asyncio.run(routing.route_initial(agent, **kwargs))
    assert result["strategy_recorded"] and events
    assert clocks == ["2026-09-25T13:41:01Z"]


def test_materialization_recovers_exact_committed_row_after_link_crash(tmp_path, monkeypatch):
    from prism_core.order_intents import OrderIntent
    agent, kwargs, events = setup_route(tmp_path, monkeypatch)
    original = OneilExecution.link_strategy_position
    monkeypatch.setattr(OneilExecution, "link_strategy_position", lambda *a: (_ for _ in ()).throw(RuntimeError("crash")))
    result = asyncio.run(routing.route_initial(agent, **kwargs))
    assert result["handled"] and len(events) == 1
    monkeypatch.setattr(OneilExecution, "link_strategy_position", original)
    execution = OneilExecution(config(tmp_path)["live_db"], mode="LIVE")
    state = execution.snapshot(result["campaign_id"])
    reserved = dict(campaign=state, intent=OrderIntent(**state["orders"][0]["intent"]))
    async def recover_twice():
        return await asyncio.gather(routing.materialize_initial(agent, execution, reserved),
                                    routing.materialize_initial(agent, execution, reserved))
    recovered = asyncio.run(recover_twice())
    assert all(s["strategy_position_id"] == "legacy:US:1" for s in recovered)
    assert len(events) == 1
    assert agent.conn.execute("SELECT COUNT(*) FROM us_stock_holdings").fetchone()[0] == 1


def test_closed_execution_marker_still_owns_strategy_and_finalizes_once(tmp_path, monkeypatch):
    agent, kwargs, _ = setup_route(tmp_path, monkeypatch)
    result = asyncio.run(routing.route_initial(agent, **kwargs))
    execution = OneilExecution(config(tmp_path)["live_db"], mode="LIVE")
    cid = result["campaign_id"]
    # Execution can be flat/closed independently of its strategy ledger outcome.
    with execution._transaction() as db:
        state = execution._get(db, cid)
        state["status"] = "CLOSED"
        execution._save(db, state)
    monkeypatch.setattr(routing, "load", lambda **kw: config(tmp_path, "OFF"))
    assert asyncio.run(routing.route_owned_entry(agent, kwargs["account"], "TEST"))
    stock = dict(agent.conn.execute("SELECT * FROM us_stock_holdings").fetchone())
    stock["current_price"] = 99
    assert routing.route_exit(agent, stock, "strategy_stop") and stock["_oneil_owned_exit"]
    calls = []
    async def sell_stock(stock, reason, exit_kind=None):
        assert routing.route_exit(agent, stock, reason)
        calls.append((stock["id"], reason))
        agent.conn.execute("DELETE FROM us_stock_holdings WHERE id=?", (stock["id"],))
        agent.conn.commit()
        routing.mark_owned_strategy_exit(stock)
        return True
    agent.sell_stock = sell_stock
    assert asyncio.run(routing.finalize_owned_strategy(agent, execution, cid, 99, "strategy_stop"))
    assert not asyncio.run(routing.finalize_owned_strategy(agent, execution, cid, 99, "strategy_stop"))
    assert calls == [(1, "strategy_stop")]
    final = execution.snapshot(cid)
    assert final["strategy_exit_recorded"]["source_ref"] == final["strategy_exit_reference"]["source_ref"]


def test_slow_add_sources_reread_changed_portfolio_and_stop_before_final_quote(monkeypatch):
    import sqlite3
    from prism_core.oneil_adaptive_policy import _time
    args = arguments()
    clock = [args["now"]]
    monkeypatch.setattr(routing, "_now", lambda: clock[0])
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE us_stock_holdings(id,ticker,scenario,stop_loss,account_key)")
    saved = dict(_decision_id="d1", _oneil_execution=dict(campaign_id="campaign:1"))
    connection.execute("INSERT INTO us_stock_holdings VALUES (1,'TEST',?,100,'account')", (json.dumps(saved),))
    agent = SimpleNamespace(cursor=connection.cursor(), max_slots=10, MAX_SAME_SECTOR=3,
                            SECTOR_CONCENTRATION_RATIO=.3)
    campaign = dict(plan=args["plan"], context=dict(scenario=dict(decision="entry", buy_score=8,
        target_price=130, stop_loss=100, max_portfolio_size=1), exchange="NYSE"),
        position_id="owned:1", campaign_id="campaign:1", account_id="account",
        strategy_position_id="legacy:US:1", current_stop=100, mode="LIVE", status="ACTIVE")
    def warmup(*_):
        clock[0] = "2026-09-25T13:43:31Z"
        connection.execute("UPDATE us_stock_holdings SET stop_loss=105 WHERE id=1")
        connection.execute("INSERT INTO us_stock_holdings VALUES (2,'OTHER','{}',1,'account')")
        connection.commit()
        return args["intraday_input"]
    quote_calls = []
    def quote(symbol):
        quote_calls.append(clock[0])
        return dict(symbol=symbol, currency="USD", regularMarketPrice=104,
                    regularMarketTime=_time(clock[0]).timestamp())
    def market():
        return dict(observed_at=clock[0], source_ref="market:fresh", regime="strong_bull",
                    market_pulse="UPTREND", pilot_reexposure_active=False)
    result = asyncio.run(routing.collect_initial_envelope(agent, campaign, market_provider=market,
        intraday_provider=warmup, quote_provider=quote))
    assert result["status"] == "OK", result["reason_codes"]
    assert result["tick"]["evidence"]["gates"]["slot"] is False
    assert result["protection"]["stop"]["current_stop"] == 105
    assert quote_calls == [args["now"], "2026-09-25T13:43:31Z"]

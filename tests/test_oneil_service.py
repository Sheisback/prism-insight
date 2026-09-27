"""Operational service integration: actual policy/journal, no network or orders."""
import asyncio
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from observability.scenario_shadow import _connect, _json
from observability.trading_context import execution_profile_ref
from prism_core.oneil_capture_tape import OneilCaptureTape
from prism_core.oneil_config import validate
from prism_core.oneil_execution import OneilExecution
from prism_core.oneil_service import OneilService
from test_oneil_shadow_runner import setup


def service_fixture(tmp_path, monkeypatch):
    kwargs, capture, args, _ = setup(tmp_path)
    account = dict(account_key="demo", name="primary", buy_amount_usd=10000)
    capture["attributes"]["execution_profile_ref"] = execution_profile_ref(account["account_key"])
    capture["attributes"]["initial_underwriting"] = dict(
        decision="entry", buy_score=8, target_price=130, stop_loss=100,
        max_portfolio_size=10, _decision_id=capture["decision_id"])
    # A new immutable registry fixture rather than editing a frozen original.
    registry = tmp_path / "service-capture.sqlite"
    with closing(_connect(registry)) as connection, connection:
        connection.execute("INSERT INTO captures VALUES (?,?)", ("new", _json(capture)))
    tape_path = tmp_path / "service-tape.sqlite"
    tape = OneilCaptureTape(tape_path)
    tape.ingest_capture(capture)
    config = validate(dict(mode="SHADOW", accounts=["primary"], capture_since="2026-09-25T13:00:00Z",
        capture_db=str(registry), holdings_db=str(kwargs["holdings_db"]), tape_db=str(tape_path),
        shadow_db=str(tmp_path / "execution.sqlite"), live_db=str(tmp_path / "live.sqlite"),
        runtime_db=str(tmp_path / "paired.sqlite")))
    import prism_core.oneil_routing as routing
    monkeypatch.setattr(routing, "_now", lambda: args["now"])
    quote = dict(symbol="TEST", currency="USD", exchange="NMS", regularMarketPrice=104,
                 regularMarketTime=1790343660)
    market = dict(observed_at=args["now"], source_ref="market", regime="strong_bull",
                  market_pulse="UPTREND", pilot_reexposure_active=False)
    class Intraday:
        def __call__(self, *a):
            return deepcopy(args["intraday_input"])
    service = OneilService(config, [account], clock=lambda: args["now"],
        quote_provider=lambda *a: deepcopy(quote), market_provider=lambda: deepcopy(market),
        intraday_factory=Intraday)
    return service, config, capture, args, tape


def test_shadow_uses_owned_whole_share_journal_without_broker_or_source_writes(tmp_path, monkeypatch):
    service, config, capture, _, _ = service_fixture(tmp_path, monkeypatch)
    from prism_core.execution_service import ExecutionService
    monkeypatch.setattr(ExecutionService, "us", lambda **kw: pytest.fail("SHADOW cannot construct broker"))
    before = {key: Path(config[key]).read_bytes() for key in ("holdings_db", "capture_db", "tape_db")}
    result = asyncio.run(service.once(session_open=True))
    assert result["capture"]["loaded"] == 1, result
    assert result["rows"][0]["status"] == "SHADOW_SIMULATED", result
    assert result["rows"][0]["virtual"] is True
    assert result["rows"][0]["quantity"] * 104 <= 5000
    assert all(Path(config[k]).read_bytes() == value for k, value in before.items())
    second = asyncio.run(service.once(session_open=True))
    execution = OneilExecution(config["shadow_db"])
    state = execution.snapshot(second["rows"][0]["campaign_id"])
    assert len(state["orders"]) == 1
    assert state["strategy_position_id"] == capture["position_id"]


def test_no_market_session_performs_no_network_or_claim(tmp_path, monkeypatch):
    service, config, _, _, _ = service_fixture(tmp_path, monkeypatch)
    service.quote_provider = lambda *a: pytest.fail("outside-session provider")
    result = asyncio.run(service.once(session_open=False))
    assert result["status"] == "OUTSIDE_REGULAR_SESSION" and not result["rows"]
    assert not Path(config["shadow_db"]).exists()


def test_pending_original_exit_closes_shadow_not_legacy_broker(tmp_path, monkeypatch):
    service, config, capture, args, tape = service_fixture(tmp_path, monkeypatch)
    asyncio.run(service.once(session_open=True))
    cid = tape.campaign_id_for_position(capture["position_id"])
    # Fixture clock moves after an authoritative original strategy exit.
    args["now"] = "2026-09-25T13:42:00Z"
    tape.append_exit(cid, tape.bind_event_identity(cid, dict(
        occurred_at=args["now"], available_at=args["now"], price="104", source_ref="original-exit")))
    result = asyncio.run(service.once(session_open=True))
    assert result["rows"][0]["owned_status"] == "CLOSED", result
    assert result["rows"][0]["quantity"] == 0
    assert not Path(config["live_db"]).exists()


def test_metadata_failure_is_unavailable_not_fabricated_exchange(tmp_path, monkeypatch):
    service, _, _, _, _ = service_fixture(tmp_path, monkeypatch)
    service.quote_provider = lambda *a: {}
    result = asyncio.run(service.once(session_open=True))
    assert result["capture"]["unavailable"] == 1
    assert not result["rows"]
    assert "primary" not in json.dumps(result)


def test_worker_exit_finalizes_exact_strategy_row_and_checkpoint_once(tmp_path, monkeypatch):
    service, config, capture, args, _ = service_fixture(tmp_path, monkeypatch)
    execution = OneilExecution(config["live_db"], mode="LIVE")
    state = execution.claim_campaign(account_id="demo", position_id=capture["position_id"],
        plan=args["plan"], unit_budget=10000, now=args["now"], account_snapshot=dict(
            status="OK", quantity=0, account_id="demo", symbol="TEST", observed_at=args["now"],
            source_ref="flat", open_orders_count=0, open_orders_status="OK"))
    cid = state["campaign_id"]
    execution.link_strategy_position(cid, capture["position_id"])
    execution.request_exit(cid, reason="PROTECTIVE_STOP", reference=dict(at=args["now"], price="99", source_ref="stop"))
    calls = []

    async def agent_factory(account):
        connection = sqlite3.connect(config["holdings_db"])
        connection.row_factory = sqlite3.Row
        cursor = connection.cursor()
        async def sell(stock, reason, **kwargs):
            calls.append((stock["current_price"], reason))
            connection.execute("DELETE FROM us_stock_holdings WHERE id=?", (stock["id"],))
            connection.commit()
            return True
        return SimpleNamespace(conn=connection, cursor=cursor, sell_stock=sell)

    with closing(sqlite3.connect(config["holdings_db"])) as connection, connection:
        scenario = dict(capture["attributes"]["initial_underwriting"],
                        _oneil_execution={"campaign_id": cid})
        connection.execute("UPDATE us_stock_holdings SET scenario=? WHERE id=1", (json.dumps(scenario),))
    service.live_agent_factory = agent_factory
    asyncio.run(service._finalize_strategy(execution, execution.snapshot(cid)))
    asyncio.run(service._finalize_strategy(execution, execution.snapshot(cid)))
    assert calls == [(99.0, "PROTECTIVE_STOP")]
    assert execution.snapshot(cid)["strategy_exit_recorded"]["source_ref"] == "stop"


def test_slow_collection_yields_to_protection_without_cancelling_orders(tmp_path, monkeypatch):
    service, _, _, _, _ = service_fixture(tmp_path, monkeypatch)
    calls = []
    original = asyncio.wait
    async def wait(tasks, *, timeout):
        if not calls:
            return set(), tasks
        return await original(tasks, timeout=timeout)
    async def protect(execution, rows):
        calls.append("protection")
        await asyncio.sleep(0)
    async def collect():
        await asyncio.sleep(0)
        return "collected"
    monkeypatch.setattr(asyncio, "wait", wait)
    monkeypatch.setattr(service, "_protect_all", protect)
    assert asyncio.run(service._with_protection(collect(), object(), [])) == "collected"
    assert calls

"""Real read-only source -> current gates -> policy -> atomic ledger integration."""
from contextlib import closing
from copy import deepcopy
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from observability.scenario_shadow import _connect, _json
from prism_core.oneil_capture_tape import OneilCaptureTape
from prism_core.oneil_shadow_runner import ShadowRunner, read_portfolio
from prism_core.strategy_ledger import LedgerError
from test_oneil_runtime import fixture
from test_oneil_runtime_inputs import fixtures


def setup(tmp_path):
    _, _, _, capture = fixture(tmp_path)
    capture["position_id"] = "legacy:US:1"
    args, response, gate_args = fixtures()
    response["exchange"] = "NMS"
    scenario = dict(gate_args["scenario"], _decision_id=capture["decision_id"])
    registry, holdings = tmp_path / "capture.sqlite", tmp_path / "holdings.sqlite"
    with closing(_connect(registry)) as connection, connection:
        connection.execute("INSERT INTO captures VALUES (?,?)", ("original", _json(capture)))
    with closing(sqlite3.connect(holdings)) as connection, connection:
        connection.execute("CREATE TABLE us_stock_holdings (id INTEGER PRIMARY KEY,ticker TEXT,account_key TEXT,scenario TEXT,stop_loss REAL)")
        connection.execute("INSERT INTO us_stock_holdings VALUES (1,'TEST','demo',?,100)", (json.dumps(scenario),))
    calls = []

    class Intraday:
        def __call__(self, symbol, at, calendar):
            calls.append("intraday")
            return deepcopy(args["intraday_input"])

    def quote(symbol):
        calls.append("quote")
        return deepcopy(response)

    kwargs = dict(runtime_db=tmp_path / "shadow.sqlite", capture_db=registry,
                  holdings_db=holdings, since="2026-09-25T13:00:00Z", max_slots=10,
                  quote_provider=quote, market_provider=lambda: deepcopy(gate_args["market"]),
                  intraday_factory=Intraday, clock=lambda: args["now"])
    return kwargs, capture, args, calls


def test_default_source_chain_add_readonly_repeat_and_direct_mark_guard(tmp_path):
    kwargs, capture, _, calls = setup(tmp_path)
    before = {key: Path(kwargs[key]).read_bytes() for key in ("capture_db", "holdings_db")}
    runner = ShadowRunner(**kwargs)
    result = runner.once()
    assert result["rows"][0]["status"] == "RECORDED"
    assert result["rows"][0]["decision"]["action"] == "ADD"
    assert result["initial_arm"] == "INITIAL_POLICY_50"
    assert Decimal(result["rows"][0]["decision"]["target_allocation"]) == Decimal(".5")
    assert calls == ["quote", "intraday", "quote"]  # Fresh quote AFTER slow acquisition.
    assert runner.once() == result  # Same completed cycle never recollects/re-executes.
    assert calls == ["quote", "intraday", "quote"]
    assert all(Path(kwargs[k]).read_bytes() == value for k, value in before.items())
    cid = runner.runtime.campaign_id_for_position(capture["position_id"])
    with pytest.raises(LedgerError, match="direct mark"):
        runner.runtime.ledger.mark("bypass", cid + ":adaptive", 999, "2027-01-01T00:00:00Z")
    regular = runner.once(source="regular")
    assert regular["rows"][0]["decision"]["action"] == "WAIT"
    assert result["broker_execution"] is False and result["live_ready"] is False


def test_source_failure_durable_missing_and_protection_not_disabled(tmp_path):
    kwargs, _, _, _ = setup(tmp_path)
    kwargs["intraday_factory"] = lambda: lambda *a: (_ for _ in ()).throw(RuntimeError("secret"))
    kwargs["quote_provider"] = lambda symbol: {
        "symbol": symbol, "currency": "USD", "regularMarketTime": 1790343660, "regularMarketPrice": 90}
    runner = ShadowRunner(**kwargs)
    result = runner.once(calendar="NASDAQ")
    row = result["rows"][0]
    assert row["status"] == "RECORDED"
    assert row["missing_observations"] == 1
    assert row["decision"]["action"] == "PROTECTIVE_EXIT_REQUIRED"
    assert "secret" not in json.dumps(result)


def test_unavailable_exit_tape_blocks_add_not_protection(tmp_path):
    kwargs, _, args, _ = setup(tmp_path)
    kwargs["tape_db"] = tmp_path / "missing-tape.sqlite"
    kwargs["quote_provider"] = lambda symbol: {
        "symbol": symbol, "currency": "USD", "regularMarketTime": 1790343660, "regularMarketPrice": 90}
    result = ShadowRunner(**kwargs).once(calendar="NASDAQ")
    assert result["rows"][0]["decision"]["action"] == "PROTECTIVE_EXIT_REQUIRED"
    assert result["rows"][0]["missing_observations"] == 1


def test_current_holding_must_exactly_match_original_decision(tmp_path):
    kwargs, capture, _, _ = setup(tmp_path)
    with closing(sqlite3.connect(kwargs["holdings_db"])) as connection, connection:
        connection.execute("UPDATE us_stock_holdings SET scenario=?", (json.dumps({"_decision_id": "other"}),))
    with pytest.raises(ValueError, match="decision mismatch"):
        read_portfolio(kwargs["holdings_db"], capture, 10)
    result = ShadowRunner(**kwargs).once()
    assert result["rows"][0]["missing_observations"] == 1


def test_original_terminal_from_tape_needs_no_network(tmp_path):
    kwargs, capture, args, _ = setup(tmp_path)
    path = tmp_path / "tape.sqlite"
    tape = OneilCaptureTape(path)
    cid = tape.ingest_capture(capture)["campaign_id"]
    tape.append_exit(cid, tape.bind_event_identity(cid, dict(
        occurred_at="2026-09-25T13:40:00Z", available_at="2026-09-25T13:40:00Z",
        price="99", source_ref="committed-exit")))
    kwargs["tape_db"] = path
    kwargs["quote_provider"] = lambda *a: pytest.fail("terminal cannot fetch quotes")
    runner = ShadowRunner(**kwargs)
    result = runner.once()
    assert result["rows"][0]["decision"]["action"] == "ORIGINAL_EXIT"
    assert result["rows"][0]["decision"]["price"] == "99"
    runtime_id = runner.runtime.campaign_id_for_position(capture["position_id"])
    assert runner.runtime.snapshot(runtime_id)["state"]["entry_status"] == "NO_ENTRY"
    with pytest.raises(LedgerError, match="closed campaign"):
        with runner.runtime.ledger._transaction() as db:
            runner.runtime._target(db, "illegal-reopen", runtime_id, "adaptive", args["plan"],
                                   50, "104", args["now"])


def test_interrupted_cycle_and_immutable_config_visible(tmp_path):
    kwargs, _, _, _ = setup(tmp_path)
    runner = ShadowRunner(**kwargs)
    with runner.runtime.ledger._transaction() as db:
        runner.runtime.ledger._event(db, "oneil:cycle:interrupted", dict(kind="runner_cycle"))
    assert runner.once()["prior_incomplete_cycle"] is True
    with pytest.raises(LedgerError, match="conflict"):
        ShadowRunner(**{**kwargs, "since": "2026-09-25T13:01:00Z"})


def test_cli_live_mode_rejected_before_creating_any_database(tmp_path):
    target = tmp_path / "uncreated.sqlite"
    script = str(Path(__file__).resolve().parents[1] / "tools/run_oneil_shadow.py")
    result = subprocess.run([sys.executable, script, "--mode", "LIVE", "--runtime-db", str(target)],
                            capture_output=True, text=True)
    assert result.returncode != 0 and not target.exists()


def test_runtime_cannot_use_any_source_database(tmp_path):
    kwargs, _, _, _ = setup(tmp_path)
    kwargs["runtime_db"] = kwargs["holdings_db"]
    with pytest.raises(ValueError, match="separate"):
        ShadowRunner(**kwargs)


def test_runtime_hardlink_to_source_rejected_before_initialization(tmp_path):
    kwargs, _, _, _ = setup(tmp_path)
    alias = tmp_path / "alias.sqlite"
    original = Path(kwargs["holdings_db"]).read_bytes()
    os.link(kwargs["holdings_db"], alias)
    kwargs["runtime_db"] = alias
    with pytest.raises(ValueError, match="separate"):
        ShadowRunner(**kwargs)
    assert Path(kwargs["holdings_db"]).read_bytes() == original


def test_missing_registry_does_not_forge_empty_success_and_leaves_cycle_gap(tmp_path):
    kwargs, _, _, _ = setup(tmp_path)
    kwargs["capture_db"] = tmp_path / "absent.sqlite"
    runner = ShadowRunner(**kwargs)
    with pytest.raises(ValueError):
        runner.once()
    with runner.runtime.ledger._transaction() as db:
        done = db.execute("SELECT COUNT(*) FROM events WHERE id LIKE 'oneil:cycle:%:done'").fetchone()[0]
    assert done == 0

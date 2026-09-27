import hashlib
import json
import sqlite3
from zoneinfo import ZoneInfo

import pytest

from tools import build_history_scenario_inventory as inventory


@pytest.fixture
def source(tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    path = folder / "history.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE trading_history (sell_date TEXT, profit_rate, scenario TEXT, secret TEXT)")
    return path


def insert(source, rows):
    with sqlite3.connect(source) as connection:
        connection.executemany("INSERT INTO trading_history VALUES (?, ?, ?, ?)",
                               [(*row, "SECRET_CANARY") for row in rows])


def test_aggregate_snapshot_and_no_source_mutation_or_secrets(source):
    insert(source, [
        ("2026-01-01 09:00:00", 10, json.dumps({"stop_loss": 90, "_scenario_contract_version": "buy-scenario-v1", "ticker": "SECRET_CANARY"})),
        ("2026-01-02T00:00:00Z", -5, json.dumps({"stop_loss": True, "_scenario_contract_version": "SECRET_CANARY"})),
        ("bad", 0, "{}"),
        (None, "bad", None),
    ])
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    result = inventory.build(source, "KR", ZoneInfo("Asia/Seoul"))
    assert result["counts"]["records"] == 4
    assert [result["counts"][key] for key in ("return_positive", "return_negative", "return_zero", "return_invalid_or_missing")] == [1, 1, 1, 1]
    assert result["counts"]["stop_loss_positive_numeric"] == 1
    assert result["counts"]["contract_version_known"] == 1
    assert result["counts"]["contract_version_unknown"] == 2
    assert result["recorded_sell_period_utc"] == {"earliest": "2026-01-01T00:00:00Z", "latest": "2026-01-02T00:00:00Z"}
    assert result["counts"]["sell_date_naive_assumed_timezone"] == 1
    assert result["counts"]["sell_date_explicit_offset"] == 1
    assert "SECRET_CANARY" not in json.dumps(result)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert result["scenario_basis"] == "CURRENT_STORED_SNAPSHOT_NOT_PROVEN_ORIGINAL_AT_ENTRY"


@pytest.mark.parametrize("scenario,category", [
    (" ", "scenario_absent"), ("{bad", "scenario_invalid_json"),
    ('{"stop_loss":NaN}', "scenario_invalid_json"),
    ('{"stop_loss":Infinity}', "scenario_invalid_json"),
    ("[]", "scenario_non_object"), ("null", "scenario_non_object"),
    ("true", "scenario_non_object"),
])
def test_malformed_scenarios(source, scenario, category):
    insert(source, [(None, float("inf"), scenario)])
    result = inventory.build(source, "KR", ZoneInfo("UTC"))
    assert result["counts"][category] == 1
    assert result["counts"]["return_invalid_or_missing"] == 1


@pytest.mark.parametrize("stop", [True, False, 0, -1, "100", None, [], {}, float("inf"), 10 ** 400])
def test_invalid_stop_numbers(source, stop):
    # Overflow exponent is valid JSON but decoded infinity must remain invalid.
    encoded = '{"stop_loss":1e999}' if isinstance(stop, float) else json.dumps({"stop_loss": stop})
    insert(source, [(None, 0, encoded)])
    counts = inventory.build(source, "KR", ZoneInfo("UTC"))["counts"]
    assert counts["stop_loss_invalid_or_missing"] == 1
    assert counts["stop_loss_positive_numeric"] == 0


def test_read_only_authorizer(source):
    connection = inventory.read_only_connection(source)
    try:
        for query in ("DELETE FROM trading_history", "SELECT secret FROM trading_history", "PRAGMA query_only=OFF", "PRAGMA table_info(secret)"):
            with pytest.raises(sqlite3.DatabaseError):
                connection.execute(query).fetchall()
    finally:
        connection.close()


def test_missing_table_and_columns(source):
    assert inventory.build(source, "US", ZoneInfo("UTC"))["source_status"] == "SOURCE_TABLE_MISSING"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE us_trading_history (sell_date TEXT)")
    assert inventory.build(source, "US", ZoneInfo("UTC"))["source_status"] == "REQUIRED_COLUMNS_MISSING"


def test_bounds_abort_without_partial_packet(source, monkeypatch):
    insert(source, [(None, 0, None), (None, 0, None)])
    monkeypatch.setattr(inventory, "ROW_LIMIT", 1)
    with pytest.raises(ValueError, match="row_limit"):
        inventory.build(source, "KR", ZoneInfo("UTC"))
    monkeypatch.setattr(inventory, "TIME_LIMIT_SECONDS", -1)
    with pytest.raises((ValueError, sqlite3.OperationalError)):
        inventory.build(source, "KR", ZoneInfo("UTC"))


def test_cli_exclusive_output_and_privacy(source, tmp_path, capsys):
    insert(source, [(None, 1, '{"memo":"SECRET_CANARY"}')])
    output = tmp_path / "inventory.json"
    args = ["--db", str(source), "--market", "KR", "--naive-source-timezone", "Asia/Seoul", "--output", str(output)]
    assert inventory.main(args) == 0
    assert output.stat().st_mode & 0o777 == 0o600
    original = output.read_bytes()
    with pytest.raises(SystemExit) as error:
        inventory.main(args)
    assert error.value.code == 2
    assert output.read_bytes() == original
    assert "SECRET_CANARY" not in original.decode() + capsys.readouterr().err


def test_output_paths(source, tmp_path):
    for path in (source.parent / "inventory.json", tmp_path / ".." / "escape.json", tmp_path / "no-parent" / "output.json"):
        with pytest.raises(ValueError):
            inventory.validate_output(source, path)
    link = tmp_path / "link.json"
    link.symlink_to(tmp_path / "nonexistent.json")
    with pytest.raises(ValueError):
        inventory.validate_output(source, link)
    parent = tmp_path / "linked"
    parent.symlink_to(source.parent, target_is_directory=True)
    with pytest.raises(ValueError):
        inventory.validate_output(source, parent / "output.json")


def test_invalid_parameters(source):
    with pytest.raises(ValueError):
        inventory.build(source, "SECRET_CANARY", ZoneInfo("UTC"))
    with pytest.raises(ValueError):
        inventory.build(source, "KR", None)

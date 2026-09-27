"""Original-plan capture is opt-in, immutable, sanitized, and fail-open."""
from copy import deepcopy
import json
import sqlite3

import pytest

from observability import scenario_shadow as capture


@pytest.fixture
def setup_capture(tmp_path, monkeypatch):
    path = tmp_path / "capture.sqlite"
    spool = tmp_path / "events.jsonl"
    monkeypatch.setenv("SCENARIO_SHADOW_CAPTURE_ENABLED", "true")
    monkeypatch.setenv("SCENARIO_SHADOW_CAPTURE_DB", str(path))
    monkeypatch.setenv("PRISM_OBSERVABILITY_SPOOL", str(spool))
    monkeypatch.setenv("PRISM_GIT_SHA", "test-capture")
    kwargs = dict(market="US", ticker="TEST", decision_id="decision-1", position_id="position-1",
                  scenario={"stop_loss": 90, "rationale": "private rationale",
                            "account": "private-account", "_decision_context": {
                                "gate_allowed": True, "cooldown_blocked": False,
                                "sector_diverse": True, "rebound_pilot": False}},
                  current_price=100, entry_eligible=True, is_add=False)
    return kwargs, path, spool


def test_disabled_zero_io(setup_capture, monkeypatch):
    args, path, _ = setup_capture
    monkeypatch.delenv("SCENARIO_SHADOW_CAPTURE_ENABLED")
    monkeypatch.setattr(capture, "_connect", lambda _: pytest.fail("unexpected I/O"))
    assert capture.emit_initial_capture(**args) is None
    assert not path.exists()


@pytest.mark.parametrize("change", [
    {"market": "KR"}, {"entry_eligible": False}, {"is_add": True},
    {"decision_id": None}, {"position_id": ""}, {"current_price": float("nan")},
])
def test_invalid_source_zero_io(setup_capture, change):
    args, path, _ = setup_capture
    args.update(change)
    assert capture.emit_initial_capture(**args) is None
    assert not path.exists()


@pytest.mark.parametrize("change", [
    {"stop_loss": None}, {"stop_loss": 100}, {"stop_loss": -1},
    {"_decision_context": {}}, {"_strategy_policy": {"mode": "split"}},
    {"_strategy_projection": {}}, {"regime_entry_policy": {"mode": "rebound_pilot"}},
    {"unknown": object()}, {"nested": {1: "not JSON keys"}},
])
def test_invalid_scenario_zero_io(setup_capture, change):
    args, path, _ = setup_capture
    args["scenario"].update(change)
    assert capture.emit_initial_capture(**args) is None
    assert not path.exists()


@pytest.mark.parametrize("key,value", [("gate_allowed", False), ("cooldown_blocked", True),
                                      ("sector_diverse", False), ("rebound_pilot", True)])
def test_explicit_gates_required(setup_capture, key, value):
    args, path, _ = setup_capture
    args["scenario"]["_decision_context"][key] = value
    assert capture.emit_initial_capture(**args) is None
    assert not path.exists()
    del args["scenario"]["_decision_context"][key]
    assert capture.emit_initial_capture(**args) is None
    assert not path.exists()


def test_real_spool_preserves_plan_and_no_input_mutation(setup_capture):
    args, path, spool = setup_capture
    original = deepcopy(args)
    event = capture.emit_initial_capture(**args)
    assert args == original
    assert event["decision_id"] == "decision-1"
    assert event["position_id"] == "position-1"
    assert json.loads(spool.read_text()) == event
    attrs = event["attributes"]
    assert attrs["plan"]["entry_price"] == "100"
    assert attrs["plan"]["initial_stop"] == "90"
    assert len(attrs["plan"]["stages"]) == 3
    assert attrs["execution_provenance"] == "NOT_REQUESTED"
    assert "private" not in spool.read_text()
    assert "[REDACTED]" not in json.dumps(attrs)
    assert path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(path) as db:
        text = db.execute("SELECT payload FROM captures").fetchone()[0]
        assert "private" not in text
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE captures SET payload='{}'")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM captures")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT OR REPLACE INTO captures SELECT capture_key, '{}' FROM captures")


def test_delivered_immutable_and_positions_separate(setup_capture):
    args, path, spool = setup_capture
    first = capture.emit_initial_capture(**args)
    args["scenario"]["stop_loss"] = 80
    assert capture.emit_initial_capture(**args) is None
    args["position_id"] = "position-2"
    second = capture.emit_initial_capture(**args)
    assert first["event_id"] != second["event_id"]
    assert first["decision_id"] == second["decision_id"]
    assert len(spool.read_text().splitlines()) == 2
    with sqlite3.connect(path) as db:
        stored = [json.loads(row[0]) for row in db.execute("SELECT payload FROM captures")]
        assert stored[0]["attributes"]["plan"]["initial_stop"] == "90"


def test_retry_uses_original_payload_and_clock(setup_capture, monkeypatch):
    args, _, _ = setup_capture
    attempts = []
    def emit(*_, **kwargs):
        attempts.append(kwargs)
        return None if len(attempts) == 1 else kwargs
    monkeypatch.setattr(capture, "emit_event", emit)
    assert capture.emit_initial_capture(**args) is None
    args["scenario"]["stop_loss"] = 75
    result = capture.emit_initial_capture(**args)
    assert result == attempts[0]
    assert result["attributes"]["plan"]["initial_stop"] == "90"
    assert capture.emit_initial_capture(**args) is None
    assert len(attempts) == 2


@pytest.mark.parametrize("field,value", [("decision_id", "other"), ("ticker", "OTHER")])
def test_same_position_identity_conflict(setup_capture, monkeypatch, field, value):
    args, _, _ = setup_capture
    monkeypatch.setattr(capture, "emit_event", lambda *a, **kw: None)
    capture.emit_initial_capture(**args)
    monkeypatch.setattr(capture, "emit_event", lambda *a, **kw: pytest.fail("conflict emitted"))
    args[field] = value
    assert capture.emit_initial_capture(**args) is None


@pytest.mark.parametrize("kind", ["malformed", "foreign", "empty", "protected"])
def test_wrong_database_untouched(setup_capture, monkeypatch, kind):
    args, path, _ = setup_capture
    if kind == "protected":
        path = path.with_name("stock_tracking.db")
        monkeypatch.setenv("SCENARIO_SHADOW_CAPTURE_DB", str(path))
    if kind == "foreign":
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE user_data (value TEXT)")
    else:
        path.write_bytes(b"not a database" if kind == "malformed" else b"")
    original = path.read_bytes()
    assert capture.emit_initial_capture(**args) is None
    assert path.read_bytes() == original


def test_io_error_fail_open(setup_capture, monkeypatch):
    args, _, _ = setup_capture
    def broken(*a, **kw):
        raise OSError("unavailable")
    monkeypatch.setattr(capture, "emit_event", broken)
    assert capture.emit_initial_capture(**args) is None
    monkeypatch.setattr(capture, "_connect", broken)
    assert capture.emit_initial_capture(**args) is None


def test_crash_checkpoint_retry_same_event_id(setup_capture):
    args, path, spool = setup_capture
    first = capture.emit_initial_capture(**args)
    # Model a successful append followed by a process crash before checkpoint.
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM delivered")
    second = capture.emit_initial_capture(**args)
    assert first["event_id"] == second["event_id"]
    assert first["timestamp"] == second["timestamp"]
    assert first["attributes"] == second["attributes"]
    assert len(spool.read_text().splitlines()) == 2


@pytest.mark.parametrize("name", ["stock_tracking_db.sqlite", "us_stock_tracking.sqlite"])
def test_production_name_never_created(setup_capture, monkeypatch, name):
    args, path, _ = setup_capture
    path = path.parent / "must-not-create" / name
    monkeypatch.setenv("SCENARIO_SHADOW_CAPTURE_DB", str(path))
    assert capture.emit_initial_capture(**args) is None
    assert not path.parent.exists()


def test_trigger_text_not_exported(setup_capture):
    args, _, spool = setup_capture
    args.update(trigger_type="private-account-canary", trigger_mode="private-report-canary")
    event = capture.emit_initial_capture(**args)
    assert event is not None
    assert "canary" not in spool.read_text()
    assert len(event["attributes"]["trigger_type_hash"]) == 64
    assert "trigger_mode" not in event["attributes"]


def test_busy_database_fail_open(setup_capture):
    args, path, _ = setup_capture
    capture.emit_initial_capture(**args)
    args["position_id"] = "new-position"
    with sqlite3.connect(path) as blocker:
        blocker.execute("BEGIN EXCLUSIVE")
        assert capture.emit_initial_capture(**args) is None


def test_atomic_creation_failure_does_not_publish_empty_database(setup_capture, monkeypatch):
    args, path, _ = setup_capture
    def fail(*a, **kw):
        raise OSError("publication failed")
    monkeypatch.setattr(capture.os, "link", fail)
    assert capture.emit_initial_capture(**args) is None
    assert not path.exists()
    assert not list(path.parent.glob(".scenario-capture-*"))


def test_concurrent_calls_only_one_delivery(setup_capture):
    from concurrent.futures import ThreadPoolExecutor
    args, path, spool = setup_capture
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(lambda _: capture.emit_initial_capture(**args), range(4)))
    assert sum(item is not None for item in outcomes) == 1
    assert len(spool.read_text().splitlines()) == 1
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 1

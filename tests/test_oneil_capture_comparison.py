"""Real producer/adapter/tape/ledger/CLI chain with isolated synthetic sources."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

from prism_core.oneil_capture_tape import OneilCaptureTape
from prism_core.scenario_shadow_policy import create_plan
from test_oneil_current_capture import arguments
from tools.run_oneil_capture_comparison import compare_tape, ingest


def operations():
    args = arguments()
    plan = args["plan"]
    initial = dict(
        market="US", ticker=plan["symbol"], position_id=args["position_id"],
        decision_id=plan["source_decision_ref"], event_id="test-original-capture",
        event_time=plan["created_at"], attributes=dict(
            capture_schema_version=1, phase="POST_STRATEGY_COMMIT_PRE_BROKER",
            confirmed_fill=False, trading_impact="none",
            plan=create_plan(entry_price=plan["entry_reference"], initial_stop=plan["initial_stop"],
                             entry_at=plan["created_at"], source_decision_ref=plan["source_decision_ref"],
                             entry_eligible=True),
            adaptive_setup=dict(status="OK", plan=plan)))
    return args, initial


def test_adapter_tape_replay_and_retry_same_comparison(tmp_path):
    args, initial = operations()
    tape = OneilCaptureTape(tmp_path / "research.sqlite")
    cid = tape.ingest_capture(initial)["campaign_id"]
    exit_event = dict(
        symbol=args["plan"]["symbol"], source_decision_ref=args["plan"]["source_decision_ref"],
        price_basis_ref=args["plan"]["setup"]["price_basis_ref"], source_ref="test-terminal",
        occurred_at="2026-09-25T19:59:00Z", available_at="2026-09-25T19:59:00Z", price="120")
    batch = dict(contract="oneil-capture-import-v1", operations=[
        dict(kind="INITIAL", record=initial),
        dict(kind="CURRENT", campaign_id=cid, inputs=args),
        dict(kind="EXIT", campaign_id=cid, record=exit_event),
    ])
    ingest(tape, batch)
    result = compare_tape(tape)
    ingest(tape, batch)
    assert compare_tape(OneilCaptureTape(tape.path)) == result
    assert result["capture_coverage"]["closed"] == 1
    assert result["comparison"]["coverage"]["evaluated"] == 1
    decision = result["comparison"]["results"][0]["cost_cases"]["10"]["decisions"][0]
    assert decision["action"] == "ADD"
    assert result["performance_validated"] is False
    assert result["capture_completeness"] == "UNKNOWN_BEST_EFFORT"

    output = tmp_path / "comparison.json"
    command = [sys.executable, str(Path(__file__).resolve().parents[1] / "tools/run_oneil_capture_comparison.py"),
               "--db", str(tape.path), "--output", str(output)]
    run = subprocess.run(command, capture_output=True, text=True, check=True)
    assert json.loads(run.stdout) == json.loads(output.read_text()) == result
    assert subprocess.run(command, capture_output=True).returncode != 0


def test_missing_observation_invalidates_even_otherwise_complete_campaign(tmp_path):
    args, initial = operations()
    tape = OneilCaptureTape(tmp_path / "research.sqlite")
    cid = tape.ingest_capture(initial)["campaign_id"]
    valid = dict(kind="CURRENT", campaign_id=cid, inputs=args)
    missing = deepcopy(valid)
    missing["inputs"].update(now="2026-09-25T13:42:00Z", gates=None)
    ingest(tape, dict(contract="oneil-capture-import-v1", operations=[valid, missing]))
    terminal = tape.bind_event_identity(cid, dict(
        source_ref="terminal", occurred_at="2026-09-25T19:59:00Z",
        available_at="2026-09-25T19:59:00Z", price="120"))
    tape.append_exit(cid, terminal)
    result = compare_tape(tape)
    assert result["capture_coverage"]["capture_gaps"] == 1
    assert result["comparison"]["coverage"]["evaluated"] == 0
    assert result["comparison"]["cost_cases"]["10"]["adaptive"] is None


def test_cli_does_not_create_db_for_comparison_or_leak_input_errors(tmp_path):
    script = str(Path(__file__).resolve().parents[1] / "tools/run_oneil_capture_comparison.py")
    database = tmp_path / "missing.sqlite"
    result = subprocess.run([sys.executable, script, "--db", str(database)], capture_output=True)
    assert result.returncode != 0 and not database.exists()
    source = tmp_path / "bad.json"
    source.write_text(json.dumps({"contract": "secret-private-payload"}))
    result = subprocess.run([sys.executable, script, "--db", str(database), "--input", str(source)],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "secret-private-payload" not in result.stdout + result.stderr


def test_current_terminal_survives_missing_add_inputs_and_takes_priority(tmp_path):
    args, initial = operations()
    tape = OneilCaptureTape(tmp_path / "research.sqlite")
    cid = tape.ingest_capture(initial)["campaign_id"]
    ingest(tape, dict(contract="oneil-capture-import-v1", operations=[
        dict(kind="CURRENT", campaign_id=cid, inputs=args)]))
    args = deepcopy(args)
    args["now"] = "2026-09-25T19:59:00Z"
    args["exit_event"] = dict(args["quote"], price="120",
                             occurred_at=args["now"], available_at=args["now"])
    args.update(gates=None, intraday_input=None)
    ingest(tape, dict(contract="oneil-capture-import-v1", operations=[
        dict(kind="CURRENT", campaign_id=cid, inputs=args)]))
    result = compare_tape(tape)
    assert result["capture_coverage"]["closed"] == 1
    assert result["capture_coverage"]["capture_gaps"] == 0
    assert result["comparison"]["coverage"]["evaluated"] == 1
    assert len(result["comparison"]["results"][0]["cost_cases"]["10"]["decisions"]) == 1

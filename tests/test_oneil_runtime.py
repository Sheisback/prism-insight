from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
from datetime import timedelta

import pytest

from prism_core.oneil_adaptive_policy import _hash
from prism_core.oneil_current_capture import capture_current_record
from prism_core.oneil_runtime import OneilRuntime
from prism_core.scenario_shadow_policy import create_plan
from prism_core.strategy_ledger import LedgerError
from test_oneil_current_capture import arguments


def fixture(tmp_path, initial_arm="COMPATIBILITY_SCOUT_10"):
    args = arguments()
    p = args["plan"]
    original = create_plan(entry_price=p["entry_reference"], initial_stop=p["initial_stop"],
                           entry_at=p["created_at"], source_decision_ref=p["source_decision_ref"],
                           entry_eligible=True)
    capture = dict(market="US", ticker=p["symbol"], position_id=args["position_id"],
                   decision_id=p["source_decision_ref"], event_id="capture",
                   event_time=p["created_at"], attributes=dict(capture_schema_version=1,
                   phase="POST_STRATEGY_COMMIT_PRE_BROKER", confirmed_fill=False,
                   trading_impact="none", plan=original, adaptive_setup=dict(status="OK", plan=p)))
    runtime = OneilRuntime(tmp_path / "runtime.sqlite", initial_arm=initial_arm)
    opened = runtime.open_capture(capture)
    return runtime, opened["campaign_id"], args, capture


def test_actual_policy_accounting_restart_intent(tmp_path):
    runtime, cid, args, capture = fixture(tmp_path)
    before = runtime.snapshot(cid)
    assert before["arms"]["baseline"]["target_pct"] == "100"
    assert before["arms"]["adaptive"]["target_pct"] == "10"
    assert runtime.open_capture(capture) == before
    record = capture_current_record(**args)
    result = runtime.advance(cid, record, expected_revision=0)
    assert result["decision"]["action"] == "ADD"
    assert result["arms"]["adaptive"]["target_pct"] == "100.0000"
    assert result["intent"]["broker_quantity"] is None
    assert not result["intent"]["execution_authorized"]
    restarted = OneilRuntime(runtime.ledger.path, initial_arm=runtime.initial_arm)
    assert restarted.snapshot(cid) == runtime.snapshot(cid)
    assert restarted.intent(result["intent"]["intent_id"]) == result["intent"]
    assert not restarted.advance(cid, record, expected_revision=0)["event_applied"]


def test_regular_mechanical_same_bar_never_double_add(tmp_path):
    runtime, cid, args, _ = fixture(tmp_path)
    runtime.advance(cid, capture_current_record(**args), expected_revision=0)
    args["source"] = "regular"
    second = runtime.advance(cid, capture_current_record(**args), expected_revision=1)
    assert second["decision"]["action"] == "WAIT"
    assert second["state"]["last_add_bar_end"] == "2026-09-25T13:40:00+00:00"


@pytest.mark.parametrize("missing", ["quote", "gates", "intraday_input"])
def test_missing_is_durable_no_add(tmp_path, missing):
    runtime, cid, args, _ = fixture(tmp_path)
    args[missing] = None
    result = runtime.advance(cid, capture_current_record(**args), expected_revision=0)
    assert result["decision"]["action"] == "WAIT"
    assert result["state"]["missing_observations"] == 1
    assert OneilRuntime(runtime.ledger.path, initial_arm=runtime.initial_arm).snapshot(cid)["state"]["latest_input_status"] == "MISSING"


def test_negative_gate_and_stale_quote(tmp_path):
    runtime, cid, args, _ = fixture(tmp_path)
    args["gates"]["risk"] = False
    result = runtime.advance(cid, capture_current_record(**args), expected_revision=0)
    assert result["decision"]["reason"] == "ADD_GATE_NOT_MET"
    args["quote"]["observed_at"] = "2026-09-25T13:30:00Z"
    result = runtime.advance(cid, capture_current_record(**args), expected_revision=1)
    assert result["state"]["latest_input_status"] == "MISSING"


def test_protection_without_add_data_and_regressive_stop(tmp_path):
    runtime, cid, args, _ = fixture(tmp_path)
    args["gates"] = None
    runtime.advance(cid, capture_current_record(**args), expected_revision=0)
    args["quote"]["price"] = "99"
    args["stop"]["current_stop"] = "96"
    result = runtime.advance(cid, capture_current_record(**args), expected_revision=1)
    assert result["state"]["current_stop"] == "100"
    assert result["decision"]["action"] == "PROTECTIVE_EXIT_REQUIRED"
    assert result["state"]["closed"]
    for arm in result["arms"].values():
        assert Decimal(arm["normalized_units"]) == 0
        assert arm["mark_price"] == "99"


def test_original_terminal_without_add_data_exits_same_price(tmp_path):
    runtime, cid, args, _ = fixture(tmp_path)
    args["gates"] = None
    args["exit_event"] = dict(args["quote"], available_at=args["now"], occurred_at=args["now"])
    result = runtime.advance(cid, capture_current_record(**args), expected_revision=0)
    assert result["decision"]["action"] == "ORIGINAL_EXIT"
    assert all(a["mark_price"] == "104" for a in result["arms"].values())


@pytest.mark.parametrize("field,value", [("position_id", "other"), ("plan_hash", "x"),
                                        ("record_hash", "x")])
def test_identity_hash_rejection_is_atomic(tmp_path, field, value):
    runtime, cid, args, _ = fixture(tmp_path)
    before = runtime.snapshot(cid)
    record = capture_current_record(**args)
    record[field] = value
    if field != "record_hash":
        record["record_hash"] = _hash({k: v for k, v in record.items() if k != "record_hash"})
    with pytest.raises(LedgerError):
        runtime.advance(cid, record, expected_revision=0)
    assert runtime.snapshot(cid) == before


def test_cas_concurrent_distinct_observations(tmp_path):
    runtime, cid, args, _ = fixture(tmp_path)
    first = capture_current_record(**args)
    args["source"] = "regular"
    second = capture_current_record(**args)
    def run(record):
        try:
            return OneilRuntime(runtime.ledger.path, initial_arm=runtime.initial_arm).advance(cid, record, expected_revision=0)["event_applied"]
        except LedgerError:
            return False
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(run, (first, second))) == [False, True]
    assert runtime.snapshot(cid)["revision"] == 1


def test_immutable_capture_and_unknown_campaign(tmp_path):
    runtime, cid, args, capture = fixture(tmp_path)
    modified = deepcopy(capture)
    modified["event_id"] = "other"
    with pytest.raises(LedgerError):
        runtime.open_capture(modified)
    with pytest.raises(LedgerError):
        runtime.advance("unknown", capture_current_record(**args), expected_revision=0)


def test_invalid_revision_and_tick_quote_binding(tmp_path):
    runtime, cid, args, _ = fixture(tmp_path)
    record = capture_current_record(**args)
    for revision in (True, -1, 4):
        with pytest.raises(LedgerError):
            runtime.advance(cid, record, expected_revision=revision)
    record["tick"]["evidence"]["quote"]["price"] = "103"
    record["record_hash"] = _hash({k: v for k, v in record.items() if k != "record_hash"})
    with pytest.raises(LedgerError):
        runtime.advance(cid, record, expected_revision=0)
    assert runtime.snapshot(cid)["revision"] == 0


def test_regressive_stop_blocks_add_but_preserves_protection(tmp_path):
    runtime, cid, args, _ = fixture(tmp_path)
    args["gates"]["risk"] = False
    runtime.advance(cid, capture_current_record(**args), expected_revision=0)
    args["gates"]["risk"] = True
    args["stop"]["current_stop"] = "99"
    result = runtime.advance(cid, capture_current_record(**args), expected_revision=1)
    assert result["decision"]["action"] == "WAIT"
    assert result["state"]["latest_input_status"] == "MISSING"
    assert result["state"]["current_stop"] == "100"


def test_committed_terminal_precedes_later_protective_quote(tmp_path):
    runtime, cid, args, _ = fixture(tmp_path)
    args["exit_event"] = dict(args["quote"], price="103", occurred_at="2026-09-25T13:40:00Z",
                              available_at="2026-09-25T13:40:00Z")
    args["quote"]["price"] = "94"
    args["gates"] = None
    result = runtime.advance(cid, capture_current_record(**args), expected_revision=0)
    assert result["decision"]["action"] == "ORIGINAL_EXIT"
    assert all(a["mark_price"] == "103" for a in result["arms"].values())
    assert all(a["mark_at"] == "2026-09-25T13:40:00+00:00" for a in result["arms"].values())


def test_accounting_failure_rolls_back_observation_and_decision(tmp_path, monkeypatch):
    runtime, cid, args, _ = fixture(tmp_path)
    before = runtime.snapshot(cid)
    actual = runtime._target
    def fail(*arguments, **kwargs):
        actual(*arguments, **kwargs)
        raise RuntimeError("injected accounting failure")
    monkeypatch.setattr(runtime, "_target", fail)
    record = capture_current_record(**args)
    with pytest.raises(RuntimeError):
        runtime.advance(cid, record, expected_revision=0)
    assert runtime.snapshot(cid) == before
    monkeypatch.setattr(runtime, "_target", actual)
    assert runtime.advance(cid, record, expected_revision=0)["event_applied"]


def test_owned_books_reject_direct_accounting_mutation(tmp_path):
    runtime, cid, _, _ = fixture(tmp_path)
    with pytest.raises(LedgerError):
        runtime.ledger.apply_target("bypass", cid + ":adaptive", cid + ":adaptive", "TEST", 80,
                                    104, "2026-09-25T13:41:00Z")
    with pytest.raises(LedgerError):
        runtime.ledger.sell("bypass", cid + ":adaptive", 104, "2026-09-25T13:41:00Z")


def test_deferred_terminal_after_missing_poll_uses_accounting_clock(tmp_path):
    runtime, cid, args, _ = fixture(tmp_path)
    args["gates"] = None
    poll = runtime.advance(cid, capture_current_record(**args), expected_revision=0)
    assert poll["state"]["missing_observations"] == 1
    args["now"] = "2026-09-25T13:42:00Z"
    args["exit_event"] = dict(args["quote"], price="103", occurred_at="2026-09-25T13:40:00Z",
                              available_at="2026-09-25T13:40:00Z")
    result = runtime.advance(cid, capture_current_record(**args), expected_revision=1)
    assert result["state"]["closed"]
    assert result["state"]["missing_observations"] == 1
    assert result["state"]["latest_input_status"] == "TERMINAL"
    assert all(a["mark_at"] == "2026-09-25T13:40:00+00:00" for a in result["arms"].values())
    assert all(a["mark_price"] == "103" for a in result["arms"].values())


def test_deferred_terminal_conflicts_with_already_accounted_add(tmp_path):
    runtime, cid, args, _ = fixture(tmp_path)
    runtime.advance(cid, capture_current_record(**args), expected_revision=0)
    before = runtime.snapshot(cid)
    args["now"] = "2026-09-25T13:42:00Z"
    args["gates"] = None
    args["exit_event"] = dict(args["quote"], price="103", occurred_at="2026-09-25T13:40:00Z",
                              available_at="2026-09-25T13:40:00Z")
    with pytest.raises(LedgerError, match="TERMINAL_ACCOUNTING_CHRONOLOGY_CONFLICT"):
        runtime.advance(cid, capture_current_record(**args), expected_revision=1)
    assert runtime.snapshot(cid) == before


def test_terminal_missing_add_data_is_not_itself_capture_gap(tmp_path):
    runtime, cid, args, _ = fixture(tmp_path)
    args["gates"] = None
    args["exit_event"] = dict(args["quote"], occurred_at=args["now"], available_at=args["now"])
    result = runtime.advance(cid, capture_current_record(**args), expected_revision=0)
    assert result["state"]["missing_observations"] == 0
    assert result["state"]["latest_input_status"] == "TERMINAL"


def test_initial_policy_fifty_is_pending_until_current_evidence(tmp_path):
    runtime, cid, args, _ = fixture(tmp_path, "INITIAL_POLICY_50")
    opened = runtime.snapshot(cid)
    assert opened["adaptive_arm"] == "INITIAL_POLICY_50"
    assert opened["arms"]["adaptive"]["target_pct"] == "0"
    assert opened["state"]["entry_status"] == "PENDING"
    missing = dict(args, gates=None)
    runtime.advance(cid, capture_current_record(**missing), expected_revision=0)
    result = runtime.advance(cid, capture_current_record(**args), expected_revision=1)
    assert result["decision"]["action"] == "ADD"
    assert Decimal(result["arms"]["adaptive"]["target_pct"]) == 50
    assert result["arms"]["adaptive"]["mark_price"] == "104"
    assert result["arms"]["adaptive"]["mark_at"] == "2026-09-25T13:41:00+00:00"
    assert result["arms"]["baseline"]["mark_price"] == "100"


def test_initial_fifty_can_advance_at_next_profitable_bar(tmp_path):
    from prism_core.oneil_adaptive_policy import _time
    runtime, cid, args, _ = fixture(tmp_path, "INITIAL_POLICY_50")
    record = capture_current_record(**args)
    runtime.advance(cid, record, expected_revision=0)
    next_record = deepcopy(record)
    now = "2026-09-25T13:46:00+00:00"
    next_record["occurred_at"] = now
    tick = next_record["tick"]
    tick.update(occurred_at=now, available_at=now)
    facts = tick["evidence"]
    for q in (next_record["protection"]["quote"], facts["quote"]):
        q.update(price="104.5", observed_at=now)
    facts["gates"]["observed_at"] = now
    for bar in facts["bars"]:
        for key in ("start_at", "end_at"):
            bar[key] = (_time(bar[key]) + timedelta(minutes=5)).isoformat()
        bar["close"] = "104.5"
    facts["volume"]["as_of"] = facts["bars"][-1]["end_at"]
    facts["volume"]["elapsed_minutes"] += 5
    for sample in facts["volume"]["samples"]:
        sample["elapsed_minutes"] += 5
    next_record["record_hash"] = _hash({k: v for k, v in next_record.items() if k != "record_hash"})
    result = runtime.advance(cid, next_record, expected_revision=1)
    assert result["decision"]["action"] == "ADD"
    assert Decimal(result["arms"]["adaptive"]["target_pct"]) == 100


@pytest.mark.parametrize("terminal", [True, False])
def test_pending_zero_ends_no_entry_without_fake_sell(tmp_path, terminal):
    runtime, cid, args, _ = fixture(tmp_path, "INITIAL_POLICY_50")
    args["gates"] = None
    if terminal:
        args["exit_event"] = dict(args["quote"], occurred_at=args["now"], available_at=args["now"])
    else:
        args["quote"]["price"] = "94"
    result = runtime.advance(cid, capture_current_record(**args), expected_revision=0)
    assert result["state"]["closed"] and result["state"]["entry_status"] == "NO_ENTRY"
    assert Decimal(result["arms"]["adaptive"]["realized_contribution"]) == 0
    with runtime.ledger._transaction() as db:
        assert not db.execute("SELECT 1 FROM legs WHERE campaign_id=?", (cid + ":adaptive",)).fetchone()


def test_initial_arm_immutable_per_database(tmp_path):
    runtime, _, _, _ = fixture(tmp_path, "INITIAL_POLICY_50")
    with pytest.raises(LedgerError):
        OneilRuntime(runtime.ledger.path, initial_arm="COMPATIBILITY_SCOUT_10")
    assert OneilRuntime(runtime.ledger.path).initial_arm == "INITIAL_POLICY_50"

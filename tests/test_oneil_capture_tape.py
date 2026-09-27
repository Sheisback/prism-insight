"""Local append-only persistence and paired replay, no network or orders."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import sqlite3
import json

import pytest

from prism_core.oneil_capture_tape import OneilCaptureTape
from prism_core.oneil_paired_replay import digest, evaluate_replay
from prism_core.scenario_shadow_policy import create_plan
from test_oneil_paired_replay import payload


def capture():
    p = payload()["campaigns"][0]["plan"]
    original = create_plan(entry_price=p["entry_reference"], initial_stop=p["initial_stop"],
                           entry_at=p["created_at"], source_decision_ref=p["source_decision_ref"],
                           entry_eligible=True)
    return dict(market="US", ticker=p["symbol"], position_id="position-test",
                decision_id=p["source_decision_ref"], event_id="original-capture",
                event_time=p["created_at"], attributes=dict(capture_schema_version=1,
                phase="POST_STRATEGY_COMMIT_PRE_BROKER", confirmed_fill=False,
                trading_impact="none", plan=original,
                adaptive_setup=dict(status="OK", plan=p)))


def started(tmp_path):
    tape = OneilCaptureTape(tmp_path / "research.sqlite")
    cid = tape.ingest_capture(capture())["campaign_id"]
    return tape, cid, payload()["campaigns"][0]


def test_end_to_end_restart_determinism(tmp_path):
    tape, cid, c = started(tmp_path)
    assert tape.append_tick(cid, c["ticks"][0])["evidence_status"] == "OK"
    tape.append_exit(cid, c["exit"])
    exported = tape.export_replay()
    assert exported == OneilCaptureTape(tape.path).export_replay()
    assert exported["capture_coverage"] == dict(total=1, closed=1, open=0, missing_plan=0, missing_or_invalid_ticks=0, capture_gaps=0)
    result = evaluate_replay(exported)
    assert result["coverage"]["evaluated"] == 1
    assert result["validation_kind"] == "EXPLORATORY_ONLY"
    assert not result["performance_validated"]


def test_missing_evidence_never_becomes_wait_zero(tmp_path):
    tape, cid, c = started(tmp_path)
    c["ticks"][0]["evidence"] = {}
    assert tape.append_tick(cid, c["ticks"][0])["evidence_status"] == "MISSING_OR_INVALID"
    tape.append_exit(cid, c["exit"])
    exported = tape.export_replay()
    assert exported["capture_coverage"]["missing_or_invalid_ticks"] == 1
    result = evaluate_replay(exported)
    assert result["coverage"]["unavailable"] == 1
    assert result["cost_cases"]["10"]["adaptive"] is None


def test_no_ticks_open_and_missing_plan_are_separate(tmp_path):
    tape, cid, c = started(tmp_path)
    assert tape.export_replay()["capture_coverage"]["open"] == 1
    missing = capture()
    missing["position_id"] = "missing"
    missing["attributes"].pop("adaptive_setup")
    out = tape.ingest_capture(missing)
    assert out["evidence_status"] == "MISSING"
    tape.append_exit(cid, c["exit"])
    exported = tape.export_replay()
    assert exported["capture_coverage"]["missing_plan"] == 1
    assert evaluate_replay(exported)["coverage"]["unavailable"] == 1


def test_identical_concurrent_ingestion_and_ticks(tmp_path):
    path = tmp_path / "race.sqlite"
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: OneilCaptureTape(path, timeout=5).ingest_capture(capture()), range(16)))
    assert sum(x["status"] == "RECORDED" for x in results) == 1
    cid = results[0]["campaign_id"]
    tick = payload()["campaigns"][0]["ticks"][0]
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: OneilCaptureTape(path, timeout=5).append_tick(cid, tick), range(16)))
    assert sum(x["status"] == "RECORDED" for x in results) == 1
    assert len({x["event_id"] for x in results}) == 1


def test_conflicts_cannot_overwrite_and_closed_retry_is_idempotent(tmp_path):
    tape, cid, c = started(tmp_path)
    assert tape.ingest_capture(capture())["status"] == "DUPLICATE"
    changed = capture()
    changed["event_id"] = "other"
    with pytest.raises(ValueError, match="conflict"):
        tape.ingest_capture(changed)
    tape.append_tick(cid, c["ticks"][0])
    bad = deepcopy(c["ticks"][0])
    bad["evidence"]["quote"]["price"] = "103"
    with pytest.raises(ValueError, match="conflict"):
        tape.append_tick(cid, bad)
    tape.append_exit(cid, c["exit"])
    assert tape.append_tick(cid, c["ticks"][0])["status"] == "DUPLICATE"
    assert tape.append_exit(cid, c["exit"])["status"] == "DUPLICATE"
    bad["occurred_at"] = "2026-09-25T13:42:00Z"
    with pytest.raises(ValueError, match="closed"):
        tape.append_tick(cid, bad)


@pytest.mark.parametrize("key,value", [("symbol", "WRONG"), ("source_decision_ref", "WRONG"),
    ("price_basis_ref", "WRONG"), ("available_at", "2026-09-26T00:00:00Z"),
    ("stop_available_at", "2026-09-26T00:00:00Z"), ("current_stop", "89"),
    ("source_ref", ""), ("occurred_at", "2026-09-25T13:30:00Z")])
def test_reject_invalid_record_boundary(tmp_path, key, value):
    tape, cid, c = started(tmp_path)
    c["ticks"][0][key] = value
    with pytest.raises(ValueError):
        tape.append_tick(cid, c["ticks"][0])


def test_monotonic_stops_and_times(tmp_path):
    tape, cid, c = started(tmp_path)
    tick = c["ticks"][0]
    tape.append_tick(cid, tick)
    tick = dict(tick, occurred_at="2026-09-25T13:42:00Z", current_stop="99")
    with pytest.raises(ValueError, match="lowered"):
        tape.append_tick(cid, tick)
    tick.update(occurred_at="2026-09-25T13:40:00Z", available_at="2026-09-25T13:40:00Z")
    with pytest.raises(ValueError, match="nonmonotonic"):
        tape.append_tick(cid, tick)


def test_identity_binding_never_supplies_fresh_evidence(tmp_path):
    tape, cid, _ = started(tmp_path)
    assert cid == tape.campaign_id_for_position("position-test")
    bound = tape.bind_event_identity(cid, {})
    assert set(bound) == {"symbol", "source_decision_ref", "price_basis_ref"}
    with pytest.raises(ValueError, match="identity"):
        tape.bind_event_identity(cid, {"symbol": "OTHER"})


def test_owned_schema_no_mutations_or_existing_foreign_db(tmp_path):
    tape, cid, c = started(tmp_path)
    tape.append_tick(cid, c["ticks"][0])
    with sqlite3.connect(tape.path) as conn:
        for sql in ("DELETE FROM campaigns", "UPDATE records SET kind='EXIT'",
                    "INSERT OR REPLACE INTO campaigns SELECT * FROM campaigns"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                conn.execute(sql)
    foreign = tmp_path / "production.db"
    with sqlite3.connect(foreign) as conn:
        conn.execute("CREATE TABLE user_data (value TEXT)")
    before = foreign.read_bytes()
    with pytest.raises(ValueError):
        OneilCaptureTape(foreign)
    assert foreign.read_bytes() == before
    link = tmp_path / "linked.sqlite"
    link.symlink_to(tape.path)
    with pytest.raises(ValueError):
        OneilCaptureTape(link)


def test_bad_capture_binding_rejected(tmp_path):
    tape = OneilCaptureTape(tmp_path / "research.sqlite")
    c = capture()
    c["ticker"] = "OTHER"
    with pytest.raises(ValueError, match="identity"):
        tape.ingest_capture(c)


def test_rejected_records_leave_durable_gap(tmp_path):
    tape, cid, c = started(tmp_path)
    tape.append_tick(cid, c["ticks"][0])
    rejected = dict(c["ticks"][0], current_stop="89")
    with pytest.raises(ValueError):
        tape.append_tick(cid, rejected)
    tape.append_exit(cid, c["exit"])
    result = OneilCaptureTape(tape.path).export_replay()
    assert result["capture_coverage"]["capture_gaps"] == 1
    assert result["campaigns"][0]["capture_gaps"][0]["reason"] == "REJECTED_TICK"


def test_current_observation_connection_and_missing_gap(tmp_path):
    tape, cid, c = started(tmp_path)
    envelope = dict(c["ticks"][0], position_id="position-test", plan_hash=c["plan"]["plan_hash"],
                    status="OK", tick=dict(c["ticks"][0], position_id="position-test"),
                    contract_version="oneil-current-capture-v1")
    envelope["record_hash"] = digest(envelope)
    assert tape.append_observation(cid, envelope)["evidence_status"] == "OK"
    envelope = dict(envelope, status="MISSING", tick=None)
    envelope["record_hash"] = digest({k: v for k, v in envelope.items() if k != "record_hash"})
    assert tape.append_observation(cid, envelope)["evidence_status"] == "MISSING_OR_INVALID"
    tape.append_exit(cid, c["exit"])
    assert tape.export_replay()["capture_coverage"]["capture_gaps"] == 1


def test_non_utc_timestamp_orders_by_instant(tmp_path):
    tape, cid, c = started(tmp_path)
    tick = dict(c["ticks"][0], occurred_at="2026-09-25T22:41:00+09:00")
    tape.append_tick(cid, tick)
    assert tape.append_tick(cid, c["ticks"][0])["status"] == "DUPLICATE"
    tape.append_exit(cid, c["exit"])
    assert tape.export_replay()["campaigns"][0]["ticks"][0]["occurred_at"] == "2026-09-25T13:41:00+00:00"


def test_append_reads_only_latest_record(tmp_path, monkeypatch):
    tape, cid, c = started(tmp_path)
    tape.append_tick(cid, c["ticks"][0])
    statements = []
    connect = tape._connect

    def traced():
        conn = connect()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(tape, "_connect", traced)
    tape.append_exit(cid, c["exit"])
    history_reads = [sql for sql in statements if sql.startswith("SELECT kind,payload FROM records")]
    assert len(history_reads) == 1
    assert history_reads[0].endswith("ORDER BY occurred_at DESC LIMIT 1")
    with connect() as conn:
        query_plan = conn.execute("EXPLAIN QUERY PLAN SELECT kind,payload FROM records WHERE campaign_id=? ORDER BY occurred_at DESC LIMIT 1", (cid,)).fetchall()
    assert any("USING INDEX" in row[3] for row in query_plan)
    assert not any("TEMP B-TREE" in row[3] for row in query_plan)


def test_export_refuses_oversized_tape_before_decoding_payloads(tmp_path):
    tape, cid, _ = started(tmp_path)
    # Deliberately invalid JSON proves the bound is checked before any payload
    # decoding. Direct fixture inserts do not represent an accepted runtime API.
    with sqlite3.connect(tape.path) as conn:
        conn.executemany("INSERT INTO records VALUES (?,?,?,?,?)",
                         ((f"tick-{i}", cid, "TICK", f"time-{i:05}", "invalid-json")
                          for i in range(10001)))
    with pytest.raises(ValueError, match="10000 ticks") as error:
        tape.export_replay()
    assert not isinstance(error.value, json.JSONDecodeError)

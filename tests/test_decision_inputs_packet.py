import json
import sqlite3

from tools import build_decision_inputs_evidence_packet as P


def test_packet_joins_exact_decision_ids_and_buckets(tmp_path):
    db = tmp_path / "t.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE analysis_performance_tracker (decision_id TEXT, tracked_7d_return REAL, "
                 "tracked_14d_return REAL, tracked_30d_return REAL, was_traded INTEGER)")
    conn.execute("CREATE TABLE us_analysis_performance_tracker (decision_id TEXT, return_7d REAL, return_14d REAL, "
                 "return_30d REAL, was_traded INTEGER)")
    conn.execute("INSERT INTO analysis_performance_tracker VALUES ('d1', 0.01, 0.05, NULL, 0)")
    conn.execute("INSERT INTO analysis_performance_tracker VALUES ('d2', -0.02, -0.04, NULL, 0)")
    conn.commit()
    conn.close()
    events = tmp_path / "e.jsonl"
    rows = [
        {"event_type": "decision_inputs.shadow_captured", "market": "KR", "decision_id": "d1",
         "attributes": {"decision": "Skip", "features": {"rvol_time_scaled_linear": 2.5, "rubric_probe": {"x": True}},
                        "peer_valuation": {"per_discount_vs_median_pct": 40}}},
        {"event_type": "decision_inputs.shadow_captured", "market": "KR", "decision_id": "d1", "attributes": {}},
        {"event_type": "decision_inputs.shadow_captured", "market": "KR", "decision_id": "d2",
         "attributes": {"decision": "Skip", "features": {"rvol_time_scaled_linear": 0.5}}},
        {"event_type": "decision_inputs.shadow_captured", "market": "KR", "decision_id": "zz", "attributes": {}},
        {"event_type": "other", "decision_id": "d1"},
    ]
    events.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    packet = P.build(P.read_events([events]), P.outcomes(db))
    assert packet["join"] == {"matched": 2, "duplicate_event": 1, "unmatched": 1}
    assert packet["buckets"]["KR|rvol_time_scaled_linear|>=2|14d"] == {"n": 1, "mean": 0.05, "median": 0.05, "win_rate": 1.0}
    assert packet["buckets"]["KR|rvol_time_scaled_linear|<1|14d"]["mean"] == -0.04
    assert packet["buckets"]["KR|per_discount_vs_median_pct|>=30|14d"]["n"] == 1
    assert packet["buckets"]["KR|rvol_time_scaled_linear|>=2|30d"] == {"n": 0}
    assert packet["sufficiency"]["verdict"] == "CONTINUE_CAPTURE"

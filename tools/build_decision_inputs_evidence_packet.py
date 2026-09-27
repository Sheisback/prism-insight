"""Evidence packet: do decision-time input features separate forward returns?

    python tools/build_decision_inputs_evidence_packet.py --events logs/prism_events.jsonl \
        --db stock_tracking_db.sqlite

Exact decision_id join between ``decision_inputs.shadow_captured`` events and the
7/14/30-day performance trackers. Buckets are pre-registered below. This measures
association on the same decisions, not the effect of showing a feature to the
BUY agent; that needs a paired LLM replay.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

BUCKETS = {
    "rvol_time_scaled_linear": (1.0, 2.0),
    "rvol_last_completed": (1.0, 2.0),
    "move_atr_multiple": (1.0, 2.0),
    "gap_atr_multiple": (0.5, 1.5),
    "dist_20d_high_pct": (-5.0, 0.0),
    "up_down_volume_ratio_20": (1.0, 1.5),
}
PEER_BUCKETS = {"per_discount_vs_median_pct": (-30.0, 30.0), "pbr_discount_vs_median_pct": (-30.0, 30.0)}
HORIZONS = ("7d", "14d", "30d")
MIN_PER_BUCKET = 30


def _bucket(value, edges):
    if value is None:
        return "MISSING"
    low, high = edges
    return f"<{low:g}" if value < low else (f"{low:g}..{high:g}" if value < high else f">={high:g}")


def read_events(paths):
    for path in paths:
        opener = gzip.open if str(path).endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("event_type") == "decision_inputs.shadow_captured" and event.get("decision_id"):
                    yield event


def outcomes(db_path):
    uri = "file:" + str(db_path) + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        out = {}
        for market, sql in (("KR", "SELECT decision_id, tracked_7d_return, tracked_14d_return, tracked_30d_return, "
                                   "was_traded FROM analysis_performance_tracker WHERE decision_id IS NOT NULL"),
                            ("US", "SELECT decision_id, return_7d, return_14d, return_30d, was_traded "
                                   "FROM us_analysis_performance_tracker WHERE decision_id IS NOT NULL")):
            for decision_id, r7, r14, r30, traded in conn.execute(sql):
                out.setdefault((market, decision_id), []).append({"7d": r7, "14d": r14, "30d": r30, "traded": traded})
        return out


def _stats(values):
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0}
    return {"n": len(values), "mean": round(statistics.mean(values), 6), "median": round(statistics.median(values), 6),
            "win_rate": round(sum(v > 0 for v in values) / len(values), 4)}


def build(events, tracked):
    groups = defaultdict(list)
    join = defaultdict(int)
    seen = set()
    for event in events:
        key = (event.get("market"), event["decision_id"])
        if key in seen:
            join["duplicate_event"] += 1
            continue
        seen.add(key)
        rows = tracked.get(key, [])
        if len(rows) != 1:
            join["unmatched" if not rows else "ambiguous"] += 1
            continue
        join["matched"] += 1
        attrs = event.get("attributes") or {}
        features = attrs.get("features") or {}
        peer = attrs.get("peer_valuation") or {}
        earnings = attrs.get("earnings") or {}
        labels = {name: _bucket(features.get(name), edges) for name, edges in BUCKETS.items()}
        labels.update({name: _bucket(peer.get(name), edges) for name, edges in PEER_BUCKETS.items()})
        days = earnings.get("calendar_days_to_earnings")
        labels["earnings_within_14d"] = "MISSING" if days is None else str(days <= 14)
        for probe, flag in (features.get("rubric_probe") or {}).items():
            labels["probe:" + probe] = str(bool(flag))
        decision = str(attrs.get("decision"))
        for name, label in labels.items():
            for horizon in HORIZONS:
                groups[(key[0], name, label, horizon)].append(rows[0][horizon])
                groups[(key[0], name, label, horizon, "decision=" + decision)].append(rows[0][horizon])
    table = {"|".join(k): _stats(v) for k, v in sorted(groups.items())}
    thin = sorted(k for k, v in table.items() if k.endswith("|14d") and 0 < v["n"] < MIN_PER_BUCKET)
    body = {"contract": "decision_inputs_evidence_packet_v1", "join": dict(join), "buckets": table,
            "sufficiency": {"min_per_bucket_14d": MIN_PER_BUCKET, "thin_buckets_14d": thin,
                            "verdict": "CONTINUE_CAPTURE" if thin or not table else "REVIEW_ELIGIBLE"},
            "notes": ["Association on identical decisions, not the causal effect of prompting with a feature.",
                      "Tracker returns are price-path proxies, not fills; MISSING stays MISSING."]}
    body["packet_id"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:24]
    return body


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", nargs="+", required=True)
    parser.add_argument("--db", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(build(read_events([Path(p) for p in args.events]), outcomes(args.db)),
                     ensure_ascii=False, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Deterministic evidence packet for the re-entry SHADOW state (forward or replay).

    python tools/build_reentry_evidence_packet.py runtime/reentry_shadow_state_kr_v1.json [...]

Groups closed hypothetical trades by market / source / event kind / market check
and compares them with the stored controls. PENDING / MISSING / SKIPPED are
counted, never filled with 0%. LATE (backfilled) enrolments are reported apart
from PROSPECTIVE ones; only PROSPECTIVE rows count toward sufficiency.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

COSTS_BP = (0, 25, 50)
MIN_CLOSED = 30
MIN_SIGNAL_DATES = 20


def _stats(values):
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0}
    out = {"n": len(values), "mean": round(statistics.mean(values), 6),
           "median": round(statistics.median(values), 6),
           "win_rate": round(sum(v > 0 for v in values) / len(values), 4)}
    for bp in COSTS_BP[1:]:
        out[f"mean_net_{bp}bp"] = round(statistics.mean(values) - bp / 10000, 6)
    if len(values) > 1:
        trimmed = sorted(values)[:-1]
        out["mean_without_best"] = round(statistics.mean(trimmed), 6)
    return out


def build(states):
    groups = defaultdict(list)
    controls = defaultdict(list)
    paired = defaultdict(list)
    counts = defaultdict(lambda: defaultdict(int))
    signal_dates = defaultdict(set)
    for state in states:
        market = state["market"]
        for watch in state["watches"]:
            enrol = watch.get("enrollment", "LATE")
            source = watch.get("source")
            counts[(market, enrol, source)][watch.get("status_final") or watch["status"]] += 1
            control = watch.get("control") or {}
            if control.get("status") == "CLOSED":
                controls[(market, enrol, source, control["kind"])].append(control.get("ret"))
            for event in watch.get("events", []):
                trade = event.get("trade") or {}
                check = (event.get("market_check") or {}).get("ok")
                label = "market_ok" if check else ("market_weak" if check is False else "market_unknown")
                key = (market, enrol, source, event["kind"])
                counts[key][trade.get("status", "UNKNOWN")] += 1
                if trade.get("status") != "CLOSED":
                    continue
                groups[key + ("all",)].append(trade["ret"])
                groups[key + (label,)].append(trade["ret"])
                signal_dates[(market, enrol)].add(event["date"])
                if control.get("status") == "CLOSED":
                    paired[key].append(trade["ret"] - (control.get("ret") or 0.0)
                                       if control["kind"] == "IMMEDIATE_NEXT_OPEN" else None)
    closed_prospective = defaultdict(int)
    for (market, enrol, *_rest), values in groups.items():
        if enrol == "PROSPECTIVE" and _rest[-1] == "all":
            closed_prospective[market] += len(values)
    sufficiency = {}
    for market in sorted({s["market"] for s in states}):
        reasons = []
        if closed_prospective[market] < MIN_CLOSED:
            reasons.append(f"PROSPECTIVE_CLOSED_LT_{MIN_CLOSED}")
        if len(signal_dates[(market, "PROSPECTIVE")]) < MIN_SIGNAL_DATES:
            reasons.append(f"PROSPECTIVE_SIGNAL_DATES_LT_{MIN_SIGNAL_DATES}")
        sufficiency[market] = {"data_sufficient": not reasons, "reasons": reasons,
                               "verdict": "CONTINUE_CAPTURE" if reasons else "REVIEW_ELIGIBLE"}
    body = {
        "contract": "reentry_evidence_packet_v1",
        "trades": {"|".join(k): _stats(v) for k, v in sorted(groups.items())},
        "controls": {"|".join(k): _stats(v) for k, v in sorted(controls.items())},
        "paired_vs_immediate": {"|".join(k): _stats([x for x in v if x is not None])
                                for k, v in sorted(paired.items())},
        "status_counts": {"|".join(k): dict(v) for k, v in sorted(counts.items())},
        "sufficiency": sufficiency,
        "notes": ["Hypothetical next-open trades, not fills; costs are assumptions.",
                  "LOCATION_SKIP control = buying at the next open after the analysis; STOP_EXIT control = holding without the stop.",
                  "Never an automatic promotion: LIVE needs the trading change harness and explicit approval."],
    }
    body["packet_id"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:24]
    return body


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("states", nargs="+")
    args = parser.parse_args(argv)
    states = [json.loads(Path(p).read_text()) for p in args.states]
    print(json.dumps(build(states), ensure_ascii=False, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

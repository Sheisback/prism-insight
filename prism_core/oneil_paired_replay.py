"""Bounded exploratory paired replay; temporary ledgers, never broker execution."""
from decimal import Decimal
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory

from prism_core.oneil_adaptive_policy import (
    _num, _ref, _time, _validate, create_plan, evaluate_target,
)
from prism_core.strategy_ledger import StrategyLedger

VERSION = "oneil-paired-replay-v1"
INVALID_REASONS = {
    "MISSING_QUOTE_OR_IDENTITY", "INVALID_QUOTE_OR_IDENTITY", "MISSING_GATE_EVIDENCE",
    "MISSING_VOLUME_DENOMINATOR", "MISSING_ADD_EVIDENCE", "ADD_EVIDENCE_REJECTED",
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _identity(event, plan):
    _ref(event["source_ref"])
    for key, expected in (("symbol", plan["symbol"]),
                          ("source_decision_ref", plan["source_decision_ref"]),
                          ("price_basis_ref", plan["setup"]["price_basis_ref"])):
        if event[key] != expected:
            raise ValueError("SOURCE_IDENTITY_MISMATCH")
    at = _time(event["occurred_at"])
    if _time(event["available_at"]) > at:
        raise ValueError("FUTURE_SOURCE")
    return at


def _validate_campaign(campaign):
    _ref(campaign["campaign_id"])
    if campaign.get("capture_gaps"):
        raise ValueError("CAPTURE_GAPS_UNRESOLVED")
    plan = campaign["plan"]
    _validate(plan)
    entry, terminal = campaign["entry"], campaign["exit"]
    start, end = _identity(entry, plan), _identity(terminal, plan)
    if start != _time(plan["created_at"]) or end <= start:
        raise ValueError("ENTRY_EXIT_TIME_MISMATCH")
    if _num(entry["price"], True) != _num(plan["entry_reference"], True):
        raise ValueError("ENTRY_PRICE_MISMATCH")
    _num(terminal["price"], True)
    ticks = campaign["ticks"]
    if not isinstance(ticks, list) or not 1 <= len(ticks) <= 10000:
        raise ValueError("TICK_TAPE_UNAVAILABLE")
    previous, stop = start, _num(plan["initial_stop"], True)
    for tick in ticks:
        at = _identity(tick, plan)
        if not previous < at < end:
            raise ValueError("TICK_CHRONOLOGY")
        _ref(tick["stop_source_ref"])
        if _time(tick["stop_available_at"]) > at:
            raise ValueError("FUTURE_STOP")
        current_stop = _num(tick["current_stop"], True)
        if current_stop < stop:
            raise ValueError("LOWERED_STOP")
        stop, previous = current_stop, at
        facts = tick["evidence"]
        # Never let an early policy return hide missing/future raw evidence.
        for name in ("quote", "gates", "volume", "market_window", "bars"):
            if name not in facts:
                raise ValueError("MISSING_TICK_EVIDENCE")
        for timestamp in (facts["quote"]["observed_at"], facts["gates"]["observed_at"],
                          facts["volume"]["as_of"]):
            if _time(timestamp) > at:
                raise ValueError("FUTURE_EVIDENCE")
        if any(_time(bar["end_at"]) > at for bar in facts["bars"]):
            raise ValueError("FUTURE_BAR")
        if (facts["symbol"] != plan["symbol"]
                or facts["price_basis_ref"] != plan["setup"]["price_basis_ref"]):
            raise ValueError("EVIDENCE_IDENTITY_MISMATCH")
        # Validate the whole add-evidence schema even when the economic policy
        # would exit early (expiry, protection, or a negative gate).
        probe_plan = create_plan(symbol=plan["symbol"], entry_reference=plan["entry_reference"],
                                 initial_stop=plan["initial_stop"],
                                 source_decision_ref=plan["source_decision_ref"],
                                 created_at=tick["occurred_at"], setup=plan["setup"],
                                 entry_eligible=True)
        probe = evaluate_target(probe_plan, facts, now=tick["occurred_at"],
                                cumulative_allocation=0, remaining_allocation=0,
                                normalized_units=0, remaining_entry_cost=0,
                                current_stop=current_stop)
        if probe["reason"] in INVALID_REASONS or probe["evidence_status"] == "MISSING":
            raise ValueError("UNUSABLE_POLICY_EVIDENCE:" + probe["reason"])
        if probe["reason"] == "ADD_GATE_NOT_MET":
            validation_facts = deepcopy(facts)
            validation_facts["gates"].update(admission=True, risk=True, RR=True, sector=True,
                                               slot=True, market_pulse="UPTREND", regime="moderate_bull")
            probe = evaluate_target(probe_plan, validation_facts, now=tick["occurred_at"],
                                    cumulative_allocation=0, remaining_allocation=0,
                                    normalized_units=0, remaining_entry_cost=0,
                                    current_stop=current_stop)
            if probe["reason"] in INVALID_REASONS or probe["evidence_status"] == "MISSING":
                raise ValueError("UNUSABLE_POLICY_EVIDENCE:" + probe["reason"])


def _run(campaign, bps, directory):
    original = campaign["plan"]
    plan = create_plan(symbol=original["symbol"], entry_reference=original["entry_reference"],
                       initial_stop=original["initial_stop"],
                       source_decision_ref=original["source_decision_ref"],
                       created_at=original["created_at"], setup=original["setup"],
                       entry_eligible=True, fee_bps=bps)
    ledger = StrategyLedger(Path(directory) / f"{bps}.sqlite")
    fee = str(Decimal(bps) / 10000)
    for arm, target in (("baseline", 100), ("adaptive", 10)):
        ledger.create_book(arm, "US", max_slots=1, cohort=arm, mode="VALIDATION")
        ledger.apply_target(arm + ":entry", arm, arm, plan["symbol"], target,
                            campaign["entry"]["price"], campaign["entry"]["occurred_at"],
                            fee_rate=fee)
    decisions, last_add = [], None
    exit_event, reason = campaign["exit"], "ORIGINAL_TERMINAL_EXIT"
    for index, tick in enumerate(campaign["ticks"]):
        state = ledger.snapshot("adaptive")["campaigns"][0]
        decision = evaluate_target(
            plan, tick["evidence"], now=tick["occurred_at"],
            cumulative_allocation=state["cumulative_deployed_allocation"],
            remaining_allocation=state["remaining_allocation"],
            normalized_units=state["normalized_units"],
            remaining_entry_cost=state["remaining_entry_cost"],
            current_stop=tick["current_stop"], last_add_bar_end=last_add,
        )
        if decision["reason"] in INVALID_REASONS or decision["evidence_status"] == "MISSING":
            raise ValueError("UNUSABLE_POLICY_EVIDENCE:" + decision["reason"])
        decisions.append(decision)
        if decision["action"] == "PROTECTIVE_EXIT_REQUIRED":
            exit_event = dict(tick, price=decision["price"])
            reason = "COMMON_PROTECTIVE_EXIT"
            break
        if decision["action"] == "ADD":
            ledger.apply_target(f"adaptive:add:{index}", "adaptive", "adaptive", plan["symbol"],
                                str(Decimal(decision["target_allocation"]) * 100),
                                decision["price"], tick["occurred_at"], fee_rate=fee,
                                policy_version=plan["policy_version"])
            last_add = decision["bar_end"]
    returns = {}
    for arm in ("baseline", "adaptive"):
        ledger.sell(arm + ":exit", arm, exit_event["price"], exit_event["occurred_at"], fee_rate=fee)
        state = ledger.snapshot(arm)["campaigns"][0]
        returns[arm] = state["realized_contribution"]
    return {"fee_bps_per_side": bps, "net_return_full_budget": returns,
            "paired_delta": str(Decimal(returns["adaptive"]) - Decimal(returns["baseline"])),
            "exit_reason": reason, "exit_at": exit_event["occurred_at"],
            "decisions": decisions}


def _stats(values):
    if not values:
        return None
    gains = sum((x for x in values if x > 0), Decimal(0))
    losses = -sum((x for x in values if x < 0), Decimal(0))
    return {"mean": str(sum(values) / len(values)), "median": str(median(values)),
            "worst_campaign_return": str(min(values)),
            "profit_factor": str(gains / losses) if losses else None,
            "profit_factor_status": "DEFINED" if losses else "NO_LOSSES_UNDEFINED",
            "win_rate": str(Decimal(sum(x > 0 for x in values)) / len(values))}


def _summary(results, bps):
    rows = sorted((r["campaign_id"], r["cost_cases"][str(bps)]) for r in results if r["status"] == "EVALUATED")
    baseline = [Decimal(r["net_return_full_budget"]["baseline"]) for _, r in rows]
    adaptive = [Decimal(r["net_return_full_budget"]["adaptive"]) for _, r in rows]
    deltas = [a - b for a, b in zip(adaptive, baseline)]
    removal = None
    if rows and max(baseline) > 0:
        index = max(range(len(rows)), key=lambda i: baseline[i])
        removal = {"removed_campaign_id": rows[index][0],
                   "remaining_pairs": len(rows) - 1,
                   "baseline": _stats(baseline[:index] + baseline[index + 1:]),
                   "adaptive": _stats(adaptive[:index] + adaptive[index + 1:]),
                   "paired_delta": _stats(deltas[:index] + deltas[index + 1:])}
    winner_indices = [i for i, value in enumerate(baseline) if value > 0]
    winner_ratio = (sum(adaptive[i] for i in winner_indices) / sum(baseline[i] for i in winner_indices)
                    if winner_indices else None)
    return {"baseline": _stats(baseline), "adaptive": _stats(adaptive),
            "baseline_winner_count": len(winner_indices),
            "winner_profit_capture_ratio": None if winner_ratio is None else str(winner_ratio),
            "winner_capture_at_least_90_pct": winner_ratio is not None and winner_ratio >= Decimal(".9"),
            "paired_delta": _stats(deltas), "remove_best_baseline_winner": removal}


def evaluate_replay(payload):
    if (not isinstance(payload, dict) or payload.get("contract") != "oneil-paired-replay-input-v1"
            or payload.get("kind") not in {"SYNTHETIC", "HISTORICAL"}):
        raise ValueError("explicit non-prospective replay contract required")
    campaigns = payload.get("campaigns")
    if not isinstance(campaigns, list) or len(campaigns) > 1000:
        raise ValueError("bounded campaign list required")
    ids = [c.get("campaign_id") for c in campaigns if isinstance(c, dict)]
    if len(ids) != len(campaigns) or any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("unique campaign IDs required")
    input_hash = digest(payload)
    results = []
    for campaign in campaigns:
        result = {"campaign_id": campaign["campaign_id"]}
        try:
            _validate_campaign(campaign)
            with TemporaryDirectory(prefix="oneil-paired-") as directory:
                cases = {str(bps): _run(campaign, bps, directory) for bps in (10, 25)}
            result.update(status="EVALUATED", cost_cases=cases,
                          entry_date=_time(campaign["entry"]["occurred_at"]).date().isoformat())
        except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError) as exc:
            result.update(status="INPUT_UNAVAILABLE", reason="INVALID_OR_MISSING_ORIGINAL_EVIDENCE",
                          exception_type=type(exc).__name__)
        results.append(result)
    valid = [r for r in results if r["status"] == "EVALUATED"]
    dates = len({r["entry_date"] for r in valid})
    summaries = {str(bps): _summary(results, bps) for bps in (10, 25)}
    output = {"contract": VERSION, "input_sha256": input_hash,
              "status": "EVALUATED" if valid else "INPUT_UNAVAILABLE",
              "validation_kind": "FUNCTIONAL_ONLY" if payload["kind"] == "SYNTHETIC" else "EXPLORATORY_ONLY",
              "adaptive_arm": "COMPATIBILITY_SCOUT_10", "results": results,
              "coverage": {"supplied": len(campaigns), "evaluated": len(valid),
                           "unavailable": len(campaigns) - len(valid), "entry_dates": dates},
              "minimum_descriptive_sample_met": len(valid) >= 30 and dates >= 20 and summaries["10"]["baseline_winner_count"] >= 10,
              "cost_cases": summaries,
              "performance_validated": False, "live_ready": False, "broker_execution": False,
              "source_authentication": "CALLER_SUPPLIED_NOT_AUTHENTICATED",
              "portfolio_drawdown": None}
    output["packet_id"] = digest(output)
    return output


def assess_entry_quality_packet(packet):
    """Coverage only: canonical summaries cannot reconstruct an adaptive tape."""
    if (packet.get("packet_schema_version") != 3
            or packet.get("analysis_contract_version") != "entry-quality-harness-v2"
            or packet.get("market") != "US"
            or packet.get("source_contract", {}).get("kind") != "local_sanitized_observability_jsonl"):
        raise ValueError("canonical US sanitized Evidence Packet required")
    _ref(packet["packet_id"])
    content = {key: value for key, value in packet.items() if key != "packet_id"}
    if digest(content)[:24] != packet["packet_id"]:
        raise ValueError("canonical Packet hash mismatch")
    _time(packet["as_of"])
    readiness = packet["strategy_readiness"]
    if readiness["analysis_basis"] != "strategy_ledger" or readiness["broker_fill_coverage_required"] is not False:
        raise ValueError("independent strategy outcome contract required")
    ranked = packet["robustness_inputs"]["strategy_ranked"]
    closed = packet["coverage"]["strategy_closed_trade_count"]
    if type(closed) is not int or closed < 0 or len(ranked) != closed:
        raise ValueError("strategy outcome coverage mismatch")
    # Preserve the canonical outcomes independently of fill status. Do not
    # transform terminal returns into an imagined intraday adaptive path.
    outcomes = [{key: row[key] for key in ("decision_ref", "ticker", "trigger_type", "regime",
                                         "policy_version", "return_pct")} for row in ranked]
    out = {"contract": VERSION, "source_packet_id": packet["packet_id"],
           "source_sha256": digest(packet), "as_of": packet["as_of"],
           "status": "INPUT_UNAVAILABLE", "validation_kind": "CANONICAL_COVERAGE_ONLY",
           "coverage": {"candidate_count": packet["prospective_cohort"]["candidate_count"],
                        "decision_dates": packet["prospective_cohort"]["decision_date_count"],
                        "strategy_closed_count": closed, "paired_evaluated": 0},
           "canonical_insufficiency_reasons": deepcopy(readiness["insufficiency_reasons"]),
           "missingness": deepcopy(packet["missingness"]),
           "replay_insufficiency_reasons": ["ADAPTIVE_PLAN_UNAVAILABLE", "TICK_TAPE_UNAVAILABLE",
                                           "ORIGINAL_STOP_PATH_UNAVAILABLE", "EXIT_TAPE_UNAVAILABLE"],
           "strategy_outcomes_independent_of_fills": outcomes,
           "performance_validated": False, "live_ready": False, "broker_execution": False,
           "broker_realized_pnl_verified": False, "verdict": "CONTINUE_CAPTURE"}
    out["packet_id"] = digest(out)
    return out

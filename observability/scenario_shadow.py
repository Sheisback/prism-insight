"""Opt-in original-plan capture; never orders or alters a trading decision.

The durable original and delivery checkpoint are separate. A crash after spool
append but before checkpoint can duplicate the same event ID (not exactly-once).
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import tempfile

from observability.events import emit_event
from prism_core.scenario_shadow_policy import VERSION, create_plan

_APP_ID = 1396917059
_DEFAULT_DB = Path(__file__).resolve().parents[1] / "runtime/scenario-shadow-capture.sqlite"
_SCHEMA = (
    "CREATE TABLE captures (capture_key TEXT PRIMARY KEY, payload TEXT NOT NULL)",
    "CREATE TABLE delivered (capture_key TEXT PRIMARY KEY REFERENCES captures(capture_key))",
    "CREATE TRIGGER frozen_update BEFORE UPDATE ON captures BEGIN SELECT RAISE(ABORT, 'immutable capture'); END",
    "CREATE TRIGGER frozen_delete BEFORE DELETE ON captures BEGIN SELECT RAISE(ABORT, 'immutable capture'); END",
    "CREATE TRIGGER frozen_replace BEFORE INSERT ON captures WHEN EXISTS (SELECT 1 FROM captures WHERE capture_key=NEW.capture_key) BEGIN SELECT RAISE(ABORT, 'immutable capture'); END",
)


def _json(value):
    # No default=str: unknown values must not leak object representations.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _safe_json(value):
    if value is None or type(value) in (str, bool, int, float):
        return
    if type(value) is list:
        for item in value:
            _safe_json(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _safe_json(item)
        return
    raise ValueError("plain JSON original required")


def _validate_db(connection):
    actual = {row[0] for row in connection.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")}
    if (actual != set(_SCHEMA)
            or connection.execute("PRAGMA application_id").fetchone()[0] != _APP_ID
            or connection.execute("PRAGMA user_version").fetchone()[0] != 1):
        raise ValueError("not an owned capture database")


def _connect(path):
    if (path.is_symlink() or path.suffix.lower() not in {".sqlite", ".sqlite3", ".db"}
            or path.name.lower() in {
            "stock_tracking.db", "us_stock_tracking.db", "stock_analysis.db",
            "stock_tracking_db.sqlite", "us_stock_tracking.sqlite",
            "strategy-ledger.sqlite", "strategy_ledger.db", "scenario-shadow.sqlite"}):
        raise ValueError("dedicated capture database required")
    if path.exists():
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.05)) as check:
            _validate_db(check)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".scenario-capture-", dir=path.parent)
        os.close(descriptor)
        try:
            with closing(sqlite3.connect(temporary, timeout=.05)) as initialize, initialize:
                initialize.execute("BEGIN IMMEDIATE")
                for statement in _SCHEMA:
                    initialize.execute(statement)
                initialize.execute("PRAGMA application_id=1396917059")
                initialize.execute("PRAGMA user_version=1")
            # Publish a fully initialized file without overwriting a racing writer.
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
    connection = sqlite3.connect(path, timeout=.05)
    try:
        _validate_db(connection)
    except Exception:
        connection.close()
        raise
    return connection


def capture_enabled():
    from prism_core.oneil_config import capture_enabled as operational_enabled
    return operational_enabled() or os.getenv("SCENARIO_SHADOW_CAPTURE_ENABLED", "false").strip().lower() in {
        "true", "1", "yes", "on"}


def _initial_underwriting(scenario):
    # Freeze only fields actually consumed by the deterministic gate. Never
    # copy rationale, reports, credentials, account identity or broker payloads.
    fields = ("decision", "buy_score", "macro_adjustment", "effective_score", "min_score",
              "target_price", "stop_loss", "entry_price", "_analysis_entry_price",
              "risk_reward_ratio", "expected_return_pct", "expected_loss_pct",
              "momentum_signal_count", "additional_confirmation_count", "max_portfolio_size",
              "sector", "_decision_id", "_deterministic_trend_facts")
    result = {key: scenario[key] for key in fields if key in scenario
              and type(scenario[key]) in (str, int, float, bool, type(None))}
    if len(_json(result).encode()) > 8192:
        return None
    fundamental = scenario.get("fundamental_check")
    if isinstance(fundamental, dict) and type(fundamental.get("all_passed")) is bool:
        result["fundamental_check"] = {"all_passed": fundamental["all_passed"]}
    context = scenario.get("_decision_context") or {}
    score = context.get("adjusted_score")
    if type(score) in (int, float):
        result["_decision_context"] = {"adjusted_score": score}
    return result


def emit_initial_capture(*, market, ticker, decision_id, position_id, scenario,
                         current_price, entry_eligible, is_add,
                         trigger_type=None, trigger_mode=None, adaptive_review=None, account_id=None):
    """Capture an eligible committed US strategy entry before broker execution.

    All I/O is fail-open. Retry on a later invocation only, never synchronously.
    There is no background delivery: pending originals remain in this registry
    until the identical position is explicitly presented again by the caller.
    No scenario text, account identifiers, or broker fill assumptions are stored.
    """
    if not capture_enabled():
        return None
    try:
        if (market != "US" or entry_eligible is not True or is_add is not False
                or not all(isinstance(value, str) and value.strip()
                           for value in (ticker, decision_id, position_id))
                or not isinstance(scenario, dict)):
            return None
        gates = scenario.get("_decision_context")
        if (not isinstance(gates, dict)
                or gates.get("gate_allowed") is not True
                or gates.get("cooldown_blocked") is not False
                or gates.get("sector_diverse") is not True
                or gates.get("rebound_pilot") is not False
                or "_strategy_projection" in scenario
                or scenario.get("_strategy_policy")
                or (scenario.get("regime_entry_policy") or {}).get("mode") == "rebound_pilot"):
            return None
        _safe_json(scenario)
        original_hash = _hash(scenario)
        captured = datetime.now(timezone.utc)
        plan = create_plan(entry_price=current_price, initial_stop=scenario.get("stop_loss"),
                           entry_at=captured.isoformat(), source_decision_ref=decision_id,
                           entry_eligible=True)
        attributes = {
            "capture_schema_version": 1, "mode": "SHADOW", "plan": plan,
            "original_input_hash": original_hash,
            "phase": "POST_STRATEGY_COMMIT_PRE_BROKER", "trading_impact": "none",
            "execution_provenance": "NOT_REQUESTED", "fill_status": "VIRTUAL_NOT_FILLED",
            "confirmed_fill": False,
            "initial_underwriting": _initial_underwriting(scenario),
        }
        if isinstance(account_id, str) and account_id:
            from observability.trading_context import execution_profile_ref
            attributes["execution_profile_ref"] = execution_profile_ref(account_id)
        if adaptive_review is not None:
            attributes["adaptive_setup"] = _adaptive_setup(
                adaptive_review, ticker=ticker, decision_id=decision_id,
                current_price=current_price, initial_stop=scenario.get("stop_loss"),
                captured_at=captured.isoformat(),
            )
        if isinstance(trigger_type, str):
            attributes["trigger_type_hash"] = _hash(trigger_type)
        if trigger_mode in {"morning", "afternoon"}:
            attributes["trigger_mode"] = trigger_mode
        key = _hash([VERSION, position_id])
        payload = {
            "event_id": key[:32], "service": "prism-us-scenario-shadow",
            "market": market, "ticker": ticker, "decision_id": decision_id,
            "position_id": position_id, "attributes": attributes,
            "event_time": captured.isoformat(),
        }
        from prism_core.oneil_config import load as load_execution_config
        configured = load_execution_config(protection_only=True)
        path = Path(os.getenv("SCENARIO_SHADOW_CAPTURE_DB", configured.get("capture_db", str(_DEFAULT_DB))))
        connection = _connect(path)
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                stored = connection.execute("SELECT payload FROM captures WHERE capture_key=?", (key,)).fetchone()
                if stored:
                    payload = json.loads(stored[0])
                    if any(payload[field] != value for field, value in (
                            ("market", market), ("ticker", ticker),
                            ("decision_id", decision_id), ("position_id", position_id))):
                        return None
                else:
                    connection.execute("INSERT INTO captures VALUES (?,?)", (key, _json(payload)))
            # The independent tape only receives the durable frozen original.
            try:
                from observability.oneil_capture import capture_initial
                capture_initial(payload)
            except Exception:
                logging.getLogger(__name__).warning("optional execution capture unavailable")
            # Serialize cooperating writers. Original is already durable if emit fails.
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute("SELECT 1 FROM delivered WHERE capture_key=?", (key,)).fetchone():
                    return None
                payload["event_time"] = datetime.fromisoformat(payload["event_time"])
                result = emit_event("scenario_shadow.initial_captured", **payload)
                if result is not None:
                    connection.execute("INSERT INTO delivered VALUES (?)", (key,))
                return result
        finally:
            connection.close()
    except Exception:  # noqa: BLE001 - observer must never interrupt trading
        return None


def _adaptive_setup(linked, *, ticker, decision_id, current_price, initial_stop, captured_at):
    """Freeze a separate research plan, not a revision of the original v1 plan.

    The trusted caller loads and verifies the PDF-bound sidecar. Revalidate the
    source spans here and export only strict policy fields, never report prose.
    Missing or rejected setup must not prevent the original capture/protection.
    """
    result = {"policy_version": "oneil-adaptive-v2", "status": "MISSING",
              "reason": "LINKED_REVIEW_UNAVAILABLE", "broker_execution": False}
    try:
        from prism_core.oneil_adaptive_policy import create_plan as adaptive_plan
        from prism_core.oneil_setup_inputs import build_setup_input

        if (not isinstance(linked, dict)
                or linked.get("contract_version") != "oneil-batch-linked-review-v1"
                or linked.get("market") != "US" or linked.get("symbol") != ticker
                or linked.get("decision_ref") != decision_id):
            return dict(result, status="INVALID", reason="LINKED_IDENTITY_MISMATCH")
        if linked.get("status") != "OK":
            return result
        report_hash = linked.get("report_sha256")
        if (not isinstance(report_hash, str) or len(report_hash) != 64
                or any(c not in "0123456789abcdef" for c in report_hash)):
            return dict(result, status="INVALID", reason="REPORT_HASH_INVALID")
        bundle = linked["bundle"]
        review = bundle["review"]
        setup = build_setup_input(
            report_text=bundle["report_text"], review=review, symbol=ticker,
            decision_ref=decision_id, as_of=captured_at,
            price_basis_ref=review["price_basis_ref"],
        )
        result["report_sha256"] = report_hash
        result["review_sha256"] = _hash(review)
        if setup["status"] != "OK":
            return dict(result, status=setup["status"], reason="SETUP_NOT_CONFIRMED")
        plan = adaptive_plan(
            symbol=ticker, entry_reference=current_price, initial_stop=initial_stop,
            source_decision_ref=decision_id, created_at=captured_at,
            setup=setup["setup"], entry_eligible=True,
        )
        return dict(result, status="OK", reason="PLAN_FROZEN_NOT_EXECUTED", plan=plan)
    except Exception:  # noqa: BLE001 - optional evidence cannot suppress v1 capture
        return dict(result, status="INVALID", reason="REVIEW_OR_PLAN_INVALID")

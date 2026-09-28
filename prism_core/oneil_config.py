"""Small operator-owned configuration. Missing means OFF; LIVE needs approval."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from prism_core.oneil_adaptive_policy import _hash, _num, _time

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "runtime/oneil-execution.json"
POLICY = "oneil-adaptive-v2"
# Operator files written before v2 still name v1. They remain valid for OFF and
# SHADOW (new SHADOW campaigns freeze v2 plans regardless); LIVE requires v2 in
# both the configuration and its explicit approval record.
LEGACY_POLICIES = frozenset({"oneil-adaptive-v1"})
ARM = "INITIAL_POLICY_50"


def implementation_hash():
    sources = ("prism_core/oneil_adaptive_policy.py", "prism_core/oneil_runtime_inputs.py",
               "prism_core/oneil_execution.py", "prism_core/oneil_dispatcher.py",
               "prism_core/oneil_broker.py", "prism_core/oneil_routing.py",
               "prism_core/execution_service.py", "prism-us/trading/us_stock_trading.py",
               "prism-us/us_stock_tracking_agent.py", "prism_core/oneil_service.py",
               "tools/hardstop_seller.py", "tools/trend_exit_seller.py", "prism_core/oneil_config.py",
               "cores/buy_gate.py", "cores/regime_policy.py", "prism_core/order_intents.py",
               "prism_core/oneil_current_capture.py", "prism_core/oneil_input_bridge.py")
    return _hash({name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in sources})


class ConfigurationError(ValueError):
    pass


def defaults():
    return dict(config_version=1, mode="OFF", policy=POLICY, initial_arm=ARM,
                accounts=[], capture_since=None, max_slots=10, interval_seconds=60,
                shadow_db=str(ROOT / "runtime/oneil-shadow-execution.sqlite"),
                live_db=str(ROOT / "runtime/oneil-live-execution.sqlite"),
                runtime_db=str(ROOT / "runtime/oneil-initial50-shadow.sqlite"),
                capture_db=str(ROOT / "runtime/scenario-shadow-capture.sqlite"),
                tape_db=str(ROOT / "runtime/oneil-capture-tape.sqlite"),
                holdings_db=str(ROOT / "stock_tracking_db.sqlite"), live_approval=None)


def validate(config, *, now=None, check_approval=True):
    if not isinstance(config, dict) or set(config) - set(defaults()):
        raise ConfigurationError("unknown configuration fields")
    value = {**defaults(), **config}
    if (value["config_version"] != 1 or value["mode"] not in {"OFF", "SHADOW", "LIVE"}
            or value["policy"] not in {POLICY, *LEGACY_POLICIES} or value["initial_arm"] != ARM
            or (value["mode"] == "LIVE" and value["policy"] != POLICY)):
        raise ConfigurationError("unsupported mode or policy")
    accounts = value["accounts"]
    if (not isinstance(accounts, list) or len(accounts) > 10
            or any(not isinstance(a, str) or not a.strip() for a in accounts)
            or len(set(accounts)) != len(accounts)):
        raise ConfigurationError("explicit account names required")
    if type(value["max_slots"]) is not int or not 1 <= value["max_slots"] <= 10:
        raise ConfigurationError("existing slot cap required")
    if type(value["interval_seconds"]) is not int or not 60 <= value["interval_seconds"] <= 3600:
        raise ConfigurationError("bounded poll interval required")
    if value["mode"] != "OFF":
        if not accounts:
            raise ConfigurationError("enabled account scope required")
        _time(value["capture_since"])
    paths = []
    for key in ("shadow_db", "live_db", "runtime_db", "capture_db", "tape_db", "holdings_db"):
        path = Path(value[key])
        if not path.is_absolute():
            path = ROOT / path
        if path.is_symlink() or path.suffix not in {".sqlite", ".sqlite3", ".db"}:
            raise ConfigurationError("explicit nonsymlink database path required")
        path = path.resolve()
        if any(path == other or (path.exists() and other.exists() and path.samefile(other)) for other in paths):
            raise ConfigurationError("databases must be distinct")
        paths.append(path)
        value[key] = str(path)
    if value["mode"] == "LIVE" and check_approval:
        require_live_approval(value, now=now)
    return value


def require_live_approval(config, *, now=None, account=None, unit_budget=None):
    """This is an explicit operator authorization record, not performance proof."""
    approval = config.get("live_approval")
    current = _time(now or datetime.now(timezone.utc).isoformat())
    if not isinstance(approval, dict):
        raise ConfigurationError("LIVE_APPROVAL_REQUIRED")
    content = {k: v for k, v in approval.items() if k != "approval_hash"}
    if (approval.get("approval_hash") != _hash(content)
            or approval.get("implementation_hash") != implementation_hash()
            or approval.get("policy") != POLICY or approval.get("initial_arm") != ARM
            or approval.get("scope") != "NEW_CAMPAIGNS_ONLY"
            or approval.get("accounts") != config["accounts"]
            or not isinstance(approval.get("approved_by"), str) or not approval["approved_by"].strip()
            or not _time(approval["approved_at"]) <= current < _time(approval["expires_at"])):
        raise ConfigurationError("LIVE_APPROVAL_INVALID_OR_EXPIRED")
    cap = _num(approval["max_unit_budget_usd"], True)
    if account is not None and account not in config["accounts"]:
        raise ConfigurationError("LIVE_ACCOUNT_NOT_APPROVED")
    if unit_budget is not None and _num(unit_budget, True) > cap:
        raise ConfigurationError("LIVE_BUDGET_EXCEEDS_APPROVAL")
    return approval


def load(path=None, *, now=None, protection_only=False):
    target = Path(path or os.getenv("ONEIL_EXECUTION_CONFIG", str(DEFAULT_PATH)))
    if not target.exists():
        return defaults()
    if target.is_symlink() or target.stat().st_size > 32768:
        raise ConfigurationError("invalid operator configuration file")
    try:
        return validate(json.loads(target.read_text()), now=now, check_approval=not protection_only)
    except (ValueError, KeyError, TypeError, OSError) as error:
        raise ConfigurationError("operator configuration unavailable") from error


def capture_enabled():
    # Optional evidence must not break old trading if config is unavailable.
    try:
        return load()["mode"] in {"SHADOW", "LIVE"}
    except ConfigurationError:
        return False

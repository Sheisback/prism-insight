"""Technical readiness and explicit activation authority, kept separate.

The real dispatcher is oneil_dispatcher; this legacy projection helper never
submits orders. Arbitrary environment flags are not LIVE authorization.
"""
from prism_core.oneil_adaptive_policy import _hash, _num, _ref, _time
from prism_core.strategy_ledger_execution import project_account_target
from prism_core.oneil_config import load, require_live_approval

IMPLEMENTED = (
    "INITIAL_MAX_50_REPLACES_LEGACY_FULL_BUY",
    "ATOMIC_ACCOUNT_CAMPAIGN_OWNERSHIP",
    "EXACT_ORDER_FILL_AND_PENDING_RECONCILIATION",
    "DURABLE_ORDER_RESERVATION",
    "OWNED_EXIT_COORDINATION",
    "EXECUTION_SERVICE_BROKER_DISPATCH",
)


def readiness(config=None, *, now=None):
    blockers = []
    try:
        config = config or load(protection_only=True)
        if config["mode"] != "LIVE":
            blockers.append("LIVE_MODE_NOT_SELECTED")
        try:
            require_live_approval(config, now=now)
        except (ValueError, KeyError, TypeError):
            blockers.append("EXPLICIT_CURRENT_LIVE_APPROVAL_REQUIRED")
    except (ValueError, KeyError, TypeError):
        blockers.append("CONFIGURATION_UNAVAILABLE")
    return dict(contract_version="oneil-live-boundary-v2", supported_modes=["OFF", "SHADOW", "LIVE"],
                technical_switch_ready=True, implemented=list(IMPLEMENTED),
                live_ready=not blockers, broker_execution=False, blockers=blockers,
                performance_validated=False)


def assert_mode(mode):
    if mode not in ("OFF", "SHADOW", "LIVE"):
        raise ValueError("UNSUPPORTED_MODE")
    if mode == "LIVE":
        status = readiness()
        if not status["live_ready"]:
            raise ValueError("LIVE_UNAVAILABLE:" + ",".join(status["blockers"]))
    return mode


def project_live_candidate(runtime, intent_id, *, plan_hash, receipt, quote, now):
    """Load the committed policy intent and project only against an owned receipt.

    All receipt claims remain caller-attested. This helper submits nothing and
    does not make readiness true, even when its arithmetic yields whole shares.
    """
    blocked = dict(status="BLOCKED", quantity=0, no_order=True, live_ready=False,
                   execution_authorized=False, source_authentication="CALLER_ATTESTED_NOT_AUTHENTICATED")
    try:
        intent = runtime.intent(intent_id)
        if (intent.get("contract_version") != "oneil-intent-candidate-v1"
                or intent.get("intent_id") != intent_id
                or intent.get("intent_hash") != _hash({k: v for k, v in intent.items() if k != "intent_hash"})
                or intent.get("plan_hash") != plan_hash
                or intent["decision"]["action"] != "ADD"):
            return dict(blocked, reason="INVALID_COMMITTED_INTENT")
        snapshot = runtime.snapshot(intent["campaign_id"])
        if (snapshot["state"]["closed"] or snapshot["revision"] != intent["revision"] + 1
                or snapshot["state"]["plan"]["plan_hash"] != plan_hash):
            return dict(blocked, reason="STALE_CAMPAIGN_INTENT")
        current = _time(now)
        if not 0 <= (current - _time(intent["occurred_at"])).total_seconds() <= 120:
            return dict(blocked, reason="STALE_INTENT")
        identity = {k: intent[k] for k in ("campaign_id", "position_id", "symbol", "source_decision_ref", "plan_hash")}
        if any(receipt.get(k) != v for k, v in identity.items()):
            return dict(blocked, reason="ACCOUNT_CAMPAIGN_IDENTITY_MISMATCH")
        initial_arm = intent.get("initial_arm")
        initial_target = _num(receipt.get("initial_target_pct"), True)
        initial_valid = (
            initial_arm == "INITIAL_POLICY_50" and initial_target <= 50
            and receipt.get("initial_max_pct") == 50
            and receipt.get("ownership") == "ONEIL_INITIAL_POLICY_50"
        ) or (
            initial_arm == "COMPATIBILITY_SCOUT_10" and initial_target == 10
            and receipt.get("ownership") == "ONEIL_INITIAL_SCOUT"
        )
        if (receipt.get("contract_version") != "oneil-owned-account-receipt-v1"
                or receipt.get("mode") != "LIVE" or receipt.get("basis") != "CONFIRMED_BROKER_FILLS"
                or not initial_valid or receipt.get("adopted") is not False
                or receipt.get("currency") != "USD"):
            return dict(blocked, reason="OWNED_INITIAL_SCOUT_RECEIPT_REQUIRED")
        _ref(receipt["account_id"])
        _ref(receipt["source_ref"])
        _ref(receipt["execution_profile_ref"])
        if (receipt.get("execution_status") != "CONFIRMED"
                or receipt.get("settlement_status") != "SETTLED"
                or receipt.get("unknown_execution") is not False
                or _num(receipt["reserved_buy_notional"]) != 0):
            return dict(blocked, reason="UNSETTLED_OR_PENDING_EXECUTION")
        if not 0 <= (current - _time(receipt["observed_at"])).total_seconds() <= 120:
            return dict(blocked, reason="STALE_ACCOUNT_RECEIPT")
        previous = _num(receipt["previously_submitted_target_pct"], True)
        budget = _num(receipt["account_unit_budget"], True)
        confirmed = _num(receipt["confirmed_buy_notional"], True)
        if not initial_target <= previous <= 100 or confirmed > budget * previous / 100:
            return dict(blocked, reason="ACCOUNT_CAP_OR_OWNERSHIP_INVALID")
        if (any(quote.get(k) != v for k, v in identity.items())
                or quote.get("account_id") != receipt["account_id"]
                or quote.get("currency") != "USD"):
            return dict(blocked, reason="QUOTE_IDENTITY_MISMATCH")
        _ref(quote["source_ref"])
        stamp = _time(quote["observed_at"])
        if not 0 <= (current - stamp).total_seconds() <= 120 or stamp < _time(intent["quote_observed_at"]):
            return dict(blocked, reason="STALE_QUOTE")
        price = _num(quote["price"], True)
        # Changed prices require a new evaluation of extension, RR and risk.
        if price != _num(intent["decision"]["price"], True):
            return dict(blocked, reason="PRICE_CHANGED_REEVALUATION_REQUIRED")
        projection = project_account_target(
            execution_profile_ref=receipt["execution_profile_ref"], account_unit_budget=budget,
            target_pct=_num(intent["decision"]["target_allocation"]) * 100,
            limit_price=price, confirmed_buy_notional=confirmed,
            reserved_buy_notional=0, unknown_execution=False,
            previously_submitted_target_pct=previous)
        return dict(blocked, **{k: v for k, v in projection.items() if k not in blocked},
                    status=projection["status"], quantity=projection["quantity"],
                    intent_id=intent_id, intent_hash=intent["intent_hash"])
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
        return dict(blocked, reason="INVALID_OR_MISSING_ACCOUNT_EVIDENCE")

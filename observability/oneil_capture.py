"""Default-off, fail-open observations; never request prices or place orders."""
from datetime import datetime, timezone
from contextvars import ContextVar
from functools import wraps
import asyncio
import json
import logging
import os

_EXIT_QUEUE = ContextVar("oneil_exit_capture_queue", default=None)
_MAX_PENDING_EXITS = 1000


def _capture_failure(error):
    logging.getLogger(__name__).warning(
        "ONEIL optional capture unavailable (%s)", type(error).__name__)


def defer_exit_capture(operation):
    """Flush optional exit I/O only after an entire protection loop finishes."""
    @wraps(operation)
    async def deferred(*args, **kwargs):
        if not enabled() or _EXIT_QUEUE.get() is not None:
            return await operation(*args, **kwargs)
        pending = []
        token = _EXIT_QUEUE.set(pending)
        try:
            return await operation(*args, **kwargs)
        finally:
            _EXIT_QUEUE.reset(token)
            if pending:
                try:
                    await asyncio.to_thread(_flush_exits, pending)
                except Exception as error:
                    _capture_failure(error)
    return deferred


def _flush_exits(pending):
    for position_id, event in pending:
        _persist_exit(position_id, event)


def enabled():
    from prism_core.oneil_config import capture_enabled
    return capture_enabled() or os.getenv("ONEIL_TAPE_CAPTURE_ENABLED", "false").strip().lower() in {
        "true", "1", "yes", "on",
    }


def _tape():
    from prism_core.oneil_capture_tape import OneilCaptureTape
    from prism_core.oneil_config import load

    path = os.getenv("ONEIL_TAPE_CAPTURE_DB", load(protection_only=True)["tape_db"])
    return OneilCaptureTape(path, timeout=.05)


def capture_initial(payload):
    if not enabled():
        return None
    try:
        return _tape().ingest_capture(payload)
    except Exception as error:  # optional observation must not interrupt trading
        _capture_failure(error)
        return None


def holding_observation(*, position_id, price, scenario, source):
    """Build only known facts; observation time is NOT a quote timestamp."""
    if not enabled():
        return None
    try:
        if isinstance(scenario, str):
            scenario = json.loads(scenario)
        now = datetime.now(timezone.utc).isoformat()
        return {"position_id": position_id, "tick": {
            "occurred_at": now, "available_at": now, "price": price,
            "current_stop": (scenario or {}).get("stop_loss"),
            "stop_source_ref": source, "stop_available_at": now,
            "source_ref": source, "evidence": {}, "evidence_status": "MISSING",
            "reason_codes": ["QUOTE_TIMESTAMP_UNAVAILABLE", "CURRENT_GATES_UNAVAILABLE",
                             "MATCHED_VOLUME_UNAVAILABLE"],
        }}
    except Exception as error:
        _capture_failure(error)
        return None


def capture_holding(observation):
    if not enabled() or observation is None:
        return None
    try:
        tape = _tape()
        campaign_id = tape.campaign_id_for_position(observation["position_id"])
        if campaign_id is not None:
            return tape.append_tick(campaign_id, tape.bind_event_identity(
                campaign_id, observation["tick"]))
    except Exception as error:
        _capture_failure(error)
        return None


def capture_exit(*, position_id, price, source):
    if not enabled():
        return None
    try:
        now = datetime.now(timezone.utc).isoformat()
        event = {"occurred_at": now, "available_at": now, "price": price,
                 "source_ref": source}
        pending = _EXIT_QUEUE.get()
        if pending is not None:
            if len(pending) < _MAX_PENDING_EXITS:
                pending.append((position_id, event))
            else:
                logging.getLogger(__name__).warning("ONEIL exit capture queue full; evidence unavailable")
            return None
        return _persist_exit(position_id, event)
    except Exception as error:
        _capture_failure(error)
        return None


def _persist_exit(position_id, event):
    try:
        tape = _tape()
        campaign_id = tape.campaign_id_for_position(position_id)
        if campaign_id is not None:
            return tape.append_exit(campaign_id, tape.bind_event_identity(campaign_id, event))
    except Exception as error:
        _capture_failure(error)
        return None

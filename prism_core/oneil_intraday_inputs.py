"""Pure, fail-closed matched intraday inputs; no quote or trading authority.

The caller supplies an authoritative exchange schedule. Validation checks its
shape, not whether omitted weekdays were exchange holidays. Reconstructed bars
are never prospective evidence, even when their numerical inputs are complete.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from zoneinfo import ZoneInfo

VERSION = "oneil-intraday-inputs-v1"
STEP = timedelta(minutes=5)
NY = ZoneInfo("America/New_York")


def _time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("aware timestamp required")
    return parsed.astimezone(timezone.utc)


def _ref(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("reference required")
    return value


def _number(value, positive=False):
    number = Decimal(str(value))
    if (isinstance(value, bool) or not number.is_finite() or number < 0
            or (positive and not number)):
        raise ValueError("invalid number")
    return number


def build_intraday_inputs(*, symbol, bars, calendar, as_of, retrieved_at,
                          price_basis_ref, source_ref, kind, retrieval_started_at=None):
    """Require all 21 exact regular-session prefixes, never fill missing bars.

    ``calendar`` has ``calendar_ref`` and exactly 21 chronological ``sessions``
    with trade_date/open_at/close_at. Bars use raw provider_timestamp/OHLCV plus
    dividends and stock_splits. Timestamps identify five-minute bucket starts.
    """
    result = {"contract_version": VERSION, "symbol": symbol, "kind": kind,
              "source_ref": source_ref, "price_basis_ref": price_basis_ref,
              "as_of": as_of, "retrieved_at": retrieved_at,
              "retrieval_started_at": retrieval_started_at,
              "status": "INVALID", "reason_codes": [], "bars": [],
              "volume": None, "market_window": None,
              "usable_for_prospective": False, "input_hash": None}

    def fail(code, status="INVALID"):
        result.update(status=status, reason_codes=[code])
        return result

    try:
        for value in (symbol, price_basis_ref, source_ref):
            _ref(value)
        if kind not in ("LIVE_CAPTURE", "RECONSTRUCTED_REPLAY"):
            return fail("UNKNOWN_INPUT_KIND")
        now, retrieved = _time(as_of), _time(retrieved_at)
        if retrieved < now:
            return fail("RETRIEVED_BEFORE_AS_OF")
        if kind == "LIVE_CAPTURE":
            if retrieval_started_at is None:
                return fail("MISSING_RETRIEVAL_START", "MISSING")
            started = _time(retrieval_started_at)
            if not now <= started <= retrieved <= now + timedelta(seconds=120):
                return fail("INVALID_LIVE_CAPTURE_CLOCK")
        calendar_ref = _ref(calendar["calendar_ref"])
        sessions = calendar["sessions"]
        if len(sessions) != 21:
            return fail("EXPECTED_21_SESSIONS", "MISSING")
        periods = []
        previous = None
        for session in sessions:
            day = date.fromisoformat(session["trade_date"])
            opened, closed = _time(session["open_at"]), _time(session["close_at"])
            local_open, local_close = opened.astimezone(NY), closed.astimezone(NY)
            if (day.weekday() >= 5 or local_open.date() != day or local_close.date() != day
                    or (local_open.hour, local_open.minute, local_open.second,
                        local_open.microsecond) != (9, 30, 0, 0)
                    or local_close.hour > 16
                    or (local_close.hour == 16 and (local_close.minute or local_close.second
                                                   or local_close.microsecond))
                    or not timedelta(0) < closed - opened <= timedelta(minutes=390)
                    or (closed - opened) % STEP
                    or (previous is not None and day <= previous)):
                return fail("INVALID_EXCHANGE_SCHEDULE")
            periods.append((day.isoformat(), opened, closed))
            previous = day
        current_day, opened, closed = periods[-1]
        if not opened < now <= closed or (now - opened) % STEP:
            return fail("AS_OF_NOT_COMPLETED_REGULAR_BOUNDARY")
        elapsed = now - opened
        if any(end - start < elapsed for _, start, end in periods):
            return fail("PRIOR_SESSION_TOO_SHORT", "MISSING")
        expected = {start + STEP * index
                    for _, start, _ in periods
                    for index in range(int(elapsed / STEP))}
        period_by_date = {day: (start, end) for day, start, end in periods}
        normalized = {}
        action_unknown = False
        action_present = False
        for raw in bars:
            stamp = _time(raw["provider_timestamp"])
            # Future/current-forming and unrelated sessions cannot affect evidence.
            if stamp + STEP > now:
                continue
            period = period_by_date.get(stamp.astimezone(NY).date().isoformat())
            if period is None or not period[0] <= stamp < period[1]:
                continue
            if (stamp - period[0]) % STEP:
                return fail("MISALIGNED_REGULAR_BAR")
            # Actions anywhere in a supplied comparison session invalidate basis.
            for field in ("dividends", "stock_splits"):
                if raw.get(field) is None:
                    action_unknown = True
                elif _number(raw[field]):
                    action_present = True
            if stamp not in expected:
                continue
            if stamp in normalized:
                return fail("DUPLICATE_REGULAR_BAR")
            values = {key: _number(raw[key], key != "volume")
                      for key in ("open", "high", "low", "close", "volume")}
            if not (values["low"] <= min(values["open"], values["close"])
                    <= max(values["open"], values["close"]) <= values["high"]):
                return fail("INCONSISTENT_OHLC")
            normalized[stamp] = {key: str(value) for key, value in values.items()}
        if action_present:
            return fail("CORPORATE_ACTION_BASIS_UNVERIFIED", "MISSING")
        if action_unknown:
            return fail("CORPORATE_ACTION_EVIDENCE_MISSING", "MISSING")
        if set(normalized) != expected:
            return fail("REGULAR_PREFIX_GAP", "MISSING")
        minutes = int(elapsed.total_seconds() / 60)
        samples = []
        for day, start, _ in periods:
            total = sum((Decimal(normalized[start + STEP * index]["volume"])
                         for index in range(int(elapsed / STEP))), Decimal(0))
            samples.append({"trade_date": day, "elapsed_minutes": minutes,
                            "cumulative_volume": str(total), "complete": True,
                            "regular": True, "source_ref": source_ref})
        if not sum(Decimal(sample["cumulative_volume"]) for sample in samples[:-1]):
            return fail("MISSING_VOLUME_DENOMINATOR", "MISSING")
        result["bars"] = [{"start_at": stamp.isoformat(), "end_at": (stamp + STEP).isoformat(),
                           "close": normalized[stamp]["close"], "complete": True,
                           "regular": True, "source_ref": source_ref}
                          for stamp in sorted(expected) if opened <= stamp < now][-2:]
        result["volume"] = {"as_of": now.isoformat(), "elapsed_minutes": minutes,
                            "basis": "MATCHED_REGULAR_CUMULATIVE", "complete": True,
                            "regular": True, "calendar_ref": calendar_ref,
                            "source_ref": source_ref,
                            "cumulative_volume": samples[-1]["cumulative_volume"],
                            "expected_prior_trade_dates": [s[0] for s in periods[:-1]],
                            "samples": samples[:-1]}
        result["market_window"] = {"trade_date": current_day, "open_at": opened.isoformat(),
                                   "close_at": closed.isoformat(), "verified": True,
                                   "source_ref": calendar_ref}
        payload = {"symbol": symbol, "kind": kind, "source_ref": source_ref,
                   "price_basis_ref": price_basis_ref, "calendar_ref": calendar_ref,
                   "periods": [(day, start.isoformat(), end.isoformat())
                               for day, start, end in periods],
                   "as_of": now.isoformat(), "retrieved_at": retrieved.isoformat(),
                   "retrieval_started_at": retrieval_started_at,
                   "bars": {key.isoformat(): value for key, value in sorted(normalized.items())}}
        result["input_hash"] = hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        result.update(status="OK", usable_for_prospective=kind == "LIVE_CAPTURE")
        return result
    except KeyError:
        return fail("REQUIRED_FIELD_MISSING", "MISSING")
    except (ValueError, TypeError, AttributeError, InvalidOperation, OverflowError):
        return fail("INVALID_INPUT")

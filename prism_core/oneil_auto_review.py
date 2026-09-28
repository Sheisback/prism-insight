"""Pure, preregistered flat-base/growth research classification (not a live gate)."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from zoneinfo import ZoneInfo


VERSION = "oneil-auto-review-v1"
VALIDATED_STATUS = "VALIDATED_RULE_OUTPUT"
PASS = VALIDATED_STATUS  # Backward-compatible classification name, not a credential.


class _EvidenceError(ValueError):
    def __init__(self, status, reason):
        self.status, self.reason = status, reason


def _fail(status, reason):
    raise _EvidenceError(status, reason)


def _time(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError):
        _fail("INVALID", "INVALID_TIMESTAMP")


def _date(value):
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        _fail("INVALID", "INVALID_DATE")


def _number(value, name):
    if value is None:
        _fail("MISSING", "MISSING_" + name)
    try:
        if isinstance(value, bool):
            raise ValueError()
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError()
        return number
    except (InvalidOperation, ValueError, TypeError):
        _fail("INVALID", "INVALID_" + name)


def _result(status, reasons=(), metrics=None, **extra):
    return dict(status=status, reason_codes=list(reasons), metrics=metrics or {}, **extra)


def _source(source, symbol, cutoff, basis=None):
    if not isinstance(source, dict) or not _reference(source.get("source_ref")):
        _fail("MISSING", "MISSING_SOURCE")
    if source.get("symbol") is None:
        _fail("MISSING", "MISSING_SOURCE_SYMBOL")
    if source.get("symbol") != symbol:
        _fail("INVALID", "SOURCE_SYMBOL_MISMATCH")
    if not source.get("available_at"):
        _fail("MISSING", "MISSING_AVAILABLE_AT")
    available = _time(source["available_at"])
    if available > cutoff:
        _fail("MISSING", "SOURCE_NOT_AVAILABLE_AS_OF")
    if basis is not None and source.get("price_basis_ref") != basis:
        _fail("MISSING", "PRICE_BASIS_MISMATCH")
    return available


def _reference(value):
    return isinstance(value, str) and bool(value.strip())


def _calendar(snapshot, cutoff):
    calendar = snapshot.get("calendar")
    if not isinstance(calendar, dict) or not _reference(calendar.get("calendar_ref")):
        _fail("MISSING", "MISSING_CALENDAR")
    local_date = cutoff.astimezone(ZoneInfo("America/New_York")).date()
    sunday = local_date + timedelta(days=6 - local_date.weekday())
    if not calendar.get("coverage_end"):
        _fail("MISSING", "MISSING_CALENDAR_COVERAGE")
    if _date(calendar["coverage_end"]) < sunday:
        _fail("MISSING", "INCOMPLETE_CALENDAR_COVERAGE")
    rows = calendar.get("sessions")
    if not isinstance(rows, list) or not rows:
        _fail("MISSING", "MISSING_CALENDAR_SESSIONS")
    sessions = {}
    for row in rows:
        if not isinstance(row, dict):
            _fail("INVALID", "INVALID_CALENDAR_SESSION")
        day = _date(row.get("trade_date"))
        opened, closed = _time(row.get("open_at")), _time(row.get("close_at"))
        local_open = opened.astimezone(ZoneInfo("America/New_York"))
        local_close = closed.astimezone(ZoneInfo("America/New_York"))
        if (day in sessions or opened >= closed or day.weekday() > 4
                or local_open.date() != day or local_close.date() != day
                or (local_open.hour, local_open.minute, local_open.second, local_open.microsecond) != (9, 30, 0, 0)
                or (local_close.hour, local_close.minute, local_close.second, local_close.microsecond) > (16, 0, 0, 0)
                or closed - opened > timedelta(hours=6, minutes=30)):
            _fail("INVALID", "INVALID_CALENDAR_SESSION")
        sessions[day] = (opened, closed)
    return sessions, local_date


def _bars(source, days, sessions):
    rows = source.get("bars")
    if not isinstance(rows, list):
        _fail("MISSING", "MISSING_BARS")
    available = _time(source.get("available_at"))
    selected = {}
    wanted = set(days)
    for row in rows:
        if not isinstance(row, dict):
            _fail("INVALID", "INVALID_BAR")
        day = _date(row.get("date"))
        if day not in wanted:
            continue
        if day in selected:
            _fail("INVALID", "DUPLICATE_BAR_DATE")
        if sessions[day][1] > available:
            _fail("MISSING", "BAR_NOT_CLOSED_AT_SOURCE_OBSERVATION")
        values = {key: _number(row.get(key), key.upper())
                  for key in ("open", "high", "low", "close", "volume", "stock_splits", "dividends")}
        if (min(values[k] for k in ("open", "high", "low", "close")) <= 0
                or values["low"] > min(values["open"], values["close"])
                or values["high"] < max(values["open"], values["close"])
                or values["volume"] < 0 or values["dividends"] < 0
                or values["stock_splits"] < 0):
            _fail("INVALID", "INVALID_OHLCV_OR_ACTION")
        if values["stock_splits"] != 0:
            _fail("MISSING", "SPLIT_PRICE_BASIS_REVIEW_REQUIRED")
        selected[day] = values
    if len(selected) != len(wanted):
        _fail("MISSING", "MISSING_SCHEDULED_BAR")
    return selected


def _base(snapshot, cutoff, sessions):
    prices = snapshot.get("prices")
    _source(prices, snapshot["symbol"], cutoff, snapshot["price_basis_ref"])
    days = sorted(day for day, (_, close) in sessions.items() if close <= cutoff)
    weeks = {}
    for day in sessions:
        monday = day - timedelta(days=day.weekday())
        weeks.setdefault(monday, []).append(day)
    complete = sorted(week for week, dates in weeks.items()
                      if all(sessions[day][1] <= cutoff for day in dates))
    if not complete or not days:
        _fail("MISSING", "NO_COMPLETED_WEEK")
    # Need five calendar weeks, not merely five sparse observed week buckets.
    end_week = complete[-1]
    failures, unavailable, candidates = [], [], []
    for duration in range(5, 9):
        start_week = end_week - timedelta(weeks=duration - 1)
        required_weeks = [start_week + timedelta(weeks=i) for i in range(duration)]
        if any(week not in complete for week in required_weeks):
            unavailable.append("INSUFFICIENT_COMPLETE_WEEKS")
            continue
        base_days = sorted(day for week in required_weeks for day in weeks[week])
        prior = [day for day in days if day < base_days[0]][-60:]
        if len(prior) != 60:
            unavailable.append("INSUFFICIENT_PRIOR_60_SESSIONS")
            continue
        following = [day for day in days if day > base_days[-1]]
        try:
            bars = _bars(prices, prior + base_days + following, sessions)
        except _EvidenceError as error:
            if error.status == "INVALID":
                raise
            unavailable.append(error.reason)
            continue
        peak = max(bars[day]["high"] for day in base_days)
        low = min(bars[day]["low"] for day in base_days)
        prior_low = min(bars[day]["low"] for day in prior)
        reasons = []
        if max(bars[day]["high"] for day in weeks[start_week]) != peak:
            reasons.append("PEAK_NOT_IN_FIRST_WEEK")
        if (peak - low) / peak > Decimal("0.15"):
            reasons.append("BASE_TOO_DEEP")
        if peak / prior_low - 1 < Decimal("0.30"):
            reasons.append("INSUFFICIENT_PRIOR_ADVANCE")
        if peak < max(bars[day]["high"] for day in prior):
            reasons.append("BASE_BELOW_PRIOR_HIGH")
        if bars[prior[-1]]["close"] < bars[prior[0]]["close"]:
            reasons.append("PRIOR_TREND_DECLINING")
        if any(bars[day]["low"] < low for day in following):
            reasons.append("BASE_LOW_BROKEN_AFTER_END")
        candidates.append({"weeks": duration, "start_date": str(base_days[0]),
                           "end_date": str(base_days[-1]), "depth": str((peak-low)/peak),
                           "prior_advance": str(peak/prior_low-1), "reason_codes": reasons})
        if reasons:
            failures.extend(reasons)
            continue
        return _result(VALIDATED_STATUS, metrics={
            "weeks": duration, "start_date": str(base_days[0]), "end_date": str(base_days[-1]),
            "latest_completed_date": str(days[-1]), "base_high": str(peak), "base_low": str(low),
            "depth": str((peak - low) / peak), "prior_advance": str(peak / prior_low - 1),
            "return_basis": "PRICE_ONLY", "dividends_present": any(b["dividends"] for b in bars.values()),
        }, pivot=str(peak + Decimal("0.10")))
    if unavailable:
        return _result("MISSING", sorted(set(unavailable)), metrics={"candidates": candidates}, pivot=None)
    return _result("REJECTED", sorted(set(failures)), metrics={"candidates": candidates}, pivot=None)


def _volatility(snapshot, cutoff, sessions, local_date):
    """ATR14 over the 14 scheduled sessions strictly before the review's NY date.

    True range needs the previous close, so 15 completed daily bars are required.
    Each bar must have closed before the price source observation (see _bars).
    """
    prices = snapshot.get("prices")
    _source(prices, snapshot["symbol"], cutoff, snapshot["price_basis_ref"])
    days = sorted(day for day, (_, close) in sessions.items() if day < local_date and close <= cutoff)[-15:]
    if len(days) != 15:
        _fail("MISSING", "INSUFFICIENT_15_SESSIONS")
    bars = _bars(prices, days, sessions)
    ranges = []
    for previous, day in zip(days, days[1:]):
        high, low, prior = bars[day]["high"], bars[day]["low"], bars[previous]["close"]
        ranges.append(max(high - low, abs(high - prior), abs(low - prior)))
    atr = (sum(ranges) / 14).quantize(Decimal("0.000001"))
    if atr <= 0:
        _fail("INVALID", "NONPOSITIVE_ATR")
    return _result(VALIDATED_STATUS, metrics={"start_date": str(days[1]), "end_date": str(days[-1]),
                                              "true_ranges": 14, "basis": "UNADJUSTED_DAILY_TRUE_RANGE"},
                   atr14=str(atr), last_trade_date=str(days[-1]), source_ref=prices["source_ref"])


def _leadership(snapshot, cutoff, sessions, local_date):
    financials = snapshot.get("financials")
    _source(financials, snapshot["symbol"], cutoff)
    expected = {"statement_kind": "QUARTERLY_ACTUAL", "eps_basis": "PROVIDER_DILUTED_EPS",
                "revenue_basis": "PROVIDER_TOTAL_REVENUE", "accounting_scope": "PROVIDER_STATEMENT_SAME_SCOPE"}
    if any(financials.get(key) != value for key, value in expected.items()):
        _fail("MISSING", "UNCONFIRMED_COMPARABLE_ACTUAL_FINANCIALS")
    currency = financials.get("currency")
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isascii() or not currency.isalpha() or not currency.isupper():
        _fail("MISSING", "MISSING_FINANCIAL_CURRENCY")
    availability_basis = financials.get("availability_basis")
    if not isinstance(availability_basis, str) or availability_basis not in {
        "PUBLICATION_TIMESTAMP", "CURRENT_PROVIDER_OBSERVATION_NOT_PUBLICATION_DATE"
    }:
        _fail("MISSING", "UNCONFIRMED_FINANCIAL_AVAILABILITY")
    records = financials.get("records")
    if not isinstance(records, list) or not records:
        _fail("MISSING", "MISSING_QUARTERLY_RECORDS")
    quarters = {}
    observed_date = _time(financials["available_at"]).astimezone(ZoneInfo("America/New_York")).date()
    for row in records:
        if not isinstance(row, dict):
            _fail("INVALID", "INVALID_QUARTER")
        period = _date(row.get("period_end"))
        if period in quarters:
            _fail("INVALID", "DUPLICATE_QUARTER")
        if period > min(local_date, observed_date):
            _fail("INVALID", "FUTURE_ACTUAL_QUARTER")
        # A row-level contradiction must not be hidden by dataset-level metadata.
        for key, expected_value in {**expected, "currency": financials["currency"]}.items():
            if key in row and row[key] != expected_value:
                _fail("MISSING", "MIXED_FINANCIAL_SCOPE")
        quarters[period] = row
    latest = max(quarters)
    if (local_date - latest).days > 180:
        _fail("MISSING", "STALE_QUARTER")
    comparisons = [period for period in quarters if 358 <= (latest - period).days <= 372]
    if len(comparisons) != 1:
        _fail("MISSING", "NONUNIQUE_PRIOR_YEAR_QUARTER")
    current, prior = quarters[latest], quarters[comparisons[0]]
    growth, operands = {}, {}
    for key in ("eps", "revenue"):
        numerator, denominator = _number(current.get(key), key.upper()), _number(prior.get(key), key.upper())
        if denominator <= 0:
            _fail("MISSING", "NONPOSITIVE_PRIOR_" + key.upper())
        growth[key] = numerator / denominator - 1
        operands[key] = {"current": str(numerator), "prior": str(denominator)}
    prices, benchmark = snapshot.get("prices"), snapshot.get("benchmark")
    _source(prices, snapshot["symbol"], cutoff, snapshot["price_basis_ref"])
    _source(benchmark, "SPY", cutoff, snapshot["price_basis_ref"])
    days = sorted(day for day, (_, close) in sessions.items() if close <= cutoff)[-61:]
    if len(days) != 61:
        _fail("MISSING", "INSUFFICIENT_61_SESSIONS")
    bars, reference = _bars(prices, days, sessions), _bars(benchmark, days, sessions)
    stock_return = bars[days[-1]]["close"] / bars[days[0]]["close"] - 1
    spy_return = reference[days[-1]]["close"] / reference[days[0]]["close"] - 1
    excess = stock_return - spy_return
    reasons = []
    for key, value in growth.items():
        if value < Decimal("0.25"):
            reasons.append(key.upper() + "_GROWTH_BELOW_25_PERCENT")
    if excess <= 0:
        reasons.append("NO_POSITIVE_SPY_EXCESS_RETURN")
    return _result("REJECTED" if reasons else VALIDATED_STATUS, reasons, {
        "period_end": str(latest), "prior_period_end": str(comparisons[0]),
        "eps_growth": str(growth["eps"]), "revenue_growth": str(growth["revenue"]),
        "growth_operands": operands,
        "stock_return_60": str(stock_return), "spy_return_60": str(spy_return),
        "excess_return_60": str(excess), "return_basis": "PRICE_ONLY",
        "start_date": str(days[0]), "end_date": str(days[-1]),
        "dividends_present": any(b["dividends"] for b in (*bars.values(), *reference.values())),
    })


def evaluate_auto_review(snapshot, *, as_of):
    """Return auditable research classifications; never infer absent evidence."""
    serialized = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
    output = {"version": VERSION, "status": "INVALID", "as_of": str(as_of),
              "input_hash": hashlib.sha256(serialized.encode()).hexdigest(),
              "source_refs": {}, "data_as_of": {}}
    try:
        cutoff = _time(as_of)
        if not isinstance(snapshot, dict):
            _fail("INVALID", "INVALID_SNAPSHOT")
        if snapshot.get("contract_version") != "oneil-auto-review-input-v1":
            _fail("INVALID", "INPUT_CONTRACT_MISMATCH")
        if snapshot.get("market") != "US":
            _fail("INVALID", "UNSUPPORTED_MARKET")
        for field in ("symbol", "decision_ref", "price_basis_ref"):
            if not isinstance(snapshot.get(field), str) or not snapshot[field].strip():
                _fail("MISSING", "MISSING_" + field.upper())
        for key in ("prices", "benchmark", "financials"):
            source = snapshot.get(key)
            if isinstance(source, dict):
                if _reference(source.get("source_ref")):
                    output["source_refs"][key] = source["source_ref"]
                if isinstance(source.get("available_at"), str):
                    output["data_as_of"][key] = source["available_at"]
        sessions, local_date = _calendar(snapshot, cutoff)
        output["source_refs"]["calendar"] = snapshot["calendar"]["calendar_ref"]
        for name, evaluator, args in (("base", _base, (snapshot, cutoff, sessions)),
                                     ("leadership", _leadership, (snapshot, cutoff, sessions, local_date))):
            try:
                output[name] = evaluator(*args)
            except _EvidenceError as error:
                output[name] = _result(error.status, [error.reason], **({"pivot": None} if name == "base" else {}))
        statuses = {output[name]["status"] for name in ("base", "leadership")}
        output["status"] = next((status for status in ("INVALID", "MISSING", "REJECTED") if status in statuses), VALIDATED_STATUS)
        # Plan-sizing evidence only; it never changes the setup classification.
        try:
            output["volatility"] = _volatility(snapshot, cutoff, sessions, local_date)
        except _EvidenceError as error:
            output["volatility"] = _result(error.status, [error.reason], atr14=None)
    except _EvidenceError as error:
        output["status"] = error.status
        output["base"] = _result(error.status, [error.reason], pivot=None)
        output["leadership"] = _result(error.status, [error.reason])
        output["volatility"] = _result(error.status, [error.reason], atr14=None)
    return output

"""Frozen classifier counterexamples; no provider or trading side effects."""

from copy import deepcopy
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from prism_core.oneil_auto_review import PASS, evaluate_auto_review


AS_OF = "2026-09-27T12:00:00Z"


def valid_snapshot():
    """Synthetic known calendar: intentional fixture, not actual exchange evidence."""
    days = []
    day = date(2026, 2, 2)
    while day <= date(2026, 9, 25):
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    base_start = len(days) - 25
    prices, spy, sessions = [], [], []
    for i, day in enumerate(days):
        sessions.append({"trade_date": str(day),
                         "open_at": datetime(day.year, day.month, day.day, 9, 30, tzinfo=ZoneInfo("America/New_York")).isoformat(),
                         "close_at": datetime(day.year, day.month, day.day, 16, tzinfo=ZoneInfo("America/New_York")).isoformat()})
        close = 30 + 69 * i / (base_start - 1) if i < base_start else 100
        high = close + 1 if i < base_start else (105 if i < base_start + 5 else 104)
        prices.append({"date": str(day), "open": close, "high": high, "low": close - 1,
                       "close": close, "volume": 10000, "stock_splits": 0, "dividends": 0})
        spy.append({"date": str(day), "open": 400, "high": 401, "low": 399,
                    "close": 400, "volume": 100000, "stock_splits": 0, "dividends": 0})
    return {
        "contract_version": "oneil-auto-review-input-v1", "market": "US", "symbol": "TEST",
        "decision_ref": "decision-fixture", "price_basis_ref": "unadjusted-price-v1",
        "calendar": {"calendar_ref": "synthetic-weekdays", "coverage_end": "2026-09-27", "sessions": sessions},
        "prices": {"symbol": "TEST", "source_ref": "test-prices", "available_at": AS_OF,
                   "price_basis_ref": "unadjusted-price-v1", "bars": prices},
        "benchmark": {"symbol": "SPY", "source_ref": "test-spy", "available_at": AS_OF,
                      "price_basis_ref": "unadjusted-price-v1", "bars": spy},
        "financials": {"symbol": "TEST", "source_ref": "test-financials", "available_at": AS_OF,
                       "availability_basis": "CURRENT_PROVIDER_OBSERVATION_NOT_PUBLICATION_DATE",
                       "statement_kind": "QUARTERLY_ACTUAL", "currency": "USD",
                       "eps_basis": "PROVIDER_DILUTED_EPS", "revenue_basis": "PROVIDER_TOTAL_REVENUE",
                       "accounting_scope": "PROVIDER_STATEMENT_SAME_SCOPE",
                       "records": [{"period_end": "2026-06-30", "eps": "1.25", "revenue": "125"},
                                   {"period_end": "2025-06-30", "eps": "1", "revenue": "100"}]},
    }


def run(snapshot=None, as_of=AS_OF):
    return evaluate_auto_review(snapshot or valid_snapshot(), as_of=as_of)


def test_valid_deterministic_no_mutation():
    snapshot = valid_snapshot()
    original = deepcopy(snapshot)
    result = run(snapshot)
    assert result["status"] == PASS
    assert result["base"]["pivot"] == "105.10"
    assert result["base"]["metrics"]["weeks"] == 5
    assert result["leadership"]["metrics"]["eps_growth"] == "0.25"
    assert result == run(snapshot)
    assert snapshot == original


@pytest.mark.parametrize("source", ["prices", "benchmark", "financials"])
def test_future_source_not_backdated(source):
    snapshot = valid_snapshot()
    snapshot[source]["available_at"] = "2026-09-28T12:00:00Z"
    assert run(snapshot)["status"] == "MISSING"


@pytest.mark.parametrize("key,value,reason", [
    ("statement_kind", "ANNUAL_ACTUAL", "UNCONFIRMED_COMPARABLE_ACTUAL_FINANCIALS"),
    ("statement_kind", "QUARTERLY_FORECAST", "UNCONFIRMED_COMPARABLE_ACTUAL_FINANCIALS"),
    ("eps_basis", "BASIC_EPS", "UNCONFIRMED_COMPARABLE_ACTUAL_FINANCIALS"),
    ("accounting_scope", None, "UNCONFIRMED_COMPARABLE_ACTUAL_FINANCIALS"),
    ("availability_basis", "FISCAL_PERIOD_END", "UNCONFIRMED_FINANCIAL_AVAILABILITY"),
    ("currency", None, "MISSING_FINANCIAL_CURRENCY"),
])
def test_financial_contract(key, value, reason):
    snapshot = valid_snapshot()
    snapshot["financials"][key] = value
    result = run(snapshot)["leadership"]
    assert result["status"] == "MISSING"
    assert reason in result["reason_codes"]


@pytest.mark.parametrize("key,value,status", [("eps", "1.249999", "REJECTED"), ("revenue", "124.9999", "REJECTED"),
                                              ("eps", "NaN", "INVALID"), ("revenue", "Infinity", "INVALID"),
                                              ("eps", None, "MISSING")])
def test_growth_boundaries(key, value, status):
    snapshot = valid_snapshot()
    snapshot["financials"]["records"][0][key] = value
    assert run(snapshot)["leadership"]["status"] == status


@pytest.mark.parametrize("denominator", [0, -1])
def test_nonpositive_denominator_not_growth(denominator):
    snapshot = valid_snapshot()
    snapshot["financials"]["records"][1]["eps"] = denominator
    assert run(snapshot)["leadership"]["status"] == "MISSING"


def test_future_actual_and_stale_quarter():
    snapshot = valid_snapshot()
    snapshot["financials"]["records"][0]["period_end"] = "2026-09-30"
    assert run(snapshot)["leadership"]["status"] == "INVALID"
    snapshot["financials"]["records"][0]["period_end"] = "2025-12-31"
    assert "STALE_QUARTER" in run(snapshot)["leadership"]["reason_codes"]


def test_duplicate_and_ambiguous_quarters():
    snapshot = valid_snapshot()
    snapshot["financials"]["records"].append(deepcopy(snapshot["financials"]["records"][1]))
    assert run(snapshot)["leadership"]["status"] == "INVALID"
    snapshot["financials"]["records"][-1]["period_end"] = "2025-07-01"
    assert "NONUNIQUE_PRIOR_YEAR_QUARTER" in run(snapshot)["leadership"]["reason_codes"]


def test_mixed_row_scope():
    snapshot = valid_snapshot()
    snapshot["financials"]["records"][0]["statement_kind"] = "QUARTERLY_FORECAST"
    assert "MIXED_FINANCIAL_SCOPE" in run(snapshot)["leadership"]["reason_codes"]


@pytest.mark.parametrize("field,value,status", [("close", "NaN", "INVALID"), ("volume", -1, "INVALID"),
                                               ("stock_splits", 2, "MISSING"), ("stock_splits", None, "MISSING"),
                                               ("dividends", None, "MISSING"), ("low", 200, "INVALID")])
def test_selected_bar_quality(field, value, status):
    snapshot = valid_snapshot()
    snapshot["prices"]["bars"][-1][field] = value
    assert run(snapshot)["status"] == status


def test_missing_duplicate_benchmark_dates():
    snapshot = valid_snapshot()
    snapshot["benchmark"]["bars"].pop(-2)
    assert run(snapshot)["leadership"]["status"] == "MISSING"
    snapshot = valid_snapshot()
    snapshot["prices"]["bars"].append(deepcopy(snapshot["prices"]["bars"][-1]))
    assert run(snapshot)["status"] == "INVALID"


def test_known_dividend_annotated_price_only():
    snapshot = valid_snapshot()
    snapshot["prices"]["bars"][-1]["dividends"] = 1
    result = run(snapshot)
    assert result["status"] == PASS
    assert result["base"]["metrics"]["dividends_present"] is True
    assert result["leadership"]["metrics"]["return_basis"] == "PRICE_ONLY"


def test_incomplete_week_and_future_bar_ignored():
    snapshot = valid_snapshot()
    for row in snapshot["prices"]["bars"][-30:-25]:
        row.update(open=100, high=105, low=99, close=100)
    # Friday's scheduled session is not complete at Thursday's evaluation.
    for source in ("prices", "benchmark", "financials"):
        snapshot[source]["available_at"] = "2026-09-24T21:00:00Z"
    result = run(snapshot, "2026-09-24T21:00:00Z")
    assert result["base"]["status"] == PASS
    assert result["base"]["metrics"]["end_date"] == "2026-09-18"
    snapshot["prices"]["bars"][-1]["close"] = "NaN"
    changed = run(snapshot, "2026-09-24T21:00:00Z")
    assert changed["base"] == result["base"]
    assert changed["leadership"] == result["leadership"]


def test_holiday_not_missing_and_calendar_coverage():
    snapshot = valid_snapshot()
    holiday = "2026-09-07"
    snapshot["calendar"]["sessions"] = [r for r in snapshot["calendar"]["sessions"] if r["trade_date"] != holiday]
    for source in ("prices", "benchmark"):
        snapshot[source]["bars"] = [r for r in snapshot[source]["bars"] if r["date"] != holiday]
    assert run(snapshot)["status"] == PASS
    snapshot["calendar"]["coverage_end"] = "2026-09-25"
    assert "INCOMPLETE_CALENDAR_COVERAGE" in run(snapshot)["base"]["reason_codes"]


def test_no_intraday_completion_at_source_time():
    snapshot = valid_snapshot()
    snapshot["prices"]["available_at"] = "2026-09-25T19:59:59Z"
    assert "BAR_NOT_CLOSED_AT_SOURCE_OBSERVATION" in run(snapshot)["base"]["reason_codes"]


def test_continuously_rising_not_flat_base():
    snapshot = valid_snapshot()
    for i, row in enumerate(snapshot["prices"]["bars"]):
        row.update(open=100 + i, high=101 + i, low=99 + i, close=100 + i)
    result = run(snapshot)["base"]
    assert result["status"] == "REJECTED"
    assert "PEAK_NOT_IN_FIRST_WEEK" in result["reason_codes"]


def test_deep_base_rejected():
    snapshot = valid_snapshot()
    snapshot["prices"]["bars"][-1]["low"] = 80
    assert "BASE_TOO_DEEP" in run(snapshot)["base"]["reason_codes"]


def test_prior_decline_rejected():
    snapshot = valid_snapshot()
    for row in snapshot["prices"]["bars"][:-25]:
        row.update(open=110, high=111, low=70, close=110)
    assert "BASE_BELOW_PRIOR_HIGH" in run(snapshot)["base"]["reason_codes"]


def test_short_history_missing():
    snapshot = valid_snapshot()
    snapshot["calendar"]["sessions"] = snapshot["calendar"]["sessions"][-20:]
    assert run(snapshot)["status"] == "MISSING"


def test_zero_excess_is_rejected():
    snapshot = valid_snapshot()
    snapshot["benchmark"]["bars"] = deepcopy(snapshot["prices"]["bars"])
    assert "NO_POSITIVE_SPY_EXCESS_RETURN" in run(snapshot)["leadership"]["reason_codes"]


def test_future_financials_cannot_be_used_historically():
    assert run(as_of="2026-08-30T12:00:00Z")["status"] == "MISSING"


@pytest.mark.parametrize("as_of", ["2026-09-27", "not-a-date", None])
def test_invalid_asof(as_of):
    assert run(as_of=as_of)["status"] == "INVALID"


def test_new_week_start_does_not_create_a_complete_week():
    snapshot = valid_snapshot()
    for day in (date(2026, 9, 28) + timedelta(days=i) for i in range(5)):
        snapshot["calendar"]["sessions"].append({
            "trade_date": str(day), "open_at": f"{day}T13:30:00Z", "close_at": f"{day}T20:00:00Z"})
    snapshot["calendar"]["coverage_end"] = "2026-10-04"
    result = run(snapshot, "2026-09-28T13:00:00Z")
    assert result["status"] == PASS
    assert result["base"]["metrics"]["end_date"] == "2026-09-25"


def test_intervening_bar_breaks_base_low():
    snapshot = valid_snapshot()
    day = "2026-09-28"
    snapshot["calendar"]["coverage_end"] = "2026-10-04"
    for i in range(5):
        day = str(date(2026, 9, 28) + timedelta(days=i))
        snapshot["calendar"]["sessions"].append({"trade_date": day, "open_at": f"{day}T13:30:00Z", "close_at": f"{day}T20:00:00Z"})
    for source in ("prices", "benchmark", "financials"):
        snapshot[source]["available_at"] = "2026-09-28T21:00:00Z"
    for source in ("prices", "benchmark"):
        row = deepcopy(snapshot[source]["bars"][-1])
        row["date"] = "2026-09-28"
        if source == "prices":
            row["low"] = 98
        snapshot[source]["bars"].append(row)
    result = run(snapshot, "2026-09-28T21:00:00Z")
    assert "BASE_LOW_BROKEN_AFTER_END" in result["base"]["reason_codes"]


@pytest.mark.parametrize("depth_low,status", [("89.25", PASS), ("89.24999", "REJECTED")])
def test_fifteen_percent_depth_boundary(depth_low, status):
    snapshot = valid_snapshot()
    snapshot["prices"]["bars"][-1]["low"] = depth_low
    assert run(snapshot)["base"]["status"] == status


def test_selected_prior_split_also_requires_review():
    snapshot = valid_snapshot()
    snapshot["prices"]["bars"][-70]["stock_splits"] = 2
    assert "SPLIT_PRICE_BASIS_REVIEW_REQUIRED" in run(snapshot)["base"]["reason_codes"]


def test_bar_gap_does_not_collapse_time_window():
    snapshot = valid_snapshot()
    snapshot["prices"]["bars"].pop(-70)
    assert "MISSING_SCHEDULED_BAR" in run(snapshot)["base"]["reason_codes"]


@pytest.mark.parametrize("offset,status", [(358, PASS), (372, PASS), (357, "MISSING"), (373, "MISSING")])
def test_fiscal_calendar_year_boundary(offset, status):
    snapshot = valid_snapshot()
    snapshot["financials"]["records"][1]["period_end"] = str(date(2026, 6, 30) - timedelta(days=offset))
    assert run(snapshot)["leadership"]["status"] == status


def test_duplicate_session_invalid():
    snapshot = valid_snapshot()
    snapshot["calendar"]["sessions"].append(deepcopy(snapshot["calendar"]["sessions"][-1]))
    assert run(snapshot)["status"] == "INVALID"


def test_mismatched_basis_and_symbol_fail_closed():
    snapshot = valid_snapshot()
    snapshot["prices"]["price_basis_ref"] = "adjusted-other"
    assert run(snapshot)["status"] == "MISSING"
    snapshot = valid_snapshot()
    snapshot["financials"]["symbol"] = "OTHER"
    assert run(snapshot)["status"] == "INVALID"


@pytest.mark.parametrize("field,value", [("open_at", "2026-09-25T14:30:00Z"),
                                        ("close_at", "2026-09-25T20:00:01Z"),
                                        ("close_at", "2026-09-26T01:00:00Z")])
def test_malformed_session_clock(field, value):
    snapshot = valid_snapshot()
    snapshot["calendar"]["sessions"][-1][field] = value
    assert run(snapshot)["status"] == "INVALID"


@pytest.mark.parametrize("value", [True, {}, {"canary": "do-not-copy"}, [], None, " "])
@pytest.mark.parametrize("source", ["prices", "benchmark", "financials", "calendar"])
def test_source_ref_type_does_not_leak(value, source):
    snapshot = valid_snapshot()
    snapshot[source]["calendar_ref" if source == "calendar" else "source_ref"] = value
    result = run(snapshot)
    assert result["status"] == "MISSING"
    assert source not in result["source_refs"]
    assert "do-not-copy" not in str(result)


@pytest.mark.parametrize("currency", [True, [], {}, "usd", "US", "123", "ÜSD"])
def test_invalid_currency(currency):
    snapshot = valid_snapshot()
    snapshot["financials"]["currency"] = currency
    assert run(snapshot)["leadership"]["status"] == "MISSING"


@pytest.mark.parametrize("source,field", [("calendar", "sessions"), ("prices", "bars"),
                                         ("benchmark", "bars"), ("financials", "records")])
@pytest.mark.parametrize("value", [None, {}, True, [None]])
def test_malformed_collections_do_not_raise(source, field, value):
    snapshot = valid_snapshot()
    snapshot[source][field] = value
    assert run(snapshot)["status"] in {"MISSING", "INVALID"}


@pytest.mark.parametrize("value", [None, {}, [], True])
def test_malformed_availability_basis(value):
    snapshot = valid_snapshot()
    snapshot["financials"]["availability_basis"] = value
    assert run(snapshot)["leadership"]["status"] == "MISSING"

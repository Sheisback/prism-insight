"""Opt-in report-side numeric evidence; never a prompt, order or entry gate."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from numbers import Real
import os
from pathlib import Path
import tempfile

from prism_core.oneil_auto_review_output import build_review_bundle

BASIS = "yfinance-unadjusted-5m-actions-checked-v1"


def enabled():
    return os.getenv("ONEIL_AUTO_REVIEW_CAPTURE_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def frame_source(frame, symbol):
    import pandas as pd

    bars = []
    if frame is not None and not frame.empty and frame.columns.is_unique:
        for date, row in frame.iterrows():
            timestamp = pd.Timestamp(date)
            if timestamp.tzinfo is not None:
                timestamp = timestamp.tz_convert("America/New_York")
            record = {"date": timestamp.date().isoformat()}
            for key in (
                "open",
                "high",
                "low",
                "close",
                "volume",
                "stock_splits",
                "dividends",
            ):
                value = row.get(key)
                try:
                    record[key] = (
                        float(value)
                        if isinstance(value, Real)
                        and not isinstance(value, bool)
                        and math.isfinite(value)
                        else None
                    )
                except (ValueError, OverflowError):
                    record[key] = None
            bars.append(record)
    result = dict(symbol=symbol, available_at=now(), price_basis_ref=BASIS, bars=bars)
    result["source_ref"] = digest(result)
    return result


def financial_frame_records(frame):
    """Exact actual rows only; no basic EPS, estimates, TTM or invented quarters."""
    if (
        frame is None
        or frame.empty
        or not frame.index.is_unique
        or not frame.columns.is_unique
    ):
        return []
    records = []
    for column in frame.columns:
        if not isinstance(column, datetime):
            return []
        record = {"period_end": column.date().isoformat()}
        for output, label in [("eps", "Diluted EPS"), ("revenue", "Total Revenue")]:
            value = frame.loc[label, column] if label in frame.index else None
            try:
                record[output] = (
                    float(value)
                    if isinstance(value, Real)
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                    else None
                )
            except (ValueError, OverflowError):
                record[output] = None
        records.append(record)
    return records


def financial_source(frame, symbol):

    return dict(
        symbol=symbol, available_at=now(), records=financial_frame_records(frame)
    )


def assemble_batch_source(
    symbol, prices, financials, company, *, benchmark_fetcher=None
):
    """Reuse target sources; one bounded public SPY fetch only in report prefetch."""
    import pandas_market_calendars as calendars
    from tools.collect_trend_replay_data import EXCHANGES, fetch, stamp

    observed = datetime.now(timezone.utc)
    end = observed.date() + timedelta(days=7)
    start = observed.date() - timedelta(days=400)
    calendar_name = EXCHANGES.get(company.get("exchange"))
    calendar = {}
    if calendar_name:
        schedule = calendars.get_calendar(calendar_name).schedule(start, end)
        calendar = dict(
            coverage_end=end.isoformat(),
            sessions=[
                dict(
                    trade_date=day.date().isoformat(),
                    open_at=row["market_open"].isoformat(),
                    close_at=row["market_close"].isoformat(),
                )
                for day, row in schedule.iterrows()
            ],
        )
        calendar["calendar_ref"] = digest(
            [calendar_name, calendars.__version__, calendar]
        )
    response = (benchmark_fetcher or fetch)(
        dict(
            market="US",
            ticker="SPY",
            interval="1d",
            start=start.isoformat() + "T00:00:00Z",
            end=observed.isoformat(),
        )
    )
    rows = []
    if (
        response.get("status") == "received"
        and EXCHANGES.get(response.get("exchange")) == "NYSE"
    ):
        from zoneinfo import ZoneInfo

        for row in response.get("raw_rows", []):
            rows.append(
                dict(
                    date=stamp(row["provider_timestamp"])
                    .astimezone(ZoneInfo("America/New_York"))
                    .date()
                    .isoformat(),
                    **{
                        key: row.get(key)
                        for key in (
                            "open",
                            "high",
                            "low",
                            "close",
                            "volume",
                            "stock_splits",
                            "dividends",
                        )
                    },
                )
            )
    benchmark = dict(
        symbol="SPY",
        source_ref=digest(response),
        available_at=response.get("retrieved_at"),
        price_basis_ref=BASIS,
        bars=rows,
    )
    financial = deepcopy(financials or {})
    financial.update(
        symbol=company.get("provider_symbol"),
        currency=company.get("financial_currency"),
        availability_basis="CURRENT_PROVIDER_OBSERVATION_NOT_PUBLICATION_DATE",
        statement_kind="QUARTERLY_ACTUAL",
        eps_basis="PROVIDER_DILUTED_EPS",
        revenue_basis="PROVIDER_TOTAL_REVENUE",
        accounting_scope="PROVIDER_STATEMENT_SAME_SCOPE",
    )
    financial["source_ref"] = digest([financial, company])
    prices = deepcopy(prices or {})
    if (
        company.get("provider_currency") != "USD"
        or company.get("provider_symbol") != symbol
    ):
        prices["bars"] = []
    return dict(
        snapshot=dict(
            contract_version="oneil-auto-review-input-v1",
            market="US",
            symbol=symbol,
            decision_ref="UNBOUND_REPORT",
            price_basis_ref=BASIS,
            calendar=calendar,
            prices=prices,
            financials=financial,
            benchmark=benchmark,
        ),
        reviewed_at=now(),
    )


def sidecar_path(path):
    path = Path(path)
    return path.with_suffix(".md.oneil.json" if path.suffix == ".md" else ".oneil.json")


def _write_exclusive(path, payload):
    encoded = json.dumps(payload, sort_keys=True, allow_nan=False).encode()
    if len(encoded) > 8_000_000:
        raise ValueError("oversized_sidecar")
    fd, temporary = tempfile.mkstemp(prefix=".oneil-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        # Atomic no-clobber publication, including concurrent writers.
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


def write_sidecar(report_path, source):
    """Exclusive creation; a stale/mismatched sidecar is never silently overwritten."""
    path = Path(report_path)
    snapshot = deepcopy(source["snapshot"])
    snapshot["decision_ref"] = "report:" + path.with_suffix(".pdf").name
    payload = dict(
        contract_version="oneil-batch-setup-v1",
        symbol=snapshot["symbol"],
        decision_ref=snapshot["decision_ref"],
        source=dict(snapshot=snapshot, reviewed_at=source["reviewed_at"]),
        report_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        report_filename=path.name,
    )
    payload["bundle"] = build_review_bundle(snapshot, reviewed_at=source["reviewed_at"])
    payload["sha256"] = digest(payload)
    _write_exclusive(sidecar_path(path), payload)


def bind_pdf_sidecar(markdown_path, pdf_path):
    original = Path(markdown_path)
    payload = json.loads(sidecar_path(original).read_text())
    checksum = payload.pop("sha256")
    if (
        digest(payload) != checksum
        or payload["report_sha256"] != hashlib.sha256(original.read_bytes()).hexdigest()
    ):
        raise ValueError("report_sidecar_mismatch")
    if payload["decision_ref"] != "report:" + Path(pdf_path).name:
        raise ValueError("report_identity_mismatch")
    payload["pdf_sha256"] = hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest()
    payload["sha256"] = digest(payload)
    _write_exclusive(sidecar_path(pdf_path), payload)


def load_review_sidecar(pdf_path, *, symbol, decision_ref, as_of):
    """Read-only fail-closed lookup; no collection, re-dating or runtime policy."""
    from prism_core.oneil_setup_inputs import _time

    try:
        path = sidecar_path(pdf_path)
        if (
            path.is_symlink()
            or Path(pdf_path).is_symlink()
            or path.stat().st_size > 8_000_000
            or Path(pdf_path).stat().st_size > 50_000_000
        ):
            raise ValueError("oversized_sidecar")
        payload = json.loads(path.read_text())
        checksum = payload.pop("sha256")
        snapshot = payload["source"]["snapshot"]
        if (
            digest(payload) != checksum
            or payload["contract_version"] != "oneil-batch-setup-v1"
            or payload["symbol"] != symbol
            or payload["decision_ref"] != decision_ref
            or snapshot["symbol"] != symbol
            or snapshot["decision_ref"] != decision_ref
            or snapshot["market"] != "US"
            or decision_ref != "report:" + Path(pdf_path).name
            or payload["pdf_sha256"]
            != hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest()
            or _time(payload["source"]["reviewed_at"]) > _time(as_of)
        ):
            raise ValueError("sidecar_identity_or_time_mismatch")
        bundle = build_review_bundle(
            payload["source"]["snapshot"], reviewed_at=payload["source"]["reviewed_at"]
        )
        if bundle != payload["bundle"]:
            raise ValueError("sidecar_calculation_mismatch")
        return dict(
            contract_version="oneil-batch-linked-review-v1",
            market="US",
            symbol=symbol,
            decision_ref=decision_ref,
            report_sha256=payload["pdf_sha256"],
            status="OK",
            reason_codes=[],
            source_sha256=checksum,
            bundle=bundle,
        )
    except Exception:
        return dict(
            contract_version="oneil-batch-linked-review-v1",
            market="US",
            symbol=symbol,
            decision_ref=decision_ref,
            status="MISSING",
            reason_codes=["SIDECAR_UNAVAILABLE_OR_INVALID"],
        )

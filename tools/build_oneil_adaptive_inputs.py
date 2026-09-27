"""Read-only, bounded public-price input collection. Never a trading runner.

Default is retrospective reconstruction. No quote is inferred from a bar close;
setup review, current quote and admission gates are separate input authorities.
"""

import argparse
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prism_core.oneil_intraday_inputs import build_intraday_inputs  # noqa: E402
from prism_core.oneil_setup_inputs import build_setup_input  # noqa: E402
from tools.collect_trend_replay_data import EXCHANGES, fetch, iso, stamp  # noqa: E402

CONTRACT = "oneil-adaptive-input-packet-v1"
BASIS = "yfinance-unadjusted-5m-actions-checked-v1"


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def build_calendar(name, as_of):
    import pandas_market_calendars as calendars

    target = stamp(as_of).astimezone(ZoneInfo("America/New_York")).date()
    schedule = calendars.get_calendar(name).schedule(
        target - timedelta(days=60), target
    )
    selected = schedule.tail(21)
    if len(selected) != 21 or selected.index[-1].date() != target:
        raise ValueError("TARGET_OR_PRIOR_TRADING_DATES_UNAVAILABLE")
    days = [
        dict(
            trade_date=day.date().isoformat(),
            open_at=iso(row["market_open"]),
            close_at=iso(row["market_close"]),
        )
        for day, row in selected.iterrows()
    ]
    provenance = dict(calendar=name, version=calendars.__version__, sessions=days)
    return dict(
        calendar_ref=digest(provenance),
        sessions=days,
        provider="pandas_market_calendars",
        provider_version=calendars.__version__,
        name=name,
    )


def collect_source(symbol, as_of, calendar_name, fetcher=fetch):
    if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", symbol) or calendar_name not in {
        "NYSE",
        "NASDAQ",
    }:
        raise ValueError("UNSUPPORTED_SYMBOL_OR_CALENDAR")
    calendar = build_calendar(calendar_name, as_of)
    cursor = stamp(calendar["sessions"][0]["open_at"]).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end = stamp(as_of)
    datasets = []
    while cursor < end:
        stop = min(cursor + timedelta(days=5), end)
        request = dict(
            market="US", ticker=symbol, interval="5m", start=iso(cursor), end=iso(stop)
        )
        response = fetcher(request)
        datasets.append(
            dict(request=request, response=response, response_sha256=digest(response))
        )
        cursor = stop
    return dict(symbol=symbol, as_of=as_of, calendar=calendar, datasets=datasets)


def build_packet(
    source,
    *,
    kind="RECONSTRUCTED_REPLAY",
    report_text=None,
    review=None,
    decision_ref=None,
):
    reasons = []
    symbol, as_of, calendar = source["symbol"], source["as_of"], source["calendar"]
    if not source["datasets"]:
        raise ValueError("EMPTY_SOURCE")
    bars = []
    starts = []
    ends = []
    current_starts = []
    target = stamp(as_of)
    for item in source["datasets"]:
        response = item["response"]
        request = item["request"]
        if digest(response) != item["response_sha256"]:
            raise ValueError("SOURCE_HASH_MISMATCH")
        if (
            request["ticker"] != symbol
            or request["market"] != "US"
            or request["interval"] != "5m"
        ):
            raise ValueError("SOURCE_IDENTITY_MISMATCH")
        if response.get("status") != "received":
            reasons.append("PROVIDER_DATA_UNAVAILABLE")
        if EXCHANGES.get(response.get("exchange")) != calendar["name"]:
            reasons.append("EXCHANGE_CALENDAR_MISMATCH")
        bars.extend(response.get("raw_rows", []))
        if response.get("retrieval_started_at"):
            starts.append(stamp(response["retrieval_started_at"]))
            if (
                stamp(request["start"]) <= target - timedelta(minutes=5)
                and stamp(request["end"]) >= target
            ):
                current_starts.append(stamp(response["retrieval_started_at"]))
        if response.get("retrieved_at"):
            ends.append(stamp(response["retrieved_at"]))
        if response.get("retrieval_started_at") and response.get("retrieved_at"):
            if stamp(response["retrieval_started_at"]) > stamp(
                response["retrieved_at"]
            ):
                reasons.append("RETRIEVAL_CLOCK_INVALID")
    if len(starts) != len(source["datasets"]) or len(ends) != len(source["datasets"]):
        reasons.append("RETRIEVAL_CLOCK_MISSING")
    if not current_starts:
        reasons.append("CURRENT_BAR_FETCH_PROVENANCE_MISSING")
    if reasons:
        intraday = dict(
            status="MISSING",
            reason_codes=sorted(set(reasons)),
            usable_for_prospective=False,
        )
    else:
        intraday = build_intraday_inputs(
            symbol=symbol,
            bars=bars,
            calendar=calendar,
            as_of=as_of,
            retrieved_at=iso(max(ends)),
            # History can be reused; current-bar acquisition must follow its close.
            retrieval_started_at=iso(min(current_starts)),
            price_basis_ref=BASIS,
            source_ref=digest(source),
            kind=kind,
        )
    setup = build_setup_input(
        report_text=report_text,
        review=review,
        symbol=symbol,
        decision_ref=decision_ref,
        as_of=as_of,
        price_basis_ref=BASIS,
    )
    missing = ["FRESH_QUOTE_NOT_SUPPLIED", "CURRENT_ADMISSION_GATES_NOT_SUPPLIED"]
    if setup["status"] != "OK":
        missing.extend(setup["reason_codes"])
    if not intraday.get("usable_for_prospective"):
        missing.append("NO_PROSPECTIVE_INTRADAY_INPUT")
    relative = None
    if intraday["status"] == "OK":
        volume = intraday["volume"]
        mean = sum(Decimal(x["cumulative_volume"]) for x in volume["samples"]) / 20
        relative = str(Decimal(volume["cumulative_volume"]) / mean)
    result = dict(
        contract_version=CONTRACT,
        kind=kind,
        symbol=symbol,
        as_of=as_of,
        source=source,
        source_sha256=digest(source),
        setup=setup,
        intraday=intraday,
        matched_rvol=relative,
        evaluation_ready=False,
        missing=missing,
        policy_executed=False,
        broker_execution=False,
    )
    result["packet_id"] = digest(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--calendar", choices=["NYSE", "NASDAQ"], required=True)
    parser.add_argument(
        "--kind",
        choices=["RECONSTRUCTED_REPLAY", "LIVE_CAPTURE"],
        default="RECONSTRUCTED_REPLAY",
    )
    parser.add_argument("--saved", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--review", type=Path)
    parser.add_argument("--decision-ref")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (
        args.output.suffix != ".json"
        or args.output.exists()
        or args.output.is_symlink()
    ):
        parser.error("output must be new")
    if args.saved:
        prior = json.loads(args.saved.read_text())
        if prior.get("contract_version") != CONTRACT:
            parser.error("saved contract mismatch")
        source = prior["source"]
        if (
            digest(source) != prior["source_sha256"]
            or source["symbol"] != args.symbol
            or stamp(source["as_of"]) != stamp(args.as_of)
            or source["calendar"]["name"] != args.calendar
        ):
            parser.error("saved source mismatch")
        # Re-reading an old file cannot become a new prospective observation.
        if args.kind != "RECONSTRUCTED_REPLAY":
            parser.error("saved sources are reconstructed only")
    else:
        source = collect_source(args.symbol, args.as_of, args.calendar)
    packet = build_packet(
        source,
        kind=args.kind,
        report_text=args.report.read_text() if args.report else None,
        review=json.loads(args.review.read_text()) if args.review else None,
        decision_ref=args.decision_ref,
    )
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(packet, stream, sort_keys=True, indent=2, allow_nan=False)
    print(
        json.dumps(
            dict(
                packet_id=packet["packet_id"],
                intraday_status=packet["intraday"]["status"],
                matched_rvol=packet["matched_rvol"],
                setup_status=packet["setup"]["status"],
                evaluation_ready=False,
                missing=packet["missing"],
            )
        )
    )


if __name__ == "__main__":
    main()

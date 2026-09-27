"""Current public-data research review. No historical backdating or trade execution."""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import json
import multiprocessing
import os
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prism_core.oneil_auto_review_output import build_review_bundle  # noqa: E402
from prism_core.oneil_batch_setup import financial_frame_records  # noqa: E402
from tools.collect_trend_replay_data import EXCHANGES, fetch, iso, stamp  # noqa: E402
from tools.build_oneil_adaptive_inputs import BASIS  # noqa: E402


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _financial_child(symbol, connection):
    import contextlib
    import io
    import logging
    import tempfile

    logging.disable(logging.CRITICAL)
    started = iso(datetime.now(timezone.utc))
    try:
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            tempfile.TemporaryDirectory() as cache,
        ):
            import yfinance as yf

            yf.set_tz_cache_location(cache)
            ticker = yf.Ticker(symbol)
            frame = ticker.quarterly_income_stmt
            info = ticker.info
            records = financial_frame_records(frame)
            result = dict(
                status="received" if records else "empty",
                symbol=info.get("symbol"),
                currency=info.get("financialCurrency"),
                quote_currency=info.get("currency"),
                records=records,
                yfinance_version=yf.__version__,
            )
    except Exception:
        result = dict(status="provider_error", records=[])
    result.update(
        retrieval_started_at=started, retrieved_at=iso(datetime.now(timezone.utc))
    )
    connection.send(result)
    connection.close()


def fetch_financials(symbol, timeout=40):
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_financial_child, args=(symbol, send), daemon=True)
    process.start()
    send.close()
    try:
        return (
            receive.recv()
            if receive.poll(timeout)
            else dict(status="timeout", records=[])
        )
    except EOFError:
        return dict(status="worker_error", records=[])
    finally:
        receive.close()
        process.join(1)
        if process.is_alive():
            process.terminate()
            process.join(2)
        if process.is_alive():
            process.kill()
            process.join(2)


def collect(
    symbol, calendar_name, price_fetcher=fetch, financial_fetcher=fetch_financials
):
    import pandas_market_calendars as calendars

    if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", symbol):
        raise ValueError("invalid symbol")
    start_time = datetime.now(timezone.utc)
    local_day = start_time.astimezone(ZoneInfo("America/New_York")).date()
    start = local_day - timedelta(days=400)
    end = local_day + timedelta(days=7)
    schedule = calendars.get_calendar(calendar_name).schedule(start, end)
    sessions = [
        dict(
            trade_date=day.date().isoformat(),
            open_at=iso(row["market_open"]),
            close_at=iso(row["market_close"]),
        )
        for day, row in schedule.iterrows()
    ]
    calendar = dict(
        coverage_end=end.isoformat(),
        sessions=sessions,
        provider=calendar_name,
        provider_version=calendars.__version__,
    )
    calendar["calendar_ref"] = digest(calendar)
    raw_prices = {}
    for ticker in (symbol, "SPY"):
        request = dict(
            market="US",
            ticker=ticker,
            interval="1d",
            start=start.isoformat() + "T00:00:00Z",
            end=iso(start_time),
        )
        response = price_fetcher(request)
        raw_prices[ticker] = dict(
            request=request, response=response, response_sha256=digest(response)
        )
    financial = financial_fetcher(symbol)
    raw = dict(prices=raw_prices, financials=financial, calendar=calendar)
    snapshot = dict(
        contract_version="oneil-auto-review-input-v1",
        market="US",
        symbol=symbol,
        decision_ref="research:" + digest([symbol, iso(start_time)]),
        price_basis_ref=BASIS,
        calendar=calendar,
    )
    for key, ticker in [("prices", symbol), ("benchmark", "SPY")]:
        response = raw_prices[ticker]["response"]
        rows = []
        expected = calendar_name if key == "prices" else "NYSE"
        if (
            response.get("status") == "received"
            and EXCHANGES.get(response.get("exchange")) == expected
            and (key == "benchmark" or financial.get("quote_currency") == "USD")
        ):
            for row in response["raw_rows"]:
                day = (
                    stamp(row["provider_timestamp"])
                    .astimezone(ZoneInfo("America/New_York"))
                    .date()
                    .isoformat()
                )
                rows.append(
                    dict(
                        date=day,
                        **{
                            k: row.get(k)
                            for k in (
                                "open",
                                "high",
                                "low",
                                "close",
                                "volume",
                                "dividends",
                                "stock_splits",
                            )
                        },
                    )
                )
        snapshot[key] = dict(
            symbol=ticker,
            source_ref=digest(raw_prices[ticker]),
            available_at=response.get("retrieved_at"),
            price_basis_ref=BASIS,
            bars=rows,
        )
    snapshot["financials"] = dict(
        symbol=financial.get("symbol"),
        source_ref=digest(financial),
        available_at=financial.get("retrieved_at"),
        availability_basis="CURRENT_PROVIDER_OBSERVATION_NOT_PUBLICATION_DATE",
        statement_kind="QUARTERLY_ACTUAL",
        currency=financial.get("currency"),
        eps_basis="PROVIDER_DILUTED_EPS",
        revenue_basis="PROVIDER_TOTAL_REVENUE",
        accounting_scope="PROVIDER_STATEMENT_SAME_SCOPE",
        records=financial.get("records", [])
        if financial.get("status") == "received"
        and financial.get("quote_currency") == "USD"
        else [],
    )
    return dict(
        snapshot=snapshot, raw_sources=raw, reviewed_at=iso(datetime.now(timezone.utc))
    )


def build_packet(source):
    bundle = build_review_bundle(source["snapshot"], reviewed_at=source["reviewed_at"])
    out = dict(
        contract_version="oneil-auto-review-packet-v1",
        source=source,
        source_hash=digest(source),
        bundle=bundle,
    )
    from prism_core.oneil_auto_review import evaluate_auto_review

    out["implementation_hashes"] = {
        name: hashlib.sha256(Path(inspect.getfile(function)).read_bytes()).hexdigest()
        for name, function in [
            ("classifier", evaluate_auto_review),
            ("assembler", build_review_bundle),
            ("collector", build_packet),
            ("price_fetcher", fetch),
            ("financial_parser", financial_frame_records),
        ]
    }
    out["packet_id"] = digest(out)
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--calendar", choices=["NYSE", "NASDAQ"], required=True)
    parser.add_argument("--saved", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (
        args.output.suffix != ".json"
        or args.output.exists()
        or args.output.is_symlink()
    ):
        parser.error("new JSON output required")
    if args.saved:
        prior = json.loads(args.saved.read_text())
        source = prior["source"]
        if (
            prior.get("contract_version") != "oneil-auto-review-packet-v1"
            or digest(source) != prior["source_hash"]
            or source["snapshot"]["symbol"] != args.symbol
            or source["snapshot"]["calendar"]["provider"] != args.calendar
        ):
            parser.error("saved source mismatch")
    else:
        source = collect(args.symbol, args.calendar)
    out = build_packet(source)
    with os.fdopen(
        os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
    ) as stream:
        json.dump(out, stream, sort_keys=True, indent=2, allow_nan=False)
    print(
        json.dumps(
            dict(
                packet_id=out["packet_id"],
                base_status=out["bundle"]["assessment"]["base"]["status"],
                base_reasons=out["bundle"]["assessment"]["base"]["reason_codes"],
                growth_status=out["bundle"]["assessment"]["leadership"]["status"],
                growth_reasons=out["bundle"]["assessment"]["leadership"][
                    "reason_codes"
                ],
                setup_status=out["bundle"]["setup_input"]["status"],
                live_ready=False,
            )
        )
    )


if __name__ == "__main__":
    main()

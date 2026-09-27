"""Aggregate-only inventory of stored historical scenarios, never a backtest.

No identifiers, prices, free text, account grouping or reconstructed originals
leave this reader. Stored scenarios may have been modified after entry.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.backfill_observability import _parse_time

TABLES = {"KR": "trading_history", "US": "us_trading_history"}
COLUMNS = frozenset({"sell_date", "profit_rate", "scenario"})
ROW_LIMIT = 100_000
TIME_LIMIT_SECONDS = 30
SCENARIO_BYTE_LIMIT = 1_000_000


def authorizer(action, first, second, _database, _source):
    if action == sqlite3.SQLITE_SELECT:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ and (
        first == "sqlite_master" or (first in TABLES.values() and second in COLUMNS)
    ):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and first == "table_info" and second in TABLES.values():
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_TRANSACTION and first in {"BEGIN", "ROLLBACK"}:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def read_only_connection(db):
    connection = sqlite3.connect(Path(db).resolve(strict=True).as_uri() + "?mode=ro", uri=True, timeout=5)
    connection.execute("PRAGMA query_only=ON")
    if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
        connection.close()
        raise ValueError("query_only_unavailable")
    connection.set_authorizer(authorizer)
    return connection


def finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def reject_constant(_value):
    raise ValueError("nonstandard_json_constant")


def utc(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build(db, market, source_timezone):
    if market not in TABLES or not isinstance(source_timezone, ZoneInfo):
        raise ValueError("invalid_market_or_source_timezone")
    counts = dict.fromkeys(("records", "return_positive", "return_negative", "return_zero",
        "return_invalid_or_missing", "scenario_absent", "scenario_invalid_json",
        "scenario_non_object", "scenario_object", "stop_loss_positive_numeric",
        "stop_loss_invalid_or_missing", "contract_version_known", "contract_version_unknown",
        "sell_date_valid", "sell_date_invalid_or_missing", "sell_date_naive_assumed_timezone",
        "sell_date_explicit_offset"), 0)
    began = datetime.now(timezone.utc)
    earliest = latest = None
    unavailable = None
    deadline = time.monotonic() + TIME_LIMIT_SECONDS
    connection = read_only_connection(db)
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        connection.execute("BEGIN")
        if market == "KR":
            info = connection.execute("PRAGMA table_info(trading_history)")
        else:
            info = connection.execute("PRAGMA table_info(us_trading_history)")
        columns = {row[1] for row in info}
        if not columns:
            unavailable = "SOURCE_TABLE_MISSING"
        elif not COLUMNS <= columns:
            unavailable = "REQUIRED_COLUMNS_MISSING"
        else:
            if market == "KR":
                records = connection.execute("SELECT sell_date, profit_rate, scenario FROM trading_history")
            else:
                records = connection.execute("SELECT sell_date, profit_rate, scenario FROM us_trading_history")
            for sold, profit, scenario in records:
                if counts["records"] >= ROW_LIMIT:
                    raise ValueError("inventory_row_limit")
                if time.monotonic() > deadline:
                    raise ValueError("inventory_timeout")
                counts["records"] += 1
                if not finite_number(profit):
                    counts["return_invalid_or_missing"] += 1
                else:
                    counts["return_positive" if profit > 0 else "return_negative" if profit < 0 else "return_zero"] += 1
                try:
                    parsed = _parse_time(sold, default_timezone=source_timezone)
                except (OverflowError, TypeError, ValueError):
                    parsed = None
                if parsed is None:
                    counts["sell_date_invalid_or_missing"] += 1
                else:
                    counts["sell_date_valid"] += 1
                    source = datetime.fromisoformat(str(sold).strip().replace("Z", "+00:00"))
                    counts["sell_date_naive_assumed_timezone" if source.tzinfo is None else "sell_date_explicit_offset"] += 1
                    earliest = min(earliest, parsed) if earliest else parsed
                    latest = max(latest, parsed) if latest else parsed
                if scenario is None or isinstance(scenario, str) and not scenario.strip():
                    counts["scenario_absent"] += 1
                    continue
                try:
                    if not isinstance(scenario, (str, bytes)) or len(scenario) > SCENARIO_BYTE_LIMIT:
                        raise ValueError("invalid_or_oversized_scenario")
                    decoded = json.loads(scenario, parse_constant=reject_constant)
                except (ValueError, UnicodeError, RecursionError):
                    counts["scenario_invalid_json"] += 1
                    continue
                if not isinstance(decoded, dict):
                    counts["scenario_non_object"] += 1
                    continue
                counts["scenario_object"] += 1
                stop = decoded.get("stop_loss")
                counts["stop_loss_positive_numeric" if finite_number(stop) and stop > 0 else "stop_loss_invalid_or_missing"] += 1
                counts["contract_version_known" if decoded.get("_scenario_contract_version") == "buy-scenario-v1" else "contract_version_unknown"] += 1
        connection.execute("ROLLBACK")
    finally:
        connection.close()
    return {"analysis_contract_version": "history-scenario-inventory-v1", "market": market,
        "source_status": unavailable or "AVAILABLE", "counts": counts,
        "recorded_sell_period_utc": {"earliest": utc(earliest) if earliest else None,
                                     "latest": utc(latest) if latest else None},
        "naive_source_timezone": str(source_timezone),
        "timestamp_basis": "OPERATOR_DECLARED_NOT_INFERRED_FROM_MARKET",
        "retrieval_started_at": utc(began), "retrieved_at": utc(datetime.now(timezone.utc)),
        "scenario_basis": "CURRENT_STORED_SNAPSHOT_NOT_PROVEN_ORIGINAL_AT_ENTRY",
        "known_contract_versions": ["buy-scenario-v1"],
        "limitations": ["Record counts are not distinct strategy trades or canonical performance",
            "No account grouping, mean return, portfolio PnL or broker-fill inference",
            "Stored positive stop is not proof of initial stop, executable plan or valid risk distance",
            "Numeric strings are not counted as numeric returns or stops",
            "Stop and version counts use scenario objects only; other scenario categories remain unknown",
            "No frozen historical cutoff; consistent read transaction covers current stored rows"]}


def validate_output(db, output):
    db, output = Path(db).resolve(strict=True), Path(output)
    if ".." in output.parts:
        raise ValueError("output_parent_traversal_forbidden")
    output = output.absolute()
    if (output.suffix != ".json" or not output.parent.is_dir()
            or output.is_relative_to(db.parent)
            or any(path.is_symlink() for path in (output, *output.parents))
            or output.exists()):
        raise ValueError("unsafe_output_location_or_existing_file")
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--market", choices=sorted(TABLES), required=True)
    parser.add_argument("--naive-source-timezone", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        output = validate_output(args.db, args.output)
        packet = build(args.db, args.market, ZoneInfo(args.naive_source_timezone))
        payload = json.dumps(packet, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        descriptor = os.open(output, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
    except (ValueError, OSError, sqlite3.Error, KeyError):
        # Errors deliberately omit exception text, SQL, source rows and paths.
        parser.exit(2, "history_scenario_inventory_failed\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

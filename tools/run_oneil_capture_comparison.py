"""Ingest local research records and compare closed campaigns without orders.

No provider calls, trading database reads, scheduler or strategy activation.
The SQLite tape must be dedicated to this tool. Without --input it must exist.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prism_core.oneil_capture_tape import OneilCaptureTape  # noqa: E402
from prism_core.oneil_current_capture import capture_current_record  # noqa: E402
from prism_core.oneil_paired_replay import digest, evaluate_replay  # noqa: E402


def compare_tape(tape):
    exported = tape.export_replay()
    comparison = evaluate_replay(exported)
    result = {
        "contract": "oneil-capture-comparison-v1",
        "capture_coverage": exported["capture_coverage"],
        "capture_completeness": "UNKNOWN_BEST_EFFORT",
        "comparison": comparison,
        "source_authentication": "LOCALLY_CAPTURED_NOT_AUTHENTICATED",
        "performance_validated": False,
        "live_ready": False,
        "broker_execution": False,
        "verdict": "CONTINUE_CAPTURE",
    }
    result["packet_id"] = digest(result)
    return result


def ingest(tape, payload):
    if not isinstance(payload, dict) or payload.get("contract") != "oneil-capture-import-v1":
        raise ValueError("explicit import contract required")
    operations = payload.get("operations")
    if not isinstance(operations, list) or not 1 <= len(operations) <= 10000:
        raise ValueError("bounded operations required")
    results = []
    for operation in operations:
        kind = operation.get("kind") if isinstance(operation, dict) else None
        if kind not in {"INITIAL", "TICK", "EXIT", "CURRENT"}:
            raise ValueError("unsupported operation")
    for operation in operations:
        kind = operation["kind"]
        if kind == "INITIAL":
            result = tape.ingest_capture(operation["record"])
        elif kind == "CURRENT":
            observation = capture_current_record(**operation["inputs"])
            # A committed original exit wins over an add evaluation at the same
            # instant. Exit provenance is independently validated by the tape.
            if observation["exit_event"] is not None:
                result = tape.append_exit(operation["campaign_id"], observation["exit_event"])
            else:
                result = tape.append_observation(operation["campaign_id"], observation)
        else:
            method = tape.append_tick if kind == "TICK" else tape.append_exit
            result = method(operation["campaign_id"], operation["record"])
        results.append(result)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--input", type=Path, help="explicit ordered initial/tick/exit import")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output and (args.output.exists() or args.output.is_symlink()):
        parser.error("output must be new")
    if not args.input and not args.db.is_file():
        parser.error("comparison requires an existing dedicated tape")
    try:
        payload = None
        if args.input:
            if args.input.is_symlink() or args.input.stat().st_size > 16 * 1024 * 1024:
                raise ValueError("bounded local input required")
            payload = json.loads(args.input.read_text())
        tape = OneilCaptureTape(args.db)
        if payload is not None:
            ingest(tape, payload)
        result = compare_tape(tape)
        encoded = json.dumps(result, sort_keys=True, allow_nan=False) + "\n"
        if args.output:
            with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
                stream.write(encoded)
        print(encoded, end="")
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        # Payloads can contain sensitive strings: print the class, never the text.
        parser.exit(2, f"capture comparison unavailable ({type(exc).__name__})\n")


if __name__ == "__main__":
    main()

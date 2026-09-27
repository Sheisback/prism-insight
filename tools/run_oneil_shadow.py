"""Run source-bound SHADOW cycles; no broker adapter or automatic LIVE switch."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prism_core.oneil_shadow_runner import ShadowRunner  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["SHADOW"], default="SHADOW")
    parser.add_argument("--runtime-db", type=Path, required=True)
    parser.add_argument("--capture-db", type=Path, required=True)
    parser.add_argument("--holdings-db", type=Path, required=True)
    parser.add_argument("--tape-db", type=Path)
    parser.add_argument("--since", required=True, help="fixed capture boundary, not proof of holdout")
    parser.add_argument("--max-slots", type=int, required=True, help="existing strategy slot cap, never account cash")
    parser.add_argument("--source", choices=["regular", "mechanical"], default="mechanical")
    parser.add_argument("--calendar", choices=["AUTO", "NYSE", "NASDAQ"], default="AUTO")
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    if not 1 <= args.cycles <= 390 or not 60 <= args.interval <= 3600:
        parser.error("cycles must be 1..390 and interval 60..3600 seconds")
    try:
        runner = ShadowRunner(runtime_db=args.runtime_db, capture_db=args.capture_db,
                              holdings_db=args.holdings_db, tape_db=args.tape_db,
                              since=args.since, max_slots=args.max_slots)
        failed = False
        for index in range(args.cycles):
            result = runner.once(source=args.source, calendar=args.calendar)
            print(json.dumps(result, sort_keys=True, allow_nan=False), flush=True)
            failed |= any(row["status"] == "ERROR" for row in result["rows"])
            if index + 1 < args.cycles:
                time.sleep(args.interval)
        return 2 if failed else 0
    except Exception as error:
        parser.exit(2, f"SHADOW unavailable ({type(error).__name__}); no broker execution\n")


if __name__ == "__main__":
    raise SystemExit(main())

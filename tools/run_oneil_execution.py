"""Run the deployed owned SHADOW/LIVE worker; authorization stays in config."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prism_core.oneil_config import DEFAULT_PATH, load  # noqa: E402
from prism_core.oneil_live_boundary import readiness  # noqa: E402
from prism_core.oneil_service import OneilService, configured_accounts  # noqa: E402


def write_health(path, value):
    if path.is_symlink():
        raise ValueError("health symlink rejected")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".oneil-health-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


async def run(args):
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    worker, previous = None, None
    while True:
        try:
            config = load(args.config, protection_only=True)
            if args.check:
                print(json.dumps(readiness(config), sort_keys=True))
                return 0
            signature = json.dumps(config, sort_keys=True)
            if config["mode"] == "OFF" and not Path(config["live_db"]).exists():
                result = dict(contract="oneil-service-v1", mode="OFF", status="OFF", rows=[],
                              at=datetime.now(timezone.utc).isoformat(), live_activation=False)
            else:
                if worker is None or signature != previous:
                    accounts = configured_accounts()
                    names = {a["name"] for a in accounts}
                    if any(name not in names for name in config["accounts"]):
                        raise ValueError("configured account scope unavailable")
                    worker = OneilService(config, accounts, config_path=args.config)
                    previous = signature
                result = await worker.once()
            result["technical_readiness"] = readiness(config)
            write_health(args.health, result)
            print(json.dumps(result, sort_keys=True, allow_nan=False), flush=True)
            if args.once:
                return 2 if any(row["status"] == "ERROR" for row in result["rows"]) else 0
            await asyncio.sleep(config["interval_seconds"])
        except Exception as error:
            result = dict(status="ERROR", error_type=type(error).__name__,
                          at=datetime.now(timezone.utc).isoformat(), broker_execution_verified=False)
            write_health(args.health, result)
            print(json.dumps(result, sort_keys=True), flush=True)
            if args.once or args.check:
                return 2
            await asyncio.sleep(60)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--health", type=Path, default=ROOT / "runtime/oneil-health.json")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    configuration = Path(args.config or os.getenv("ONEIL_EXECUTION_CONFIG", str(DEFAULT_PATH)))
    if (args.health.suffix != ".json" or args.health.is_symlink()
            or args.health.resolve() == configuration.resolve()
            or (args.health.exists() and configuration.exists() and args.health.samefile(configuration))):
        parser.error("health output must be a separate JSON file")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())

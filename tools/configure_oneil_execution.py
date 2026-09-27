"""Explicit operational mode changes; never generates its own LIVE approval."""
import argparse
import fcntl
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prism_core.oneil_config import DEFAULT_PATH, defaults, implementation_hash, validate  # noqa: E402


def active_entry_batches():
    inventory = Path("/proc")
    if not inventory.is_dir():
        raise ValueError("LIVE cutover requires the Linux server process inventory")
    pattern = re.compile(r"(?:^|[/\s])(?:us_stock_analysis_orchestrator|us_stock_tracking_agent)(?:\.py|\s|$)")
    count = 0
    for process in inventory.iterdir():
        if not process.name.isdigit() or process.name == str(os.getpid()):
            continue
        try:
            with (process / "cmdline").open("rb") as stream:
                command = stream.read(65536).replace(b"\0", b" ").decode("utf-8", errors="replace")
        except (FileNotFoundError, ProcessLookupError):
            continue
        count += bool(pattern.search(command))
    return count


def publish(path, value, *, expected_hash=None):
    if path.suffix != ".json":
        raise ValueError("configuration must be a JSON file")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _publish_locked(path, value, expected_hash=expected_hash)
    finally:
        os.close(descriptor)


def _publish_locked(path, value, *, expected_hash=None):
    if path.is_symlink():
        raise ValueError("config symlink rejected")
    if path.exists():
        existing = path.read_bytes()
        if not expected_hash or hashlib.sha256(existing).hexdigest() != expected_hash:
            raise ValueError("existing config requires exact expected SHA256")
    elif expected_hash:
        raise ValueError("expected config missing")
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    fd, temporary = tempfile.mkstemp(prefix=".oneil-config-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
                raise ValueError("configuration changed concurrently")
            backup = path.with_name(path.name + ".before-" + expected_hash[:16])
            if not backup.exists():
                with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
                    output.write(existing)
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return hashlib.sha256(encoded).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["OFF", "SHADOW", "LIVE"], required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_PATH)
    parser.add_argument("--accounts", nargs="+")
    parser.add_argument("--from-configured-us", action="store_true")
    parser.add_argument("--since")
    parser.add_argument("--approval-file", type=Path)
    parser.add_argument("--expected-hash")
    parser.add_argument("--confirm-new-campaigns", action="store_true")
    args = parser.parse_args()
    try:
        value = json.loads(args.output.read_text()) if args.output.exists() else defaults()
        if args.mode == "LIVE":
            if not args.confirm_new_campaigns or not args.approval_file or args.from_configured_us:
                raise ValueError("LIVE requires explicit approval and new-campaign confirmation")
            if active_entry_batches():
                raise ValueError("wait until existing US entry batches finish before cutover")
            value["live_approval"] = json.loads(args.approval_file.read_text())
        else:
            value["live_approval"] = None
        if args.accounts and args.from_configured_us:
            raise ValueError("choose one account source")
        if args.from_configured_us:
            from dotenv import load_dotenv
            load_dotenv(ROOT / ".env")
            from prism_core.oneil_service import configured_accounts
            value["accounts"] = [a["name"] for a in configured_accounts()]
        elif args.accounts:
            value["accounts"] = args.accounts
        value["mode"] = args.mode
        if args.since:
            value["capture_since"] = args.since
        elif not value["capture_since"]:
            value["capture_since"] = datetime.now(timezone.utc).isoformat()
        value = validate(value)
        checksum = publish(args.output, value, expected_hash=args.expected_hash)
        print(json.dumps(dict(mode=value["mode"], config_sha256=checksum,
                              implementation_hash=implementation_hash(), accounts_count=len(value["accounts"]),
                              capture_since=value["capture_since"], orders_submitted=0), sort_keys=True))
        return 0
    except Exception as error:
        parser.exit(2, f"mode change refused ({type(error).__name__})\n")


if __name__ == "__main__":
    raise SystemExit(main())

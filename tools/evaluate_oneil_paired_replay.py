"""Evaluate a local original-evidence tape without network, runtime DB or orders."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prism_core.oneil_paired_replay import assess_entry_quality_packet, evaluate_replay  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--input", type=Path)
    source_group.add_argument("--entry-quality-packet", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    source = args.input or args.entry_quality_packet
    if source.stat().st_size > 16 * 1024 * 1024:
        parser.error("input too large")
    evaluator = evaluate_replay if args.input else assess_entry_quality_packet
    result = evaluator(json.loads(source.read_text()))
    encoded = json.dumps(result, sort_keys=True, allow_nan=False) + "\n"
    if args.output:
        with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
            stream.write(encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()

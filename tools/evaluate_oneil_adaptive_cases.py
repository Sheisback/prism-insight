"""Deterministic synthetic-case evaluator. No network, database, orders or tuning."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prism_core.oneil_adaptive_policy import VERSION, evaluate_target  # noqa: E402


def evaluate_cases(payload):
    if (not isinstance(payload, dict) or payload.get('contract') != 'oneil-adaptive-cases-v1'
            or payload.get('kind') != 'SYNTHETIC'):
        raise ValueError('explicit synthetic case contract required')
    cases = payload.get('cases')
    if not isinstance(cases, list) or not 1 <= len(cases) <= 100:
        raise ValueError('bounded cases required')
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)
    outputs = []
    for index, case in enumerate(cases):
        decision = evaluate_target(case['plan'], case['evidence'], **case['state'])
        outputs.append(dict(case_index=index, decision=decision))
    return dict(contract='oneil-adaptive-functional-evidence-v1', policy=VERSION,
                input_sha256=hashlib.sha256(canonical.encode()).hexdigest(), results=outputs,
                validation_kind='FUNCTIONAL_SYNTHETIC_ONLY', live_ready=False,
                performance_validated=False, broker_execution=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    args = parser.parse_args()
    if args.input.stat().st_size > 16 * 1024 * 1024:
        parser.error('input too large')
    print(json.dumps(evaluate_cases(json.loads(args.input.read_text())), sort_keys=True, allow_nan=False))


if __name__ == '__main__':
    main()

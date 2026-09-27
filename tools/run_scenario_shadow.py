"""Run explicit scenario inputs against an isolated SHADOW ledger, never a broker.

No cron installation or LIVE mode. Input producers must authenticate scenario,
quote, session and gate evidence; caller-provided facts are labeled attestations.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from observability.strategy_ledger_projection import validate_destination
from prism_core.scenario_shadow_ledger import ScenarioShadowLedger


def run(input_path, ledger_path, book_id, max_slots=10):
    source = Path(input_path).resolve(strict=True)
    if source.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('bounded input file required')
    destination = validate_destination(ledger_path, (source,))
    engine = ScenarioShadowLedger(destination)
    engine.provision(book_id, max_slots)
    counts = dict(applied=0, duplicates=0)
    with source.open() as stream:
        for line in stream:
            if not line.strip():
                continue
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError('object input required')
            operation = item.pop('operation', None)
            if operation == 'open':
                result = engine.open(book_id=book_id, **item)
            elif operation == 'tick':
                result = engine.advance(**item)
            elif operation == 'revise':
                result = engine.revise(**item)
            else:
                raise ValueError('unsupported operation; only open/tick/revise allowed')
            counts['applied' if result['event_applied'] else 'duplicates'] += 1
    return dict(mode='SHADOW', policy='scenario-shadow-v1', **counts,
                snapshot=engine.ledger.snapshot(book_id),
                prospective_capture_connected=False, live_ready=False,
                readiness_reasons=['PROSPECTIVE_CAPTURE_NOT_CONNECTED',
                                   'AUTHENTICATED_INPUT_ADAPTER_NOT_CONNECTED',
                                   'BROKER_EXECUTION_ADAPTER_NOT_CONNECTED',
                                   'FORWARD_HOLDOUT_NOT_STARTED'],
                evidence_authority='CALLER_ATTESTED_NOT_AUTHENTICATED')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--ledger', required=True)
    parser.add_argument('--book-id', required=True)
    parser.add_argument('--max-slots', type=int, default=10)
    args = parser.parse_args()
    print(json.dumps(run(args.input,args.ledger,args.book_id,args.max_slots),sort_keys=True))


if __name__ == '__main__':
    main()

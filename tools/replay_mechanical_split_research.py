"""Bounded offline historical diagnostic; no orders, DB, or policy promotion.

Run with project PYTHONPATH. Output is exclusive-create and contains public prices
and sanitized Packet fields only. --saved reuses the frozen provider responses.
"""
import argparse
import hashlib
import json
import math
import inspect
from datetime import timedelta
from pathlib import Path

from tools.collect_trend_replay_data import fetch, stamp, iso, EXCHANGES
try:
    from prism_core.mechanical_split_research import Tick, simulate
except ModuleNotFoundError:  # isolated /tmp research bundle, never a runtime install
    from mechanical_split_research import Tick, simulate


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def ticks_for(row, datasets):
    import pandas_market_calendars as calendars
    start = stamp(row['outcomes']['strategy_entry_at'])
    end = stamp(row['outcomes']['strategy_closed_at'])
    reasons, raw = set(), {}
    exchanges = {d['response'].get('exchange') for d in datasets}
    if len(exchanges) != 1 or next(iter(exchanges), None) not in EXCHANGES:
        return [], ['UNKNOWN_CALENDAR']
    for dataset in datasets:
        response = dataset['response']
        if response.get('status') != 'received':
            reasons.add('PROVIDER_DATA_UNAVAILABLE')
        for bar in response.get('raw_rows', []):
            at = stamp(bar['provider_timestamp'])
            if at in raw and raw[at] != bar:
                reasons.add('CONFLICTING_BAR')
            raw[at] = bar
    calendar = calendars.get_calendar(EXCHANGES[next(iter(exchanges))])
    schedule = calendar.schedule(start.date(), end.date())
    ticks = []
    entry_bar_seen = False
    for _, session in schedule.iterrows():
        opened, closed = session['market_open'].to_pydatetime(), session['market_close'].to_pydatetime()
        at = opened
        while at < closed and at < end:
            if at + timedelta(minutes=5) > start:
                b = raw.get(at)
                if b is None:
                    reasons.add('MISSING_REGULAR_BAR')
                elif not all(isinstance(b.get(k), (int,float)) and math.isfinite(b[k]) and b[k] > 0
                             for k in ('open','high','low','close')):
                    reasons.add('INVALID_OHLC')
                elif not b['low'] <= min(b['open'], b['close']) <= max(b['open'], b['close']) <= b['high']:
                    reasons.add('INVALID_OHLC')
                else:
                    if b.get('stock_splits') != 0 or b.get('dividends') != 0:
                        reasons.add('CORPORATE_ACTION_UNVERIFIED')
                    if at <= start < at + timedelta(minutes=5):
                        entry_bar_seen = True
                        e = row['entry']['price_evidence']['reference_price']
                        if not b['low'] <= e <= b['high']:
                            reasons.add('ENTRY_PRICE_BASIS_UNVERIFIED')
            at += timedelta(minutes=5)
        at = opened + timedelta(minutes=10)
        while at < closed and at < end:
            if at > start:
                signal, execution = raw.get(at-timedelta(minutes=5)), raw.get(at)
                if signal is None or execution is None:
                    reasons.add('MISSING_TICK_PAIR')
                else:
                    ticks.append(Tick(at, signal['close'], execution['open']))
            at += timedelta(minutes=10)
    if not ticks:
        reasons.add('NO_REGULAR_TICKS')
    if not entry_bar_seen:
        reasons.add('ENTRY_PRICE_BAR_MISSING')
    # This profile deliberately operates regular-hours only. Outside-session
    # original exits retain their recorded price and are never relabeled fills.
    return ticks, sorted(reasons)


def run(packet, fetcher):
    if packet.get('analysis_contract_version') != 'entry-quality-harness-v2' or packet.get('market') != 'US':
        raise ValueError('unsupported_packet')
    results, inputs = [], []
    for row in packet['analysis_rows']:
        if not row['entry']['observed'] or not row['outcomes'].get('strategy_closed_at'):
            continue
        result = {k: row.get(k) for k in ('decision_ref','ticker','trigger_type','regime','policy_version')}
        entry = row['entry']['price_evidence']
        exit_evidence = row['outcomes'].get('exit_price_evidence', {})
        if entry.get('status') != 'OK' or exit_evidence.get('status') != 'OK':
            results.append({**result, 'reasons': ['MISSING_ORIGINAL_PRICE']})
            continue
        start, end = stamp(row['outcomes']['strategy_entry_at']), stamp(row['outcomes']['strategy_closed_at'])
        cursor = start.replace(hour=0,minute=0,second=0,microsecond=0)
        final = end.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)
        datasets = []
        while cursor < final:
            stop = min(cursor+timedelta(days=5), final)
            request = dict(market='US',ticker=row['ticker'],interval='5m',start=iso(cursor),end=iso(stop))
            response = fetcher(request)
            dataset = dict(request=request,response=response,sha256=digest(response))
            datasets.append(dataset)
            inputs.append(dataset)
            cursor = stop
        ticks, reasons = ticks_for(row,datasets)
        result.update(reasons=reasons,tick_count=len(ticks))
        if not reasons:
            result['cost_cases'] = {}
            for cost in (10,25):
                cases = {}
                for name, trailing in (('mechanical',False),('trailing',True)):
                    cases[name] = simulate(entry=entry['reference_price'],stop=entry['stop_loss_at_entry'],
                        entry_at=start,exit_at=end,exit_price=exit_evidence['reference_price'],
                        ticks=ticks,trailing=trailing,cost_bps=cost)
                result['cost_cases'][str(cost)] = cases
        results.append(result)
    return dict(kind='RETROSPECTIVE_DIAGNOSTIC_NOT_HOLDOUT',profile='micro-split-mechanical-v1',
                packet_id=packet['packet_id'],packet_sha256=digest(packet),inputs=inputs,results=results,
                scope_caveat='single-entry/pilot eligibility and scenario revision history not proven',
                verdict='CONTINUE_CAPTURE',automatic_live_forbidden=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--packet',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--saved')
    args=parser.parse_args()
    if Path(args.output).exists():
        raise ValueError('output_exists')
    packet=json.loads(Path(args.packet).read_text())
    fetcher=fetch
    if args.saved:
        saved=json.loads(Path(args.saved).read_text())
        for d in saved['inputs']:
            if digest(d['response']) != d['sha256']:
                raise ValueError('source_hash_mismatch')
        sources={digest(d['request']):d['response'] for d in saved['inputs']}
        def saved_fetcher(request):
            return sources[digest(request)]
        fetcher = saved_fetcher
    result=run(packet,fetcher)
    import pandas_market_calendars as calendars
    result['implementation'] = {
        'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'engine_sha256': hashlib.sha256(Path(inspect.getfile(simulate)).read_bytes()).hexdigest(),
        'calendar_version': calendars.__version__,
    }
    result['study_id']=digest(result)
    with open(args.output,'x') as stream:
        json.dump(result,stream,sort_keys=True,indent=2,allow_nan=False)
    print(json.dumps({k:result[k] for k in ('study_id','packet_id','verdict')}))


if __name__=='__main__':
    main()

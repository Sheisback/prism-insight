"""Offline integration: policy targets reuse existing normalized-unit accounting."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys

import pytest

from prism_core.oneil_adaptive_policy import evaluate_target
from prism_core.strategy_ledger import StrategyLedger
from test_oneil_adaptive_policy import evidence, plan
from tools.evaluate_oneil_adaptive_cases import evaluate_cases


def state(campaign, stop='100', **overrides):
    return dict(now='2026-09-25T13:41:00Z',
                cumulative_allocation=campaign['cumulative_deployed_allocation'],
                remaining_allocation=campaign['remaining_allocation'],
                normalized_units=campaign['normalized_units'],
                remaining_entry_cost=campaign['remaining_entry_cost'], current_stop=stop, **overrides)


@pytest.mark.parametrize('price,target', [('102', Decimal('.8')), ('104', Decimal('1'))])
def test_scout_jumps_to_target_in_one_ledger_leg(tmp_path, price, target):
    ledger = StrategyLedger(tmp_path/'research.sqlite')
    ledger.create_book('b', 'US', cohort='oneil-adaptive-research', mode='VALIDATION')
    opened = ledger.apply_target('scout', 'b', 'c', 'TEST', 10, 100,
                                 '2026-09-25T13:30:00Z', fee_rate='.001')
    decision = evaluate_target(plan(), evidence(price), **state(opened['campaigns'][0]))
    assert decision['action'] == 'ADD'
    assert Decimal(decision['target_allocation']) == target
    args = ('bulk', 'b', 'c', 'TEST', str(target*100), price, '2026-09-25T13:41:00Z')
    snapshot = ledger.apply_target(*args, fee_rate='.001', policy_version='oneil-adaptive-v1')
    campaign = snapshot['campaigns'][0]
    assert len(campaign['legs']) == 2  # scout plus ONE bulk add, not intermediate orders
    assert Decimal(campaign['cumulative_deployed_allocation']) == target
    assert snapshot['executions'] == []  # virtual accounting is not a broker fill
    assert not ledger.apply_target(*args, fee_rate='.001', policy_version='oneil-adaptive-v1')['event_applied']
    repeated = evaluate_target(plan(), evidence('104'), **state(campaign, last_add_bar_end=decision['bar_end']))
    assert repeated['action'] == 'WAIT'


@pytest.mark.parametrize('bps', [10, 25])
def test_risk_clipped_target_matches_ledger_costs(tmp_path, bps):
    ledger = StrategyLedger(tmp_path/'risk.sqlite')
    ledger.create_book('b', 'US', mode='VALIDATION')
    fee = Decimal(bps)/10000
    opened = ledger.apply_target('scout','b','c','TEST',10,100,'2026-09-25T13:30:00Z',fee_rate=str(fee))
    p = plan(bps)
    decision = evaluate_target(p, evidence(), **state(opened['campaigns'][0],stop='90'))
    assert decision['risk_clipped'] and decision['action']=='ADD'
    snapshot = ledger.apply_target('bulk','b','c','TEST',str(Decimal(decision['target_allocation'])*100),
                                   decision['price'],'2026-09-25T13:41:00Z',fee_rate=str(fee))
    c = snapshot['campaigns'][0]
    loss = Decimal(c['remaining_allocation'])+Decimal(c['remaining_entry_cost'])-Decimal(c['normalized_units'])*90*(1-fee)
    assert loss <= Decimal(p['risk_limit'])


def test_offline_cli_is_reproducible_and_not_performance_claim(tmp_path):
    p, facts = plan(), evidence()
    inputs = dict(now='2026-09-25T13:41:00Z',cumulative_allocation='.1',remaining_allocation='.1',
                  normalized_units='.001',remaining_entry_cost='.0001',current_stop='100')
    payload = dict(contract='oneil-adaptive-cases-v1',kind='SYNTHETIC',
                   cases=[dict(plan=p,evidence=facts,state=inputs)])
    original = deepcopy(payload)
    expected = evaluate_cases(payload)
    assert evaluate_cases(payload)==expected and payload==original
    path=tmp_path/'cases.json'
    path.write_text(json.dumps(payload))
    command=[sys.executable,str(Path(__file__).resolve().parents[1]/'tools/evaluate_oneil_adaptive_cases.py'),
             '--input',str(path)]
    actual=json.loads(subprocess.run(command,check=True,capture_output=True,text=True).stdout)
    assert actual==expected
    assert not actual['live_ready'] and not actual['performance_validated'] and not actual['broker_execution']
    payload['kind']='PROSPECTIVE'
    with pytest.raises(ValueError):
        evaluate_cases(payload)

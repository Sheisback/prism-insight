import json
import subprocess
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from prism_core.scenario_shadow_ledger import ScenarioShadowLedger
from prism_core.strategy_ledger import LedgerError
from tools.run_scenario_shadow import run

ENTRY='2026-09-28T14:00:00+00:00'
NOW='2026-09-28T14:10:00+00:00'


def inputs():
    return dict(entry_price='100',initial_stop='90',entry_at=ENTRY,
                source_decision_ref='sanitized-decision-1',entry_eligible=True)


def evidence(price='105',source='mechanical'):
    return dict(source=source,quote=dict(price=price,observed_at=NOW),
        bar=dict(open_at='2026-09-28T14:05:00+00:00',close_at=NOW,
                 observed_at=NOW,close=price,completed=True),
        session=dict(open_at='2026-09-28T13:30:00+00:00',close_at='2026-09-28T20:00:00+00:00',
                     verified=True,source_ref='fixture-session'),
        gates=dict(risk=True,regime=True,sector=True,slot=True,observed_at=NOW,source_ref='fixture-gates'))


@pytest.fixture
def engine(tmp_path):
    engine=ScenarioShadowLedger(tmp_path/'scenario.sqlite')
    engine.provision('shadow')
    engine.open('open','shadow','campaign','TEST',plan_inputs=inputs())
    return engine


def test_atomic_add_duplicate_and_restart(engine):
    kwargs=dict(expected_revision=0,evidence=evidence(),occurred_at=NOW)
    first=engine.advance('tick','campaign',**kwargs)
    assert first['decision']['action']=='ADD'
    restarted=ScenarioShadowLedger(engine.ledger.path)
    assert not restarted.advance('tick','campaign',**kwargs)['event_applied']
    campaign=restarted.ledger.snapshot('shadow')['campaigns'][0]
    assert len(campaign['legs'])==2
    assert campaign['scenario_shadow']['revision']==1
    assert float(campaign['cumulative_deployed_allocation'])==.3


def test_two_sources_race_only_one_can_advance(engine):
    def advance(source):
        try:
            return engine.advance(source,'campaign',expected_revision=0,
                                  evidence=evidence(source=source),occurred_at=NOW)['event_applied']
        except LedgerError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(advance,['mechanical','regular']))==1
    assert len(engine.ledger.snapshot('shadow')['campaigns'][0]['legs'])==2


def test_stale_revision_rolls_back_and_direct_paths_rejected(engine):
    before=engine.ledger.snapshot('shadow')
    with pytest.raises(LedgerError,match='revision conflict'):
        engine.advance('bad','campaign',expected_revision=7,evidence=evidence(),occurred_at=NOW)
    assert engine.ledger.snapshot('shadow')==before
    with pytest.raises(LedgerError,match='direct target'):
        engine.ledger.apply_target('bypass','shadow','campaign','TEST',100,110,NOW)
    with pytest.raises(LedgerError,match='direct sell'):
        engine.ledger.sell('bypass-exit','campaign',90,NOW)


def test_exit_is_atomic_and_cannot_reopen(engine):
    result=engine.advance('exit','campaign',expected_revision=0,evidence=evidence('89'),occurred_at=NOW)
    assert result['decision']['action']=='EXIT'
    assert result['campaigns'][0]['status']=='CLOSED'
    again=engine.advance('after','campaign',expected_revision=1,evidence=evidence('105'),occurred_at=NOW)
    assert again['decision']['action']=='WAIT'
    assert len(again['campaigns'][0]['legs'])==2


def test_revision_authority_and_cancellation_keep_protection(engine):
    with pytest.raises(ValueError):
        engine.revise('bad','campaign',expected_revision=0,occurred_at=NOW,
                      changes=dict(source='mechanical',expected_version=0,cancel=True))
    result=engine.revise('cancel','campaign',expected_revision=0,occurred_at=NOW,
                        changes=dict(source='regular',expected_version=0,cancel=True))
    assert result['campaigns'][0]['scenario_shadow']['state']['add_cancelled']
    assert result['campaigns'][0]['conditional_remaining_allocation']=='0'
    assert result['campaigns'][0]['add_permission']=='ADD_CANCELLED'
    result=engine.advance('stop','campaign',expected_revision=1,evidence=evidence('89'),occurred_at=NOW)
    assert result['decision']['action']=='EXIT'


def test_conflicting_event_not_silently_accepted(engine):
    engine.advance('tick','campaign',expected_revision=0,evidence=evidence(),occurred_at=NOW)
    with pytest.raises(LedgerError,match='payload conflict'):
        engine.advance('tick','campaign',expected_revision=0,evidence=evidence('106'),occurred_at=NOW)


def test_runner_replay_no_live_mode(tmp_path):
    source=tmp_path/'input.jsonl'
    source.write_text(json.dumps(dict(operation='open',event_id='o',campaign_id='c',symbol='TEST',
                                      plan_inputs=inputs()))+'\n'+json.dumps(dict(operation='tick',event_id='t',
                                      campaign_id='c',expected_revision=0,evidence=evidence(),occurred_at=NOW))+'\n')
    first=run(source,tmp_path/'ledger.sqlite','shadow')
    second=run(source,tmp_path/'ledger.sqlite','shadow')
    assert first['applied']==2 and second['duplicates']==2
    assert not first['live_ready'] and not first['prospective_capture_connected']
    with pytest.raises(ValueError):
        run(source,source,'shadow')
    process=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'tools/run_scenario_shadow.py'),
                            '--input',str(source),'--ledger',str(tmp_path/'cli.sqlite'),'--book-id','shadow'],
                           capture_output=True,text=True,check=True)
    cli=json.loads(process.stdout)
    assert cli['applied']==2 and cli['mode']=='SHADOW' and not cli['live_ready']


def test_wait_marks_price_and_expired_capacity_not_executable(engine):
    result=engine.advance('wait','campaign',expected_revision=0,evidence=evidence('104'),occurred_at=NOW)
    assert result['decision']['action']=='WAIT'
    assert result['campaigns'][0]['mark_price']=='104'
    future='2026-10-05T14:10:00+00:00'
    quote_only=dict(source='mechanical',quote=dict(price='103',observed_at=future))
    result=engine.advance('expired','campaign',expected_revision=1,evidence=quote_only,occurred_at=future)
    assert result['campaigns'][0]['add_permission']=='ADD_EXPIRED'
    assert result['campaigns'][0]['conditional_remaining_allocation']=='0'


def test_initial_cost_risk_rejected_atomically(tmp_path):
    engine=ScenarioShadowLedger(tmp_path/'risk.sqlite')
    engine.provision('shadow')
    with pytest.raises(ValueError):
        engine.open('open','shadow','c','TEST',plan_inputs=dict(inputs(),initial_stop='99.99'))
    assert engine.ledger.snapshot('shadow')['campaigns']==[]


def test_wait_mark_enforces_book_chronology_across_campaigns(engine):
    engine.advance('mark','campaign',expected_revision=0,evidence=evidence('104'),occurred_at=NOW)
    before=engine.ledger.snapshot('shadow')
    with pytest.raises(LedgerError,match='out-of-order book'):
        engine.open('earlier','shadow','other','OTHER',plan_inputs=inputs())
    assert engine.ledger.snapshot('shadow')==before

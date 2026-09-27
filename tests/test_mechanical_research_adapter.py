import sys
from types import SimpleNamespace
from datetime import datetime,timedelta,timezone

import pandas as pd
import pytest

from tools.replay_mechanical_split_research import ticks_for, run, digest


@pytest.fixture
def fixture(monkeypatch):
    start=datetime(2026,9,1,14,30,tzinfo=timezone.utc)
    schedule=pd.DataFrame({'market_open':[pd.Timestamp(start)],
                           'market_close':[pd.Timestamp(start+timedelta(minutes=30))]})
    monkeypatch.setitem(sys.modules,'pandas_market_calendars',SimpleNamespace(
        get_calendar=lambda name:SimpleNamespace(schedule=lambda *args:schedule)))
    row=dict(decision_ref='d1',ticker='AAA',trigger_type='test',regime='test',policy_version='test',
        entry=dict(observed=True,price_evidence=dict(status='OK',reference_price=100,stop_loss_at_entry=90)),
        outcomes=dict(strategy_entry_at=(start+timedelta(minutes=1)).isoformat(),
                      strategy_closed_at=(start+timedelta(minutes=25)).isoformat(),
                      exit_price_evidence=dict(status='OK',reference_price=110)))
    bars=[dict(provider_timestamp=(start+timedelta(minutes=5*i)).isoformat(),
               open=105,high=110,low=99,close=105,stock_splits=0,dividends=0)
          for i in range(5)]
    response=dict(status='received',exchange='NYQ',raw_rows=bars)
    return row,response


def test_complete_regular_path(fixture):
    row,response=fixture
    ticks,reasons=ticks_for(row,[dict(response=response)])
    assert not reasons
    assert len(ticks)==2
    assert ticks[0].at.isoformat()=='2026-09-01T14:40:00+00:00'


@pytest.mark.parametrize('mutation,expected',[
    ('missing','MISSING_REGULAR_BAR'),('basis','ENTRY_PRICE_BASIS_UNVERIFIED'),
    ('split','CORPORATE_ACTION_UNVERIFIED'),('bad','INVALID_OHLC')])
def test_fail_closed_path(fixture,mutation,expected):
    row,response=fixture
    if mutation=='missing':
        response['raw_rows'].pop(2)
    if mutation=='basis':
        row['entry']['price_evidence']['reference_price']=200
    if mutation=='split':
        response['raw_rows'][1]['stock_splits']=2
    if mutation=='bad':
        response['raw_rows'][1]['low']=120
    _,reasons=ticks_for(row,[dict(response=response)])
    assert expected in reasons


def test_saved_response_reproducibility_and_missing_exit(fixture):
    row,response=fixture
    packet=dict(analysis_contract_version='entry-quality-harness-v2',market='US',
                packet_id='fixture',analysis_rows=[row])
    a=run(packet,lambda request:response)
    assert digest(a)==digest(run(packet,lambda request:response))
    assert a['results'][0]['cost_cases']['10']
    row['outcomes']['exit_price_evidence']['status']='MISSING'
    assert run(packet,lambda request:pytest.fail('must not fetch'))['results'][0]['reasons']==['MISSING_ORIGINAL_PRICE']

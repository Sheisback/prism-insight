from datetime import datetime, timedelta, timezone

import pytest

from prism_core.mechanical_split_research import Tick, simulate

START = datetime(2026, 9, 1, 14, tzinfo=timezone.utc)


def run(prices, **kw):
    args = dict(entry=100, stop=90, entry_at=START,
                exit_at=START + timedelta(days=1), exit_price=110,
                ticks=[Tick(START+timedelta(minutes=10*i), p, p)
                       for i,p in enumerate(prices,1)])
    args.update(kw)
    return simulate(**args)


def test_trailing_unlocks_risk_budget_but_cannot_decrease_stop():
    r = run([105,110,120,130,115], trailing=True)
    assert r['legs'] == [30,60,100]
    assert r['stop_path'] == sorted(r['stop_path'])
    assert r['exit_reason'] == 'MECHANICAL_STOP'
    assert r['spent'] <= 1


def test_without_trailing_risk_blocks_full_add():
    r = run([105,110,120])
    assert r['target'] < 100
    assert 'CASH_OR_RISK_LIMIT' in r['decisions']


def test_overshoot_cannot_chase_or_skip_stage():
    assert run([150,160])['legs'] == []


def test_one_tick_one_stage_and_exact_duplicate_idempotent():
    t = Tick(START+timedelta(minutes=10),110,110)
    assert run([],ticks=[t,t]) == run([],ticks=[t])
    assert len(run([],ticks=[t])['legs']) == 1


def test_conflicting_tick_fails():
    t=START+timedelta(minutes=10)
    with pytest.raises(ValueError, match='CONFLICTING'):
        run([], ticks=[Tick(t,105,105),Tick(t,106,106)])


def test_stop_and_gap_have_priority_over_add():
    t=START+timedelta(minutes=10)
    r=run([], ticks=[Tick(t,105,85)])
    assert r['legs'] == []
    assert r['exit_reason'] == 'MECHANICAL_STOP'
    assert r['pnl'] < -.015


def test_plan_expiry_does_not_disable_protection():
    t=START+timedelta(days=6)
    r=run([],ticks=[Tick(t,105,105),Tick(t+timedelta(minutes=10),80,80)],
          exit_at=START+timedelta(days=7))
    assert r['legs'] == []
    assert r['exit_reason'] == 'MECHANICAL_STOP'


def test_original_exit_tie_prevents_add():
    t=START+timedelta(minutes=10)
    r=run([], ticks=[Tick(t,105,105)],exit_at=t)
    assert r['legs'] == []
    assert r['exit_reason'] == 'ORIGINAL_EXIT'


@pytest.mark.parametrize('cost',[10,25])
def test_whipsaw_counterexample(cost):
    prices=[105,110,120,109,130]
    a=run(prices,trailing=False,exit_price=130,cost_bps=cost)
    b=run(prices,trailing=True,exit_price=130,cost_bps=cost)
    assert b['exit_reason'] == 'MECHANICAL_STOP'
    assert b['pnl'] < a['pnl']


@pytest.mark.parametrize('p',[0,-1,float('nan'),float('inf')])
def test_invalid_prices(p):
    with pytest.raises(ValueError):
        run([p])

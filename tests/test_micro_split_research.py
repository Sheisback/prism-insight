from datetime import datetime, timedelta, timezone

import pytest

from prism_core.micro_split_research import Batch, replay

START = datetime(2026, 9, 1, 14, tzinfo=timezone.utc)


def batch(n, signal, execution=None, ref=None):
    t = START + timedelta(hours=n)
    return Batch(ref or str(n), t, t - timedelta(minutes=1), signal,
                 t + timedelta(minutes=5), signal if execution is None else execution)


def run(batches=(), **kwargs):
    args = dict(entry=100, stop=90, exit_price=90, entry_at=START,
                exit_at=START + timedelta(hours=10), entry_batch="initial", batches=batches)
    args.update(kwargs)
    return replay(**args)


@pytest.mark.parametrize("cost", [10, 25])
def test_early_loss_scales_but_does_not_prove_alpha(cost):
    r = run(cost_bps=cost)
    assert r["experiment_pnl"] == pytest.approx(.1 * r["baseline_pnl"])
    assert r["difference"] > 0
    assert not r["historical_validation"]


def test_fast_winner_is_counterexample():
    r = run(exit_price=150)
    assert r["difference"] < 0
    assert r["experiment_pnl"] / r["baseline_pnl"] == pytest.approx(.1)


def test_stages_and_cash():
    r = run([batch(1, 105), batch(2, 110), batch(3, 120)], exit_price=130)
    assert r["transitions"] == [30, 60, 100]
    assert r["spent"] <= 1


def test_one_stage_per_batch_and_duplicate_idempotency():
    b = batch(1, 150)
    assert run([b, b]) == run([b])
    assert run([b])["transitions"] == [30]


def test_conflicting_batch_rejected():
    with pytest.raises(ValueError, match="CONFLICTING_BATCH"):
        run([batch(1, 105), batch(1, 106)])


def test_gap_does_not_advance():
    r = run([batch(1, 110, 99), batch(2, 104)])
    assert r["target"] == 10


def test_exit_wins_over_add():
    r = run([batch(1, 110)], exit_at=START + timedelta(hours=1, minutes=5))
    assert r["target"] == 10
    assert r["reasons"] == ["EXIT_FIRST"]


def test_initial_batch_cannot_add():
    assert run([batch(1, 150, ref="initial")])["target"] == 10


@pytest.mark.parametrize("stop", [0, 100, 101, float("nan")])
def test_invalid_r(stop):
    with pytest.raises(ValueError):
        run(stop=stop)


@pytest.mark.parametrize("offset,reason", [(-11, "STALE"), (1, "LOOKAHEAD")])
def test_bad_signal_clock(offset, reason):
    b = batch(1, 110)
    b = Batch(b.ref, b.decision, b.decision + timedelta(minutes=offset),
              b.signal_price, b.execution_start, b.execution_price)
    with pytest.raises(ValueError, match=reason):
        run([b])


def test_same_time_fill_is_lookahead():
    b = batch(1, 110)
    with pytest.raises(ValueError, match="LOOKAHEAD"):
        run([Batch(b.ref, b.decision, b.signal_end, 110, b.decision, 110)])

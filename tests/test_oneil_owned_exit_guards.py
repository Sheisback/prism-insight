"""Owned US exits must not also submit the old whole-account SELL."""
import asyncio
from types import SimpleNamespace

import pytest

from tools import hardstop_seller, trend_exit_seller


@pytest.mark.parametrize("module", [hardstop_seller, trend_exit_seller])
def test_owned_close_skips_legacy_order_and_signal(monkeypatch, module):
    calls = []
    monkeypatch.setattr(module, "has_open_inflight", lambda *a: False)
    monkeypatch.setattr(module, "claim_lock", lambda *a: True)
    monkeypatch.setattr(module, "record_inflight", lambda *a: calls.append(("record", a[5])))
    monkeypatch.setattr(module, "release_lock", lambda *a, **kw: calls.append(("release", kw["new_state"])))
    monkeypatch.setattr(module, "HARDSTOP_LIVE" if module is hardstop_seller else "TREND_EXIT_LIVE", True)
    if module is trend_exit_seller:
        monkeypatch.setattr(module, "MIN_HOLD_MIN", 0)
    monkeypatch.setattr(module, "_open_context", lambda *a, **kw: pytest.fail("legacy broker cannot open"))

    async def sell(stock, reason, **kwargs):
        stock["_oneil_owned_exit"] = True
        calls.append(("strategy_close", stock["ticker"]))
        return True

    async def notify(*a, **kw):
        calls.append(("notify", True))

    agent = dict(ref=SimpleNamespace(sell_stock=sell, send_telegram_message=notify))
    summary = dict(sold=0, skipped=0)
    args = [None, "US", "TEST", {"ticker": "TEST"}, "stop"]
    if module is trend_exit_seller:
        args.append(2)
    asyncio.run(module._act_on_trigger(*args, "run1", agent, summary))
    assert summary["sold"] == summary["owned_exit_delegated"] == 1
    assert ("record", "OWNED_EXIT_PENDING") in calls
    assert ("release", "SOLD") in calls

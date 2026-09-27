"""Post-entry peak ratchet shared by the KR/US SELL agents and the trend-exit loop."""
from __future__ import annotations


def ratchet_highest_price(scenario: dict, buy_price: float, current_price: float) -> tuple[float, bool]:
    """Return (peak, must_persist) and write the peak into ``scenario`` when it changed.

    A missing peak is initialised to max(buy, current) and must be persisted too;
    before 2026-09-27 only a *raise* was saved, so an initialised peak was never
    stored and every run re-initialised it to the current price.
    """
    stored = scenario.get("highest_price")
    try:
        peak = float(stored) if stored is not None else None
    except (TypeError, ValueError):
        peak = None
    if peak is None or peak <= 0:
        peak = max(float(buy_price or 0), float(current_price or 0))
        scenario["highest_price"] = peak
        return peak, peak > 0
    if current_price > peak:
        scenario["highest_price"] = float(current_price)
        return float(current_price), True
    return peak, False

"""Preregistered synthetic counterexamples, not observations or market evidence."""
from copy import deepcopy

from test_oneil_paired_replay import payload


def comparison_cases():
    result = payload()
    result["campaigns"] = []
    for name, price, stop, terminal in (
        ("confirmed_rise", "102", "90", "120"),
        ("strong_rise", "104", "100", "120"),
        ("unchanged_stop_rise", "104", "90", "120"),
        ("initial_failure", "89", "90", "89"),
        ("failure_after_add", "104", "100", "99"),
        ("weak_volume_winner", "104", "90", "130"),
        ("extended_winner", "106", "90", "130"),
        ("missing_current_gates", "104", "90", "120"),
    ):
        campaign = payload(exit_price=terminal, tick_price=price)["campaigns"][0]
        campaign["campaign_id"] = name
        tick = campaign["ticks"][0]
        tick["current_stop"] = stop
        if name == "failure_after_add":
            failure = deepcopy(tick)
            failure.update(occurred_at="2026-09-25T13:42:00Z",
                           available_at="2026-09-25T13:42:00Z")
            failure["evidence"]["quote"].update(
                price="99", observed_at="2026-09-25T13:41:30Z")
            failure["evidence"]["gates"]["observed_at"] = "2026-09-25T13:41:30Z"
            campaign["ticks"].append(failure)
        elif name == "weak_volume_winner":
            tick["evidence"]["volume"]["cumulative_volume"] = 100
        elif name == "missing_current_gates":
            del tick["evidence"]["gates"]
        result["campaigns"].append(campaign)
    return result

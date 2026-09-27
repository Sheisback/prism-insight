from tools import replay_buy_decision_facts as T


def _record(ticker, a_allowed, b_allowed, a_score=2, b_score=2, a_mom=1, b_mom=1):
    arm = lambda allowed, score, mom: {"decision": "진입" if allowed else "미진입", "buy_score": score,  # noqa: E731
                                       "momentum_signal_count": mom, "additional_confirmation_count": 0,
                                       "gate": {"entering": allowed, "allowed": allowed}}
    return {"ticker": ticker, "decided_at": "2026-09-10 09:40:00", "outcome": {"14d": 0.05},
            "A": arm(a_allowed, a_score, a_mom), "B": arm(b_allowed, b_score, b_mom)}


def test_summary_counts_flips_and_deltas():
    records = [_record("A", False, False, b_mom=2), _record("B", False, True, b_score=7), {"ticker": "C", "error": "x"}]
    out = T.report_summary(records)
    assert out["completed"] == 2 and out["errors"] == 1
    assert out["final_entry_A"] == 0 and out["final_entry_B"] == 1
    assert [f["ticker"] for f in out["final_flips"]] == ["B"]
    assert out["mean_delta_momentum"] == 0.5 and out["mean_delta_buy_score"] == 2.5


def test_prompt_keeps_arms_identical_except_the_block():
    item = {"trigger_type": "거래량 급증 상위주", "trigger_mode": "morning", "trend_facts": "### 📉 facts"}
    a = T.user_prompt(item, "KR", "REPORT")
    b = T.user_prompt(item, "KR", "REPORT", facts="### 📐 block\n")
    assert b.replace("### 📐 block\n", "") == a
    assert "past holdings not reconstructed" in a


def test_non_entry_skips_gate():
    assert T.gate({"decision": "미진입"}, {"price": 1, "regime": "sideways", "trend_facts": ""}) == {
        "entering": False, "allowed": False, "reason": "llm_no_entry"}

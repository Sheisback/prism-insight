"""Pure functions of the preregistered confirmed-resistance replay (research tool)."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("replay_crt", ROOT / "tools/replay_confirmed_resistance_target.py")
crt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(crt)


def _bars(highs, start_day=1):
    return [{"date": f"2026-08-{i + start_day:02d}", "high": h, "low": h * 0.97, "close": h * 0.99}
            for i, h in enumerate(highs)]


def test_todays_high_is_never_a_candidate_and_next_confirmed_pivot_is_used():
    # HPSP shape: June spike to 92k then a crash with no pivots, base ~57k; today (61.5k) is not an input.
    highs = [70, 80, 92, 80, 70, 66, 60, 62, 63.5, 62, 61, 59, 58, 55, 50, 48, 47, 50, 52, 55, 60.3, 55, 54, 55, 56, 57.2, 56]
    confirmed = _bars(highs)
    new = crt.confirmed_target(60.9, confirmed)
    assert new["basis"] == "structural" and new["resistance"] == 62  # nearest traded-above high
    assert abs(new["target"] - (60.9 + 0.8 * (62 - 60.9))) < 1e-9


def test_breakout_near_52w_high_uses_oneil_2a():
    highs = [50] * 10 + [70] + [60] * 10
    new = crt.confirmed_target(68.0, _bars(highs))  # only the 52w high sits above
    assert new["basis"] == "oneil_2a" and abs(new["target"] - 81.6) < 1e-9


def test_no_confirmed_resistance_is_missing_not_pass():
    highs = [50] * 10 + [70] + [60] * 10
    new = crt.confirmed_target(80.0, _bars(highs))
    assert new["target"] is None and new["basis"].startswith("MISSING")


def test_us_session_date_converts_kst_record_to_new_york():
    assert crt.session_date("US", "2026-09-29 04:00:43") == "2026-09-28"
    assert crt.session_date("KR", "2026-09-29 15:21:51") == "2026-09-29"


def test_outcome_is_missing_until_ten_sessions_mature():
    after = _bars([100] * 9)
    assert crt.outcome(100, 110, 95, after)["status"] == "MISSING_immature"
    after = _bars([100, 100, 111] + [100] * 7)
    assert crt.outcome(100, 110, 95, after)["first"] == "target"

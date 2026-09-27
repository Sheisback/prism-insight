import json
import sqlite3
from datetime import date, timedelta

from observability import reentry_v2_shadow as V2
from prism_core import pivot_reentry as P


def _bars(rows, start=date(2026, 1, 1)):
    out, day = [], start
    for o, h, low, c, v in rows:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        out.append({"date": day.isoformat(), "open": o, "high": h, "low": low, "close": c, "volume": v})
        day += timedelta(days=1)
    return out


def _series():
    """Uptrend, base under a 110 pivot, a skip decision inside the base, breakout, then a run-up."""
    rows = [(80 + i * 0.5, 80.5 + i * 0.5, 79.5 + i * 0.5, 80 + i * 0.5, 1000) for i in range(55)]
    rows.append((108, 110, 107, 109, 1500))
    for j in range(15):
        c = 104 + (j % 3)
        rows.append((c, c + 1, c - 1, c, 900))
    rows.append((106, 106.8, 105.5, 106.5, 900))                  # decision day (index 71)
    rows.append((106.5, 107.5, 106, 107, 900))
    rows.append((107.5, 112, 107, 111, 1500))                     # breakout: high > 110, volume >= avg
    rows += [(111 + k * 0.3, 112 + k * 0.3, 110.5 + k * 0.3, 111.5 + k * 0.3, 1000) for k in range(70)]
    return _bars(rows)


def _row(bars, source="LOCATION_SKIP"):
    day = bars[71]
    return {"source": source, "account_key": "analysis", "ticker": "000001", "company_name": "A",
            "entry_date": day["date"], "entry_price": day["close"], "exit_date": day["date"],
            "exit_price": day["close"], "trigger_type": "t", "exit_kind": None, "buy_score": 6, "min_score": 7,
            "skip_reason": "추세 게이트 T1", "decision_id": "d1"}


def _state():
    return {"schema_version": 1, "policy_version": P.POLICY_VERSION, "market": "KR", "watches": []}


def test_trigger_freezes_recheck_inputs_once_and_is_idempotent(tmp_path):
    bars = _series()
    frames = {"000001": bars, "__benchmark_rows": {"000001": bars}}
    completed = bars[-1]["date"]
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / f"000001_A_{bars[60]['date'].replace('-', '')}_morning_x.md").write_text("REPORT")
    state, frozen = V2.advance(_state(), [_row(bars)], frames, completed, "KR", reports_root=tmp_path, archive_db=None)
    assert len(frozen) == 1
    watch, item = frozen[0]
    assert watch["status"] in {"TRIGGERED", "CLOSED"} and item["trigger_date"] == bars[73]["date"]
    assert item["llm_recheck"] == "NOT_EVALUATED" and item["report_ref"]["kind"] == "file"
    assert item["report_ref"]["report_date"] == bars[60]["date"] and item["report_ref"]["age_days"] == (
        date.fromisoformat(bars[73]["date"]) - date.fromisoformat(bars[60]["date"])).days
    assert item["report_ref"]["stale"] is False
    assert item["levels"]["support_pivot"] == 110 and item["levels"]["measured_move_target"] > 110
    assert "📏 트리거 시점 가격 수준" in item["facts_text"] and item["original"]["reason"] == "추세 게이트 T1"
    _, again = V2.advance(state, [_row(bars)], frames, completed, "KR", reports_root=tmp_path, archive_db=None)
    assert again == [] and len(state["watches"]) == 1


def test_price_basis_mismatch_and_missing_bars_are_explicit():
    bars = _series()
    row = dict(_row(bars), entry_price=500.0)
    state, frozen = V2.advance(_state(), [row], {"000001": bars}, bars[-1]["date"], "KR", archive_db=None)
    assert frozen == [] and state["watches"][0]["status"] == "MISSING_FINAL"
    state, _ = V2.advance(_state(), [_row(bars)], {}, bars[-1]["date"], "KR", archive_db=None)
    assert state["watches"][0]["status"] == "PENDING_ENROLL"


def test_fresh_levels_math():
    levels = V2.fresh_levels(110.0, {"pivot": 110.0, "base_low": 100.0})
    assert levels["measured_move_target"] == 120.0 and levels["stop_used"] == 102.3
    assert levels["rr"] == round(10 / 7.7, 2)


def _db(path, bars):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE trading_history (account_key TEXT, ticker TEXT, company_name TEXT, buy_date TEXT, "
                 "buy_price REAL, sell_date TEXT, sell_price REAL, profit_rate REAL, trigger_type TEXT, exit_kind TEXT)")
    conn.execute("CREATE TABLE watchlist_history (id INTEGER PRIMARY KEY, ticker TEXT, company_name TEXT, "
                 "analyzed_date TEXT, current_price REAL, buy_score INTEGER, min_score INTEGER, decision TEXT, "
                 "skip_reason TEXT, trigger_type TEXT, scenario TEXT, was_traded INTEGER DEFAULT 0)")
    day = bars[71]
    conn.execute("INSERT INTO watchlist_history (ticker, company_name, analyzed_date, current_price, buy_score, "
                 "min_score, decision, skip_reason, trigger_type, scenario) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ("000001", "A", day["date"] + " 09:40:00", day["close"], 7, 7, "Enter", "게이트 차단", "t",
                  json.dumps({"fundamental_check": {"all_passed": True}})))
    conn.commit()
    conn.close()


def test_run_dry_run_writes_nothing_and_real_run_freezes(tmp_path, monkeypatch):
    bars = _series()
    db = tmp_path / "t.sqlite"
    _db(db, bars)
    monkeypatch.setattr(V2, "LOOKBACK_DAYS", 400)
    sent = []
    monkeypatch.setattr(V2, "emit_event", lambda name, **kw: sent.append((name, kw)) or {"ok": 1})
    collector = lambda tickers, completed: {"000001": bars, "__benchmark_rows": {"000001": bars}}  # noqa: E731
    root = tmp_path / "rt"
    summary = V2.run("KR", bars[-1]["date"], collector=collector, db_path=db, root=root, reports_root=tmp_path,
                     archive_db=None, dry_run=True, llm_recheck=False)
    assert summary["new_triggers"] == 1 and summary["llm_calls"] == 0
    assert [f.name for f in root.iterdir()] == ["reentry_v2_state_kr.lock"]     # only the run lock
    assert sent == []
    summary = V2.run("KR", bars[-1]["date"], collector=collector, db_path=db, root=root, reports_root=tmp_path,
                     archive_db=None, llm_recheck=False)
    frozen = [json.loads(line) for line in (root / "reentry_v2_recheck_inputs_kr.jsonl").read_text().splitlines()]
    assert len(frozen) == 1 and frozen[0]["source"] == "ENTER_BLOCKED"
    assert [n for n, _ in sent] == ["reentry_v2.shadow_trigger", "reentry_v2.shadow_run"]
    from observability.events import build_event
    for _, kw in sent:
        assert "[REDACTED]" not in json.dumps(build_event("x", service="s", attributes=kw["attributes"])["attributes"])
    sent.clear()
    V2.run("KR", bars[-1]["date"], collector=collector, db_path=db, root=root, reports_root=tmp_path, archive_db=None,
           llm_recheck=False)
    assert [n for n, _ in sent] == ["reentry_v2.shadow_run"]
    assert len((root / "reentry_v2_recheck_inputs_kr.jsonl").read_text().splitlines()) == 1


def test_enabled_requires_exact_policy(tmp_path, monkeypatch):
    path = tmp_path / "p.json"
    monkeypatch.setattr(V2, "POLICY_PATH", path)
    assert not V2.enabled("KR")
    path.write_text(json.dumps(V2.POLICY))
    assert V2.enabled("KR") and V2.enabled("US")
    monkeypatch.setenv("REENTRY_V2_SHADOW_ENABLED", "false")
    assert not V2.enabled("KR")


def test_report_lookup_is_strictly_before_the_trigger_day(tmp_path):
    from observability.reentry_recheck_inputs import latest_report
    folder = tmp_path / "reports"
    folder.mkdir()
    for name in ("000001_A_20260820_morning_x.md", "000001_A_20260901_morning_x.md",
                 "000001_A_20260901_afternoon_x.md", "000001_A_20260831_morning_x_en.md"):
        (folder / name).write_text("r")
    assert latest_report(tmp_path, "KR", "000001", "2026-09-01").name == "000001_A_20260820_morning_x.md"
    assert latest_report(tmp_path, "KR", "000001", "2026-09-02").name == "000001_A_20260901_afternoon_x.md"
    assert latest_report(tmp_path, "KR", "000001", "2026-08-20") is None


def test_archive_lookup_is_strictly_before_the_trigger_day(tmp_path):
    from observability.reentry_recheck_inputs import archived_report
    db = tmp_path / "a.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE report_archive (market TEXT, ticker TEXT, language TEXT, report_date TEXT, mode TEXT, "
                 "model TEXT, content TEXT)")
    conn.executemany("INSERT INTO report_archive VALUES (?,?,?,?,?,?,?)",
                     [("us", "AAA", "ko", "2026-07-01", "morning", "m", "old"),
                      ("us", "AAA", "ko", "2026-08-10", "afternoon", "m", "same-day")])
    conn.commit()
    conn.close()
    report = archived_report(db, "US", "AAA", "2026-08-10")
    assert report.read_text() == "old" and "_20260701_" in report.name


def _forward_setup(tmp_path, monkeypatch):
    """Shadow starts before the breakout, so the trigger is forward evidence."""
    bars = _series()
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "t.sqlite"
    _db(db, bars)
    (tmp_path / "reports").mkdir(exist_ok=True)
    (tmp_path / "reports" / f"000001_A_{bars[60]['date'].replace('-', '')}_morning_x.md").write_text("REPORT")
    monkeypatch.setattr(V2, "LOOKBACK_DAYS", 400)
    monkeypatch.setattr(V2.RC, "recheck_instruction", lambda market: "SYS")
    sent = []
    monkeypatch.setattr(V2, "emit_event", lambda name, **kw: sent.append((name, kw)) or {"ok": 1})
    collector = lambda tickers, completed: {"000001": bars, "__benchmark_rows": {"000001": bars}}  # noqa: E731
    root = tmp_path / "rt"

    def run(day, **kw):
        return V2.run("KR", bars[day]["date"], collector=collector, db_path=db, root=root, reports_root=tmp_path,
                      archive_db=None, **kw)
    return bars, root, sent, run


def _fake_llm(calls, reply='{"decision": "진입", "buy_score": 8, "min_score": 7}'):
    async def llm(system, user):
        calls.append((system, user))
        if isinstance(reply, Exception):
            raise reply
        return reply, {"model": "m", "reasoning_effort": "high", "latency_s": 1.0}
    return llm


def test_forward_trigger_is_rechecked_once_with_frozen_inputs(tmp_path, monkeypatch):
    bars, root, sent, run = _forward_setup(tmp_path, monkeypatch)
    calls = []
    assert run(72, llm_recheck=True, llm=_fake_llm(calls))["rechecks"] == 0     # enrolled, no trigger yet
    summary = run(len(bars) - 1, llm_recheck=True, llm=_fake_llm(calls))
    assert summary["llm_calls"] == 1 and summary["recheck_status"] == {"OK": 1}
    system, user = calls[0]
    assert system == "SYS" and "📏 트리거 시점 가격 수준" in user and "REPORT" in user and "게이트 차단" in user
    result = json.loads((root / "reentry_v2_recheck_results_kr.jsonl").read_text())
    assert result["approved"] is True and result["buy_score"] == 8 and result["report_stale"] is False
    assert "reentry_v2.shadow_recheck" in [n for n, _ in sent]
    from observability.events import build_event
    attrs = next(kw["attributes"] for n, kw in sent if n == "reentry_v2.shadow_recheck")
    assert "[REDACTED]" not in json.dumps(build_event("x", service="s", attributes=attrs)["attributes"])
    assert run(len(bars) - 1, llm_recheck=True, llm=_fake_llm(calls))["llm_calls"] == 0
    assert len(calls) == 1


def test_backfilled_trigger_and_dry_run_never_call_the_llm(tmp_path, monkeypatch):
    bars, root, _, run = _forward_setup(tmp_path, monkeypatch)
    calls = []
    assert run(len(bars) - 1, llm_recheck=True, llm=_fake_llm(calls))["llm_calls"] == 0   # trigger before start
    bars2, root2, _, run2 = _forward_setup(tmp_path / "b", monkeypatch)
    run2(72, llm_recheck=False)
    assert run2(len(bars2) - 1, dry_run=True, llm_recheck=True, llm=_fake_llm(calls))["llm_calls"] == 0
    assert calls == []


def test_failed_recheck_is_retried_once_on_a_later_run(tmp_path, monkeypatch):
    bars, root, _, run = _forward_setup(tmp_path, monkeypatch)
    calls = []
    boom = _fake_llm(calls, RuntimeError("down"))
    run(72, llm_recheck=True, llm=boom)
    assert run(len(bars) - 2, llm_recheck=True, llm=boom)["recheck_status"] == {"ERROR": 1}
    assert run(len(bars) - 2, llm_recheck=True, llm=boom)["llm_calls"] == 0          # same day: no retry
    assert run(len(bars) - 1, llm_recheck=True, llm=boom)["recheck_status"] == {"ERROR": 1}
    assert run(len(bars) - 1, llm_recheck=True, llm=boom)["llm_calls"] == 0          # attempts exhausted
    assert len(calls) == 2


def test_changed_report_is_not_sent_to_the_llm(tmp_path, monkeypatch):
    bars, root, _, run = _forward_setup(tmp_path, monkeypatch)
    calls = []
    run(72, llm_recheck=False)
    report = tmp_path / "reports" / f"000001_A_{bars[60]['date'].replace('-', '')}_morning_x.md"
    V2.run("KR", bars[-2]["date"], collector=lambda t, c: {"000001": bars, "__benchmark_rows": {"000001": bars}},
           db_path=tmp_path / "t.sqlite", root=root, reports_root=tmp_path, archive_db=None, llm_recheck=False)
    report.write_text("EDITED")
    assert run(len(bars) - 1, llm_recheck=True, llm=_fake_llm(calls))["recheck_status"] == {"REPORT_CHANGED": 1}
    assert calls == []


def test_llm_recheck_switches(tmp_path, monkeypatch):
    path = tmp_path / "p.json"
    monkeypatch.setattr(V2, "POLICY_PATH", path)
    assert not V2.llm_recheck_enabled()
    path.write_text(json.dumps(V2.POLICY))
    assert V2.llm_recheck_enabled()
    monkeypatch.setenv("REENTRY_V2_LLM_RECHECK", "0")
    assert not V2.llm_recheck_enabled()

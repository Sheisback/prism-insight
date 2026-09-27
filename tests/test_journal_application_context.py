"""Offline checks for the same candidate across all three memory injection paths."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import types

import pytest

from tracking.db_schema import TABLE_TRADING_INTUITIONS, TABLE_TRADING_JOURNAL, TABLE_TRADING_PRINCIPLES
from tracking.journal import JournalManager
from trading_memory_policy import ensure_application_columns


def context(market, **changes):
    return dict(version=1, status="current_pipeline", market=market, stage="batch_buy",
                required_capabilities=["batch_report", "entry_advisory"],
                reason="Check only supplied evidence against existing entry policy.", **changes)


@pytest.fixture(params=["KR", "US"])
def manager(request):
    market = request.param
    conn = sqlite3.connect(":memory:")
    for schema in (TABLE_TRADING_INTUITIONS, TABLE_TRADING_JOURNAL, TABLE_TRADING_PRINCIPLES):
        conn.execute(schema)
    for table in ("trading_intuitions", "trading_journal", "trading_principles"):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN market TEXT DEFAULT 'KR'")
    ensure_application_columns(conn)
    if market == "KR":
        cls = JournalManager
    else:
        path = Path(__file__).parents[1] / "prism-us/tracking/journal.py"
        spec = importlib.util.spec_from_file_location("us_journal_applicability", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls = module.USJournalManager
    mgr = cls(conn.cursor(), conn, enable_journal=True)
    mgr.get_performance_tracker_stats = lambda trigger_type=None: {}
    yield mgr, market
    conn.close()


def seed(mgr, market, label, metadata):
    encoded = json.dumps(metadata) if metadata is not None else None
    mgr.conn.execute("""INSERT INTO trading_principles
        (scope, condition, action, priority, confidence, supporting_trades, created_at, market, application_context)
        VALUES ('universal', ?, ?, 'high', .9, 3, '2026-01-01', ?, ?)""", (label, label, market, encoded))
    mgr.conn.execute("""INSERT INTO trading_intuitions
        (category, condition, insight, confidence, created_at, market, application_context)
        VALUES ('pattern', ?, ?, .9, '2026-01-01', ?, ?)""", (label, label, market, encoded))


def test_all_three_paths_keep_current_and_separate_future_and_legacy(manager):
    mgr, market = manager
    allowed = context(market)
    labels = {"Check supplied support and trend now": allowed,
              "Automatically enter tomorrow": {**allowed, "status": "improvement"},
              "Resize to arbitrary 13 percent": {**allowed, "status": "improvement"},
              "Install a new numeric BUY threshold": {**allowed, "status": "improvement"},
              "Future exit rule as BUY veto": {**allowed, "stage": "position_management"},
              "Legacy advice is not approved": None,
              "Wrong market advice": {**allowed, "market": "US" if market == "KR" else "KR"},
              "Unknown capability instruction": {**allowed, "required_capabilities": ["autonomous_later_entry"]}}
    for label, metadata in labels.items():
        seed(mgr, market, label, metadata)
    lessons = [{"action": label, "application_context": metadata} for label, metadata in reversed(labels.items())]
    mgr.conn.execute("""INSERT INTO trading_journal
        (ticker, company_name, trade_date, trade_type, profit_rate, holding_days, one_line_summary, lessons, created_at, market)
        VALUES ('SAME', 'Same candidate', '2026-01-01', 'sell', -2, 3, 'Historical sale fact', ?, '2026-01-01', ?)""", (json.dumps(lessons), market))
    output = mgr.get_context_for_ticker("SAME")
    assert output.count("Check supplied support and trend now") >= 3
    for rejected in list(labels)[1:]:
        assert rejected not in output
    assert "Historical sale fact" in output and "-2.0%" in output
    assert "not executable instructions" in output
    assert mgr.conn.execute("SELECT COUNT(*) FROM trading_intuitions").fetchone()[0] == len(labels)


def test_new_high_priority_lesson_is_not_automatically_universal_or_approved(manager):
    mgr, market = manager
    lessons = [{"condition": "Candidate support", "action": "Read supplied support evidence",
                "priority": "high", "application_context": context(market)}]
    assert mgr.extract_principles(lessons, 1) == 1
    scope, metadata = mgr.conn.execute("SELECT scope, application_context FROM trading_principles").fetchone()
    assert scope != "universal"
    assert json.loads(metadata)["status"] == "unreviewed"
    assert mgr.get_universal_principles() == []


def test_filtered_high_confidence_rows_do_not_starve_approved_memory(manager):
    mgr, market = manager
    for index in range(12):
        seed(mgr, market, f"Unreviewed {index}", None)
    seed(mgr, market, "Retained lower ranked current check", context(market))
    mgr.conn.execute("UPDATE trading_principles SET confidence=.6 WHERE condition LIKE 'Retained%'")
    mgr.conn.execute("UPDATE trading_intuitions SET confidence=.6 WHERE condition LIKE 'Retained%'")
    result = mgr.get_context_for_ticker("SAME")
    assert "Retained lower ranked current check" in result
    assert "Unreviewed" not in result


def test_new_journal_proposal_cannot_self_authorize(manager):
    mgr, market = manager
    proposed = context(market)
    data = {"lessons": [{"action": "Read support now", "application_context": proposed}]}
    mgr._save_to_database("SAME", "Same candidate", 100, "2026-01-01", "{}", {},
                          98, "Historical stop", -2, 3, data)
    stored = json.loads(mgr.conn.execute("SELECT lessons FROM trading_journal").fetchone()[0])[0]
    assert stored["action"] == "Read support now"
    assert stored["application_context"]["status"] == "unreviewed"
    assert stored["proposed_application_context"] == proposed
    assert data["lessons"][0]["application_context"] == proposed


def test_read_only_legacy_schema_keeps_facts_without_migration(manager):
    mgr, market = manager
    mgr._save_to_database("SAME", "Same candidate", 100, "2026-01-01", "{}", {},
                          98, "Historical stop", -2, 3, {"one_line_summary": "Historical sale fact"})
    mgr.conn.execute("ALTER TABLE trading_principles DROP COLUMN application_context")
    mgr.conn.execute("ALTER TABLE trading_intuitions DROP COLUMN application_context")
    mgr.conn.commit()
    mgr.conn.execute("PRAGMA query_only=ON")
    readonly = type(mgr)(mgr.conn.cursor(), mgr.conn, enable_journal=True)
    readonly.get_performance_tracker_stats = lambda trigger_type=None: {}
    output = readonly.get_context_for_ticker("SAME")
    assert "Historical sale fact" in output and "-2.0%" in output
    assert "application_context" not in {r[1] for r in mgr.conn.execute("PRAGMA table_info(trading_intuitions)")}


def test_principle_evidence_is_idempotent_and_preserves_review(manager):
    mgr, market = manager
    approved = context(market)
    seed(mgr, market, "Check support", approved)
    mgr.conn.execute("UPDATE trading_principles SET source_journal_ids='11,12', supporting_trades=2")
    before = mgr.conn.execute("SELECT confidence, supporting_trades, application_context FROM trading_principles").fetchone()
    assert mgr._save_principle("universal", None, "Check support", "Check support", "evidence", "high", 12)
    assert mgr.conn.execute("SELECT confidence, supporting_trades, application_context FROM trading_principles").fetchone() == before
    assert mgr._save_principle("universal", None, "Check support", "Check support", "evidence", "high", 13)
    rows = mgr.conn.execute("SELECT supporting_trades, application_context FROM trading_principles").fetchall()
    assert rows == [(3, json.dumps(approved))]


def test_physical_market_mismatch_cannot_bypass_metadata_market(manager):
    mgr, market = manager
    seed(mgr, "US" if market == "KR" else "KR", "Wrong physical market", context(market))
    assert "Wrong physical market" not in mgr.get_context_for_ticker("SAME")


@pytest.mark.parametrize("market", ["KR", "US"])
@pytest.mark.parametrize("language", ["ko", "en"])
def test_journal_generation_contract_prioritizes_current_capabilities(monkeypatch, market, language):
    stub = types.ModuleType("mcp_agent.agents.agent")
    stub.Agent = lambda **kwargs: kwargs
    monkeypatch.setitem(sys.modules, "mcp_agent.agents.agent", stub)
    path = Path(__file__).parents[1] / "cores/agents/trading_journal_agent.py"
    spec = importlib.util.spec_from_file_location("journal_generation_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.create_trading_journal_agent(language, market)
    assert "sqlite" not in result["server_names"]
    instruction = result["instruction"]
    assert "Most lessons must concern checks practicable NOW" in instruction
    assert "at most" in instruction and "one distinct" in instruction
    assert "independent offline review" in instruction
    assert '"market": "' + market + '"' in instruction
    assert "7%" not in instruction
    assert "automatic entry" in instruction and "arbitrary smaller sizing" in instruction
    if market == "US":
        assert "KOSPI/KOSDAQ" not in instruction

"""Offline applicability boundary tests; no network, broker or message calls."""
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tracking.compression import CompressionManager
from tracking.db_schema import TABLE_TRADING_INTUITIONS, TABLE_TRADING_PRINCIPLES
from trading_memory_policy import ensure_application_columns, is_current_buy_memory, normalize_application_context
from test_intuition_deduplication import stub_llm_dependencies


def context(status='current_pipeline', stage='batch_buy', capabilities=None):
    return {'version': 1, 'status': status, 'market': 'KR', 'stage': stage,
            'required_capabilities': capabilities if capabilities is not None else ['batch_report', 'entry_advisory'],
            'reason': 'Compare the supplied batch evidence without adding gates.'}


def manager():
    conn = sqlite3.connect(':memory:')
    conn.executescript(TABLE_TRADING_INTUITIONS + ';' + TABLE_TRADING_PRINCIPLES)
    conn.execute('CREATE TABLE trading_journal (id INTEGER PRIMARY KEY, lessons TEXT, market TEXT)')
    return CompressionManager(conn.cursor(), conn, enable_journal=True)


def seed(mgr):
    mgr._save_intuition({'condition': '현재 보고서의 추세와 위험 점검', 'insight': '기존 매수 판단에 참고', 'source_journal_ids': [1, 2]}, [1, 2])
    mgr.conn.execute("INSERT INTO trading_principles(condition,action,created_at) VALUES ('새 초단위 데이터 필요','새 공급자 연동','2026-09-27')")
    lesson = {'condition': '현재 보고서 검토', 'action': '가용한 근거를 확인', 'priority': 'high'}
    mgr.conn.execute('INSERT INTO trading_journal VALUES (1, ?, ?)', (json.dumps([lesson], ensure_ascii=False), 'KR'))
    return lesson


@pytest.mark.parametrize('value', [None, '{}', 'invalid', {'status': 'current_pipeline'}, context(capabilities=['unknown_feed']), context(stage='system_design'), context(capabilities=['position_review'])])
def test_absent_malformed_unknown_and_wrong_stage_do_not_enter_buy(value):
    assert not is_current_buy_memory(value, 'KR')


def test_market_boundary_and_future_capability_retention():
    assert is_current_buy_memory(context(), 'KR')
    assert not is_current_buy_memory(context(), 'US')
    future = context(status='improvement', stage='system_design', capabilities=['new_tick_feed'])
    assert normalize_application_context(future, 'KR') == future
    assert not is_current_buy_memory(future, 'KR')


def test_new_memory_cannot_self_approve_or_merge_before_review():
    mgr = manager()
    for text in ('추세 점검', '동일 추세 확인'):
        mgr._save_intuition({'condition': text, 'insight': '기존 판단 참고', 'application_context': context()}, [1, 2])
    assert all(not is_current_buy_memory(row[0], 'KR') for row in mgr.conn.execute('SELECT application_context FROM trading_intuitions'))
    assert mgr._consolidate_intuitions([{'canonical_id': 1, 'duplicate_ids': [2]}]) == 0


@pytest.mark.asyncio
async def test_offline_review_all_memory_types_without_rewriting_and_skip_unchanged(monkeypatch):
    mgr = manager()
    lesson = seed(mgr)
    future = context(status='improvement', stage='system_design', capabilities=['new_tick_feed'])
    reviews = [{'ref': 'intuition:1', 'application_context': context()},
               {'ref': 'principle:1', 'application_context': future},
               {'ref': 'lesson:1:0', 'application_context': context()}]
    llm = SimpleNamespace(generate_str=AsyncMock(side_effect=[json.dumps({'reviews': reviews}), json.dumps({'approved_refs': ['intuition:1', 'lesson:1:0']})]))
    stub_llm_dependencies(monkeypatch, llm)
    result = await mgr.review_memory_applicability()
    assert result['reviewed'] == 3 and result['current_pipeline'] == 2 and result['improvement'] == 1
    stored = json.loads(mgr.conn.execute('SELECT lessons FROM trading_journal').fetchone()[0])[0]
    assert {key: stored[key] for key in lesson} == lesson
    assert is_current_buy_memory(stored['application_context'], 'KR')
    assert mgr.conn.execute('SELECT condition, action FROM trading_principles').fetchone() == ('새 초단위 데이터 필요', '새 공급자 연동')
    repeated = await mgr.review_memory_applicability()
    assert repeated['reviewed'] == 0 and llm.generate_str.await_count == 2
    mgr.conn.execute("UPDATE trading_intuitions SET insight='매수 금지라는 새 규칙' WHERE id=1")
    assert [row['ref'] for row in mgr._application_records('KR', True, 100)] == ['intuition:1']


@pytest.mark.asyncio
async def test_independent_rejection_cannot_publish_current(monkeypatch):
    mgr = manager()
    seed(mgr)
    llm = SimpleNamespace(generate_str=AsyncMock(side_effect=[
        json.dumps({'reviews': [{'ref': 'intuition:1', 'application_context': context()}]}),
        json.dumps({'approved_refs': []}),
    ]))
    stub_llm_dependencies(monkeypatch, llm)
    result = await mgr.review_memory_applicability()
    assert result['current_pipeline'] == 0 and result['unreviewed'] == 1
    assert not is_current_buy_memory(mgr.conn.execute('SELECT application_context FROM trading_intuitions').fetchone()[0], 'KR')


def test_schema_migration_preserves_missing_as_unreviewed():
    mgr = manager()
    ensure_application_columns(mgr.conn)
    ensure_application_columns(mgr.conn)
    assert 'application_context' in {row[1] for row in mgr.conn.execute('PRAGMA table_info(trading_principles)')}


def test_rewritten_canonical_needs_new_applicability_approval_and_sector_is_not_generic():
    mgr = manager()
    for condition in ('변동성 구간 추격', '변동성이 높은 구간 추격', '기술주 변동성 구간 추격'):
        mgr._save_intuition({'condition': condition, 'insight': '현재 근거를 확인'}, [1, 2])
    mgr.conn.execute('UPDATE trading_intuitions SET application_context = ?', (json.dumps(context()),))
    assert mgr._consolidate_intuitions([{'canonical_id': 1, 'duplicate_ids': [3]}]) == 0
    assert mgr._consolidate_intuitions([{'canonical_id': 1, 'duplicate_ids': [2], 'canonical_condition': '변동성 높은 구간 추격 진입'}]) == 2
    rewritten = mgr.conn.execute('SELECT application_context FROM trading_intuitions ORDER BY id DESC LIMIT 1').fetchone()[0]
    assert not is_current_buy_memory(rewritten, 'KR')


def test_merge_cannot_cross_reviewed_application_status():
    mgr = manager()
    for condition in ('조건 하나', '조건 둘'):
        mgr._save_intuition({'condition': condition, 'insight': '판단 참고'}, [1, 2])
    mgr.conn.execute('UPDATE trading_intuitions SET application_context = ? WHERE id=1', (json.dumps(context()),))
    mgr.conn.execute('UPDATE trading_intuitions SET application_context = ? WHERE id=2', (json.dumps(context(status='improvement')),))
    assert mgr._consolidate_intuitions([{'canonical_id': 1, 'duplicate_ids': [2]}]) == 0

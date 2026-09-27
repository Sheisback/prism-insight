"""Offline regressions for cumulative intuition evidence and semantic reconciliation."""
import json
import importlib.util
from pathlib import Path
import sqlite3
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tracking.compression import CompressionManager
from tracking.db_schema import TABLE_TRADING_INTUITIONS


def test_compression_model_cannot_bypass_database_validation(monkeypatch):
    module = ModuleType('mcp_agent.agents.agent')
    module.Agent = lambda **kwargs: SimpleNamespace(**kwargs)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    path = Path(__file__).parents[1] / 'cores/agents/memory_compressor_agent.py'
    spec = importlib.util.spec_from_file_location('compression_factory_under_test', path)
    factory = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(factory)
    for language in ('ko', 'en'):
        assert factory.create_memory_compressor_agent(language).server_names == []


def manager():
    conn = sqlite3.connect(':memory:')
    conn.executescript(TABLE_TRADING_INTUITIONS)
    conn.execute("ALTER TABLE trading_intuitions ADD COLUMN market TEXT DEFAULT 'KR'")
    return CompressionManager(conn.cursor(), conn, enable_journal=True)


def rule(**overrides):
    return dict(category='pattern', subcategory='', condition='변동성 구간 추격 진입',
                insight='추세 정렬 전 추격 진입 금지', confidence=.7,
                supporting_trades=2, success_rate=.5, **overrides)


def test_repeated_corpus_does_not_inflate_evidence_or_confidence():
    mgr = manager()
    assert mgr._save_intuition(rule(), [1, 2])
    changed = rule()
    changed['confidence'] = .99
    assert not mgr._save_intuition(changed, [1, 2])
    row = mgr.conn.execute('SELECT supporting_trades, confidence, source_journal_ids FROM trading_intuitions').fetchone()
    assert row == (2, .7, '[1, 2]')
    mgr._save_intuition(rule(), [2, 3])
    assert mgr.conn.execute('SELECT supporting_trades, source_journal_ids FROM trading_intuitions').fetchone() == (3, '[1, 2, 3]')


def test_semantic_merge_retains_canonical_text_and_archives_duplicates():
    mgr = manager()
    mgr._save_intuition(rule(), [1, 2])
    variant = rule()
    variant.update(condition='변동성 높은 구간 조급한 단기 진입', insight='변동성 축소와 추세 정렬 전 FOMO 진입 금지')
    mgr._save_intuition(variant, [2, 3])
    assert mgr._consolidate_intuitions([{'canonical_id': 2, 'duplicate_ids': [1]}]) == 1
    rows = mgr.conn.execute('SELECT id, condition, is_active, supporting_trades, source_journal_ids FROM trading_intuitions ORDER BY id').fetchall()
    assert rows[0][2] == 0
    assert rows[1][1:] == (variant['condition'], 1, 3, '[1, 2, 3]')
    assert mgr._consolidate_intuitions([{'canonical_id': 2, 'duplicate_ids': [1]}]) == 0


def test_market_scope_and_inactive_boundaries():
    mgr = manager()
    mgr._save_intuition(rule(), [1, 2])
    mgr.conn.execute("UPDATE trading_intuitions SET market='US'")
    assert mgr._save_intuition(rule(), [3, 4])
    assert mgr._consolidate_intuitions([{'canonical_id': 2, 'duplicate_ids': [1]}]) == 0
    other = rule()
    other.update(category='sector', condition='다른 조건')
    mgr._save_intuition(other, [5, 6])
    mgr.conn.execute("UPDATE trading_intuitions SET scope='sector' WHERE id=3")
    assert mgr._consolidate_intuitions([{'canonical_id': 2, 'duplicate_ids': [3]}]) == 0
    prompt = mgr._build_layer3_prompt('records', 2)
    assert 'duplicate_groups' in prompt and 'source_journal_ids' in prompt
    assert '"id": 1,' not in prompt


def test_explicit_evidence_must_be_subset_of_corpus():
    mgr = manager()
    assert not mgr._save_intuition(rule(source_journal_ids=[99, 100]), [1, 2])
    assert mgr.conn.execute('SELECT COUNT(*) FROM trading_intuitions').fetchone()[0] == 0


def test_empty_market_is_not_implicitly_korean():
    mgr = manager()
    mgr._save_intuition(rule(), [1, 2])
    mgr.conn.execute("UPDATE trading_intuitions SET market=''")
    assert mgr._active_intuitions() == []


def test_union_preserves_originals_and_legacy_provenance_is_not_verified_support():
    mgr = manager()
    mgr._save_intuition(rule(), [1, 2])
    variant = rule()
    variant.update(category='market', subcategory='변동성 구간', condition='조급한 FOMO 진입', insight='추세 정렬 전 관망, 첫 진입은 비중 축소')
    mgr._save_intuition(variant, [2, 3])
    mgr.conn.execute("UPDATE trading_intuitions SET verified_source_journal_ids=NULL, source_journal_ids=?, supporting_trades=2", (json.dumps(list(range(40))),))
    group = {'canonical_id': 2, 'duplicate_ids': [1], 'canonical_condition': '변동성 구간 조급한 FOMO 추격 진입',
             'canonical_insight': '추세 정렬 전 추격 진입 금지 및 관망, 첫 진입은 비중 축소'}
    assert mgr._consolidate_intuitions([group]) == 2
    rows = mgr.conn.execute('SELECT condition, insight, is_active, supporting_trades FROM trading_intuitions ORDER BY id').fetchall()
    assert rows[0][:3] == (rule()['condition'], rule()['insight'], 0)
    assert rows[1][:3] == (variant['condition'], variant['insight'], 0)
    assert rows[2] == (group['canonical_condition'], group['canonical_insight'], 1, 2)


def test_numeric_timeframe_conflict_fails_closed():
    mgr = manager()
    first, second = rule(), rule()
    first['condition'] = '3일 하락'
    second['condition'] = '30일 하락'
    mgr._save_intuition(first, [1, 2])
    mgr._save_intuition(second, [3, 4])
    assert mgr._consolidate_intuitions([{'canonical_id': 1, 'duplicate_ids': [2]}]) == 0


def test_maintenance_prompt_has_no_journal_extraction_gate():
    mgr = manager()
    mgr._save_intuition(rule(), [1, 2])
    prompt = mgr._build_reconciliation_prompt()
    assert 'No new journal records are needed' in prompt
    assert 'duplicate_groups' in prompt and 'canonical_insight' in prompt
    assert '"id": 1' in prompt
    assert 'new_intuitions' not in prompt and 'patterns appearing 2+' not in prompt


def stub_llm_dependencies(monkeypatch, llm):
    params = ModuleType('mcp_agent.workflows.llm.augmented_llm')
    params.RequestParams = lambda **kw: kw
    monkeypatch.setitem(sys.modules, params.__name__, params)
    openai = ModuleType('mcp_agent.workflows.llm.augmented_llm_openai')
    openai.OpenAIAugmentedLLM = object
    monkeypatch.setitem(sys.modules, openai.__name__, openai)
    agent = AsyncMock()
    agent.__aenter__.return_value = agent
    agent.attach_llm.return_value = llm
    factory = ModuleType('cores.agents.memory_compressor_agent')
    factory.create_memory_compressor_agent = lambda language: agent
    monkeypatch.setitem(sys.modules, factory.__name__, factory)
    agent_module = ModuleType('mcp_agent.agents.agent')
    agent_module.Agent = lambda **kwargs: agent
    monkeypatch.setitem(sys.modules, agent_module.__name__, agent_module)


@pytest.mark.asyncio
async def test_opposite_action_rejected_by_separate_review(monkeypatch):
    mgr = manager()
    mgr._save_intuition(rule(), [1, 2])
    opposite = rule()
    opposite['insight'] = '추세 정렬 전 추격 진입 허용'
    mgr._save_intuition(opposite, [3, 4])
    llm = SimpleNamespace(generate_str=AsyncMock(return_value='{"approved_groups": []}'))
    stub_llm_dependencies(monkeypatch, llm)
    proposed = [{'canonical_id': 1, 'duplicate_ids': [2]}]
    approved = await mgr._verify_duplicate_groups(llm, proposed)
    assert mgr._consolidate_intuitions(approved) == 0
    assert 'opposite actions' in llm.generate_str.call_args.kwargs['message']
    assert mgr.conn.execute('SELECT COUNT(*) FROM trading_intuitions WHERE is_active=1').fetchone()[0] == 2


@pytest.mark.asyncio
async def test_refresh_consolidates_without_new_journal_and_rejects_missing_evidence(monkeypatch):
    mgr = manager()
    mgr.conn.execute('CREATE TABLE trading_journal (id INTEGER, ticker TEXT, company_name TEXT, trade_date TEXT, profit_rate REAL, compressed_summary TEXT, one_line_summary TEXT, pattern_tags TEXT, buy_scenario TEXT, market TEXT)')
    mgr._save_intuition(rule(), [1, 2])
    variant = rule()
    variant['condition'] = '변동성 높은 구간 추격 진입'
    mgr._save_intuition(variant, [2, 3])
    groups = [{'canonical_id': 1, 'duplicate_ids': [2]}]
    llm = SimpleNamespace(generate_str=AsyncMock(side_effect=[
        json.dumps({'new_intuitions': [dict(rule(), condition='근거 없는 새 규칙')], 'duplicate_groups': groups}),
        json.dumps({'approved_groups': groups}),
    ]))
    stub_llm_dependencies(monkeypatch, llm)
    result = await mgr.refresh_intuitions()
    assert result['errors'] == []
    assert result['corpus'] == 0 and result['intuitions_generated'] == 0
    assert result['intuitions_consolidated'] == 1
    assert mgr.conn.execute('SELECT COUNT(*) FROM trading_intuitions WHERE is_active=1').fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('already_exists', [False, True])
async def test_same_batch_paraphrases_reconciled_after_ids_assigned(monkeypatch, already_exists):
    mgr = manager()
    first, second = rule(source_journal_ids=[1, 2]), rule(source_journal_ids=[2, 3])
    second['condition'] = '변동성 높은 구간 추격 진입'
    groups = [{'canonical_id': 2, 'duplicate_ids': [1]}]
    llm = SimpleNamespace(generate_str=AsyncMock(side_effect=[
        json.dumps({'duplicate_groups': groups}), json.dumps({'approved_groups': groups}),
    ]))
    stub_llm_dependencies(monkeypatch, llm)
    if already_exists:
        mgr._save_intuition(first, [1, 2])
    result = await mgr._apply_intuition_response(llm, {'new_intuitions': [second] if already_exists else [first, second]}, [1, 2, 3])
    assert result['intuitions_consolidated'] == 1
    assert mgr.conn.execute('SELECT supporting_trades FROM trading_intuitions WHERE is_active=1').fetchall() == [(3,)]


@pytest.mark.asyncio
async def test_evidence_update_precedes_deactivation(monkeypatch):
    mgr = manager()
    mgr._save_intuition(rule(), [1, 2])
    variant = rule()
    variant['condition'] = '변동성 높은 구간 추격 진입'
    mgr._save_intuition(variant, [2, 3])
    update = rule(source_journal_ids=[3, 4], existing_intuition_id=1)
    groups = [{'canonical_id': 2, 'duplicate_ids': [1]}]
    llm = SimpleNamespace(generate_str=AsyncMock(return_value=json.dumps({'approved_groups': groups})))
    stub_llm_dependencies(monkeypatch, llm)
    await mgr._apply_intuition_response(llm, {'new_intuitions': [update], 'duplicate_groups': groups}, [3, 4])
    assert mgr.conn.execute('SELECT supporting_trades FROM trading_intuitions WHERE is_active=1').fetchall() == [(4,)]


@pytest.mark.asyncio
async def test_user_three_intuitions_full_refresh_preserves_all_caveats(monkeypatch):
    mgr = manager()
    mgr.conn.execute('CREATE TABLE trading_journal (id INTEGER, ticker TEXT, company_name TEXT, trade_date TEXT, profit_rate REAL, compressed_summary TEXT, one_line_summary TEXT, pattern_tags TEXT, buy_scenario TEXT, market TEXT)')
    originals = [
        ('pattern', 'fomo_entry', '변동성 구간 조급한 추격 진입·초단기 보유', '단기 피탈 위험 = 추세 정렬·변동성 축소·지지 확인 전 추격 진입 금지'),
        ('pattern', 'volatility_whipsaw', '변동성 구간에서 조급한 추격 진입·초단기 보유', '단기 피탈 위험 = 추세 정렬과 눌림 확인 전 FOMO 진입 금지, 첫 진입은 비중 축소 또는 관망 우선'),
        ('market', '변동성 구간', '변동성 높은 구간의 조급한 단기 진입', '단기 피탈 가능성 매우 높음 = 변동성 축소와 추세 정렬 전에는 진입을 늦추고, 당일성 FOMO 진입은 피한다'),
    ]
    for index, (category, subcategory, condition, insight) in enumerate(originals):
        item = rule()
        item.update(category=category, subcategory=subcategory, condition=condition, insight=insight)
        mgr._save_intuition(item, [index + 1, index + 2])
    group = {'canonical_id': 1, 'duplicate_ids': [2, 3],
             'canonical_condition': '변동성 구간 조급한 추격·단기 진입 및 초단기 보유',
             'canonical_insight': '단기 피탈 위험: 추세 정렬·변동성 축소·지지와 눌림 확인 전 추격·FOMO 진입을 피하고 진입을 늦춘다. 첫 진입은 비중 축소 또는 관망을 우선하며 당일성 FOMO 진입은 피한다.'}
    llm = SimpleNamespace(generate_str=AsyncMock(side_effect=[
        json.dumps({'new_intuitions': [], 'duplicate_groups': [group]}),
        json.dumps({'approved_groups': [group]}),
    ]))
    stub_llm_dependencies(monkeypatch, llm)
    first = await mgr.refresh_intuitions()
    assert first['errors'] == []
    rows = mgr.conn.execute('SELECT category, subcategory, condition, insight, is_active FROM trading_intuitions ORDER BY id').fetchall()
    assert [row[:4] for row in rows[:3]] == originals
    assert [row[4] for row in rows] == [0, 0, 0, 1]
    assert rows[3][3] == group['canonical_insight']
    second = await mgr.refresh_intuitions()
    assert second['reason'] == 'insufficient_corpus'
    assert mgr.conn.execute('SELECT supporting_trades FROM trading_intuitions WHERE is_active=1').fetchall() == [(4,)]


@pytest.mark.asyncio
async def test_review_outage_retains_originals_and_reports_partial_counts(monkeypatch):
    mgr = manager()
    mgr._save_intuition(rule(), [1, 2])
    second = rule(source_journal_ids=[2, 3])
    second['condition'] = '변동성 높은 구간 추격 진입'
    llm = SimpleNamespace(generate_str=AsyncMock(side_effect=RuntimeError('review unavailable')))
    stub_llm_dependencies(monkeypatch, llm)
    result = await mgr._apply_intuition_response(llm, {'new_intuitions': [second]}, [1, 2, 3])
    assert result['intuitions_generated'] == 1 and result['intuitions_consolidated'] == 0
    assert result['errors'] == ['intuition_reconciliation_deferred: review unavailable']
    assert mgr.conn.execute('SELECT COUNT(*) FROM trading_intuitions WHERE is_active=1').fetchone()[0] == 2


@pytest.mark.asyncio
async def test_refresh_filters_market_before_limit_and_preserves_summary_fallback(monkeypatch):
    mgr = manager()
    mgr.conn.execute('CREATE TABLE trading_journal (id INTEGER, ticker TEXT, company_name TEXT, trade_date TEXT, profit_rate REAL, compressed_summary TEXT, one_line_summary TEXT, pattern_tags TEXT, buy_scenario TEXT, market TEXT)')
    mgr.conn.executemany('INSERT INTO trading_journal VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', [
        (1, 'KR_TEST', '한국', '2099-01-01', 1., None, 'fallback KR evidence', '[]', '{}', 'KR'),
        (2, 'US_TEST', '미국', '2099-01-02', 2., None, 'US evidence', '[]', '{}', 'US'),
    ])
    llm = SimpleNamespace(generate_str=AsyncMock(return_value='{"new_intuitions": [], "duplicate_groups": []}'))
    stub_llm_dependencies(monkeypatch, llm)
    result = await mgr.refresh_intuitions(limit=1, min_entries=1)
    assert result['errors'] == [] and result['corpus'] == 1
    prompt = llm.generate_str.call_args.kwargs['message']
    assert 'fallback KR evidence' in prompt and 'US evidence' not in prompt


@pytest.mark.parametrize('rewrite', [False, True])
def test_merge_preserves_earliest_creation_and_latest_actual_validation(rewrite):
    mgr = manager()
    mgr._save_intuition(rule(), [1, 2])
    other = rule()
    other['condition'] = '변동성 높은 구간 추격 진입'
    mgr._save_intuition(other, [2, 3])
    mgr.conn.execute("UPDATE trading_intuitions SET created_at='2026-01-01', last_validated_at='2026-02-01' WHERE id=1")
    mgr.conn.execute("UPDATE trading_intuitions SET created_at='2026-02-01', last_validated_at='2026-09-27' WHERE id=2")
    group = {'canonical_id': 1, 'duplicate_ids': [2]}
    if rewrite:
        group['canonical_condition'] = '변동성이 높은 구간의 추격 진입'
    mgr._consolidate_intuitions([group])
    assert mgr.conn.execute('SELECT created_at, last_validated_at FROM trading_intuitions WHERE is_active=1').fetchall() == [('2026-01-01', '2026-09-27')]


@pytest.mark.asyncio
async def test_model_approved_merge_cannot_drop_observed_support_qualifier(monkeypatch):
    mgr = manager()
    original, paraphrase = rule(), rule()
    original['insight'] = '추세 정렬·변동성 축소·지지 확인 전 추격 진입 금지'
    paraphrase['insight'] = '추세 정렬과 눌림 확인 전 FOMO 진입 금지, 첫 진입은 비중 축소 또는 관망 우선, 당일성 FOMO 금지'
    mgr._save_intuition(original, [1, 2])
    mgr._save_intuition(paraphrase, [2, 3])
    group = {'canonical_id': 1, 'duplicate_ids': [2],
             'canonical_insight': '추세 정렬·변동성 축소·눌림 확인 전 추격 금지. 첫 진입은 비중 축소 또는 관망 우선, 당일성 FOMO 금지'}
    llm = SimpleNamespace(generate_str=AsyncMock(return_value=json.dumps({'approved_groups': [group]})))
    stub_llm_dependencies(monkeypatch, llm)
    approved = await mgr._verify_duplicate_groups(llm, [group])
    assert approved == [group]  # Reproduce the real model's incorrect approval.
    assert mgr._consolidate_intuitions(approved) == 0
    assert mgr.conn.execute('SELECT COUNT(*) FROM trading_intuitions WHERE is_active=1').fetchone()[0] == 2
    prompt = mgr._build_reconciliation_prompt()
    assert 'required_qualifiers' in prompt and '지지 확인 / support confirmation' in prompt
    group['canonical_insight'] = group['canonical_insight'].replace('눌림 확인', '지지 확인과 눌림 확인')
    assert mgr._consolidate_intuitions([group]) == 2


@pytest.mark.parametrize('missing', ['첫 진입', '비중 축소 또는 관망', '당일성 FOMO'])
def test_material_source_qualifiers_cannot_be_silently_removed(missing):
    mgr = manager()
    first, second = rule(), rule()
    first['insight'] = '눌림 확인 전 금지. 첫 진입: 비중 축소 또는 관망. 당일성 FOMO 금지'
    second['condition'] = '변동성 높은 구간 추격 진입'
    second['insight'] = first['insight']
    mgr._save_intuition(first, [1, 2])
    mgr._save_intuition(second, [2, 3])
    group = {'canonical_id': 1, 'duplicate_ids': [2], 'canonical_insight': first['insight'].replace(missing, '')}
    assert mgr._consolidate_intuitions([group]) == 0

"""Versioned applicability metadata for trading memory, independent of live decisions."""

import json


CONTRACT_VERSION = 1
_CAPABILITIES = {
    'batch_report': 'Current morning/afternoon candidate analysis report and its supplied technical/fundamental evidence.',
    'trigger_context': 'The supplied trigger_type and morning/afternoon mode; trigger labels alone do not prove FOMO or authorize blanket vetoes.',
    'market_regime': 'The supplied deterministic regime enum is authoritative; report prose cannot override its gate, and missing context stays unknown.',
    'portfolio_constraints': 'Existing portfolio, cash, exposure and risk constraints enforced by the current pipeline.',
    'entry_advisory': 'Advisory Enter/NoEntry evaluation using current batch inputs; NoEntry does not schedule a future order.',
    'existing_pilot_pyramiding': 'Existing controlled pilot/pyramiding paths and their current gates; no arbitrary resizing or automatic later entry.',
    'position_review': 'Existing held-position review and protective management, separate from batch entry advice.',
}


def memory_contract(market):
    """Return the current capabilities against which offline memory reviews are checked."""
    if market not in ('KR', 'US'):
        raise ValueError('market must be KR or US')
    return {
        'version': CONTRACT_VERSION, 'market': market, 'capabilities': dict(_CAPABILITIES),
        'constraints': [
            'Current memory is a conditional advisory reference, never authorization to alter trading policy.',
            'Only evidence actually supplied in the current batch can support an entry check; never invent unavailable measurements.',
            'New numeric thresholds, global hard gates, automatic delayed entry, new data feeds or arbitrary resizing are improvement tasks.',
            'Existing controlled pilot and pyramiding capabilities are not blanket unsupported; their current gates must remain unchanged.',
            'Position management advice belongs to position_management, and engineering/data changes belong to system_design.',
            'Entry evaluation occurs in morning/afternoon batches; protective position loops are separate and do not make arbitrary intraday signals available to BUY.',
            'Screening RR may be missing; do not pretend candidate screening measured the report-stage entry/stop/target RR.',
            'General FOMO/chase lessons must not become a blanket prohibition of all breakout, gap or momentum candidates.',
            'Research watchlists are research context only and cannot authorize orders or automatic later entries.',
            ('KR reports may supply Korean institutional/foreign flow evidence; use only supplied observations.'
             if market == 'KR' else 'US institutional holdings disclosures are delayed snapshots, not real-time Korean-style foreign/institutional flow.'),
        ],
    }


def normalize_application_context(value, market):
    """Malformed, absent, incompatible and unknown metadata cannot become live advice."""
    fallback = {'version': CONTRACT_VERSION, 'status': 'unreviewed', 'market': market,
                'stage': 'system_design', 'required_capabilities': [], 'reason': 'Applicability has not been verified.'}
    if market not in ('KR', 'US'):
        return fallback
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return fallback
    if not isinstance(value, dict):
        return fallback
    capabilities = value.get('required_capabilities')
    if (type(value.get('version')) is not int or value['version'] != CONTRACT_VERSION
            or value.get('market') != market
            or value.get('status') not in ('current_pipeline', 'improvement', 'unreviewed')
            or value.get('stage') not in ('batch_buy', 'position_management', 'system_design')
            or not isinstance(capabilities, list)
            or any(not isinstance(item, str) or not item.strip() for item in capabilities)
            or not isinstance(value.get('reason'), str) or not value['reason'].strip()):
        return fallback
    if value['status'] == 'current_pipeline':
        if (not capabilities or any(item not in _CAPABILITIES for item in capabilities)
                or value['stage'] == 'system_design'
                or (value['stage'] == 'batch_buy' and 'position_review' in capabilities)
                or (value['stage'] == 'position_management' and 'position_review' not in capabilities)):
            return fallback
    return {key: value[key] for key in ('version', 'status', 'market', 'stage', 'required_capabilities', 'reason')}


def is_current_buy_memory(value, market):
    context = normalize_application_context(value, market)
    return context['status'] == 'current_pipeline' and context['stage'] == 'batch_buy'


def ensure_application_columns(conn):
    """Add nullable metadata to existing tables without activating legacy memory."""
    intuition_columns = {row[1] for row in conn.execute('PRAGMA table_info(trading_intuitions)')}
    if intuition_columns and 'application_context' not in intuition_columns:
        conn.execute('ALTER TABLE trading_intuitions ADD COLUMN application_context TEXT')
    principle_columns = {row[1] for row in conn.execute('PRAGMA table_info(trading_principles)')}
    if principle_columns and 'application_context' not in principle_columns:
        conn.execute('ALTER TABLE trading_principles ADD COLUMN application_context TEXT')

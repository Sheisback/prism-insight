"""Atomic scenario lifecycle on the existing isolated strategy ledger.

Only SHADOW books are accepted. Simulated normalized units are never broker fills.
No network, scheduler, LLM or account adapter exists in this module.
"""
from decimal import Decimal

from prism_core.strategy_ledger import StrategyLedger, LedgerError, _text, _time
from prism_core.scenario_shadow_policy import create_plan, create_state, evaluate, revise_plan

OWNER = 'scenario-shadow-v1'


class ScenarioShadowLedger:
    def __init__(self, path):
        self.ledger = StrategyLedger(path)

    def provision(self, book_id, max_slots=10):
        return self.ledger.create_book(book_id, 'US', max_slots, cohort=OWNER, mode='SHADOW')

    def _book(self, db, book_id):
        book = self.ledger._get(db, 'books', book_id)
        if book['mode'] != 'SHADOW' or book['cohort'] != OWNER or book['market'] != 'US':
            raise LedgerError('dedicated US scenario SHADOW book required')
        return book

    @staticmethod
    def _revision(value):
        if type(value) is not int or value < 0:
            raise LedgerError('nonnegative integer expected revision required')

    @staticmethod
    def _sync_permission(campaign, at):
        envelope = campaign['scenario_shadow']
        if envelope['state']['add_cancelled']:
            campaign['add_permission'] = 'ADD_CANCELLED'
        elif at >= _time(envelope['plan']['expires_at']):
            campaign['add_permission'] = 'ADD_EXPIRED'

    def _target(self, db, event_id, book_id, campaign_id, symbol, target, price, at, reason):
        payload = dict(kind='target', book_id=book_id, campaign_id=campaign_id, symbol=symbol,
                       target_pct=str(target), price=str(price), occurred_at=at,
                       policy_version=OWNER, reason=reason, regime='scenario_shadow',
                       source_hash=None, fee_rate='0.001', slippage_rate='0')
        return self.ledger._apply_target_in_transaction(db, event_id, payload, owner=OWNER, notify=False)

    def open(self, event_id, book_id, campaign_id, symbol, *, plan_inputs):
        plan = create_plan(**plan_inputs)
        state = create_state(plan)
        at = _time(plan_inputs['entry_at'])
        symbol, campaign_id = _text(symbol), _text(campaign_id)
        payload = dict(kind='scenario_open', book_id=book_id, campaign_id=campaign_id,
                       symbol=symbol, plan=plan, occurred_at=at)
        with self.ledger._transaction() as db:
            self._book(db, book_id)
            if not self.ledger._event(db, event_id, payload):
                return dict(self.ledger._snapshot(db, book_id), event_applied=False)
            if db.execute('SELECT 1 FROM campaigns WHERE id=?', (campaign_id,)).fetchone():
                raise LedgerError('existing campaign cannot be adopted')
            self._target(db, event_id+':target', book_id, campaign_id, symbol, 10,
                         plan_inputs['entry_price'], at, 'INITIAL_SCENARIO_10')
            campaign = self.ledger._get(db, 'campaigns', campaign_id)
            campaign['scenario_shadow'] = dict(revision=0, plan=plan, state=state)
            self.ledger._save(db, 'campaigns', campaign_id, campaign)
            return dict(self.ledger._snapshot(db, book_id), event_applied=True)

    def advance(self, event_id, campaign_id, *, expected_revision, evidence, occurred_at):
        self._revision(expected_revision)
        at = _time(occurred_at)
        payload = dict(kind='scenario_tick', campaign_id=campaign_id,
                       expected_revision=expected_revision, evidence=evidence, occurred_at=at)
        with self.ledger._transaction() as db:
            campaign = self.ledger._get(db, 'campaigns', campaign_id)
            book_id = campaign['book_id']
            self._book(db, book_id)
            envelope = campaign.get('scenario_shadow')
            if not envelope:
                raise LedgerError('unowned campaign')
            if not self.ledger._event(db, event_id, payload):
                return dict(self.ledger._snapshot(db, book_id), event_applied=False)
            if envelope['revision'] != expected_revision:
                raise LedgerError('scenario revision conflict')
            self.ledger._chronology(campaign, at)
            self.ledger._book_chronology(db, self._book(db, book_id), at)
            decision = evaluate(envelope['plan'], envelope['state'], evidence, now=at,
                                cumulative_allocation=campaign['cumulative_deployed_allocation'],
                                remaining_allocation=campaign['remaining_allocation'],
                                normalized_units=campaign['normalized_units'])
            self.ledger._event(db, event_id+':decision', dict(kind='scenario_decision',
                               campaign_id=campaign_id, occurred_at=at, **decision))
            if decision['action'] == 'ADD':
                self._target(db, event_id+':target', book_id, campaign_id, campaign['symbol'],
                             Decimal(decision['target_allocation'])*100, decision['price'], at, decision['reason'])
            elif decision['action'] == 'EXIT':
                self.ledger._sell_in_transaction(db, event_id+':exit', dict(
                    kind='sell', campaign_id=campaign_id, price=str(decision['price']),
                    occurred_at=at, normalized_units=None, fee_rate='0.001',
                    slippage_rate='0', source_hash=decision['evidence_hash']), owner=OWNER)
            campaign = self.ledger._get(db, 'campaigns', campaign_id)
            campaign['scenario_shadow'] = dict(envelope, state=decision['state'], revision=expected_revision+1)
            self._sync_permission(campaign, at)
            campaign['last_event_at'] = at
            if decision['price'] is not None:
                campaign.update(mark_price=str(decision['price']), mark_at=at,
                                mark_basis='caller_attested_shadow_quote')
            self.ledger._save(db, 'campaigns', campaign_id, campaign)
            return dict(self.ledger._snapshot(db, book_id), event_applied=True, decision=decision)

    def revise(self, event_id, campaign_id, *, expected_revision, occurred_at, changes):
        self._revision(expected_revision)
        at = _time(occurred_at)
        payload = dict(kind='scenario_revision', campaign_id=campaign_id,
                       expected_revision=expected_revision, occurred_at=at, changes=changes)
        with self.ledger._transaction() as db:
            campaign = self.ledger._get(db, 'campaigns', campaign_id)
            self._book(db, campaign['book_id'])
            envelope = campaign.get('scenario_shadow')
            if not envelope:
                raise LedgerError('unowned campaign')
            if not self.ledger._event(db, event_id, payload):
                return dict(self.ledger._snapshot(db, campaign['book_id']), event_applied=False)
            if envelope['revision'] != expected_revision:
                raise LedgerError('scenario revision conflict')
            if Decimal(campaign['normalized_units']) <= 0:
                raise LedgerError('closed campaign cannot be revised')
            self.ledger._chronology(campaign, at)
            revised = revise_plan(envelope['plan'], envelope['state'], now=at, **changes)
            campaign['scenario_shadow'] = dict(revised, revision=expected_revision+1)
            self._sync_permission(campaign, at)
            campaign['last_event_at'] = at
            self.ledger._save(db, 'campaigns', campaign_id, campaign)
            return dict(self.ledger._snapshot(db, campaign['book_id']), event_applied=True)

import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import challenge_rules as rules
from live_trading import LiveAgent, OfficialSpotBroker
from mcp_link import MCPBridge


class Reads:
    base = '0'

    def call(self, name, args=None):
        if name == 'get_credit_sub_accounts':
            return [dict(clientId='test-credit', isActive=True, isSuspend=False, type='prop')]
        if name == 'get_credit_package':
            return dict(client_id='test-credit', loan=5000000, assurance=0, package_title=rules.PLAN_NAME,
                        created_at=rules.START_ISO, expire_at=rules.END_ISO)
        if name == 'get_credit_balances':
            return {'TMN': {'available': '5000000', 'total': '5000000'}, 'BTC': {'available': self.base, 'total': self.base}}
        if name == 'get_spot_markets':
            return {'symbols': {'BTCTMN': dict(symbol='BTCTMN', is_spot=True, is_market_type_enable=True,
                amount_precision=8, price_precision=0, minQty='0.00001', minNotional='1000', price='100000')}}
        if name == 'get_market_candles':
            now = rules.START_AT + 3600
            return {'candles': [dict(t=int(now // 900) * 900 - 900, c=100000)]}
        raise AssertionError(name)


class FakeBroker:
    def __init__(self, agent):
        self.agent, self.sent = agent, []

    def submit_limit(self, payload):
        self.agent.authorize(payload)
        self.sent.append(payload)
        return dict(clientOrderId=payload['client_id'], symbol=payload['symbol'], side=payload['side'],
                    type='LIMIT', status='FILLED', executedQty=payload['quantity'], fills=[
                        dict(quantity=payload['quantity'], price=payload['price'], fee='1', feeAsset='TMN', timestamp=1)])

    def open_orders(self, symbol):
        return []


class LiveTradingTests(unittest.TestCase):
    def test_agent_buy_and_sell_record_real_separate_ledger_using_mocks(self):
        cfg = dict(enabled=True, symbol='BTCTMN', rule_sha256=rules.RULE_HASH,
                   dedicated_key_sha256=hashlib.sha256(b'TEST-ONLY').hexdigest(),
                   scope_evidence_verified=True, scope_evidence_reference='test-evidence',
                   credit_client_id='test-credit', verified_fee_rate='0.001', max_order_tmn=50000,
                   max_exposure_tmn=50000, fee_evidence_reference='test-only')
        with tempfile.TemporaryDirectory() as directory, patch('live_trading.settings', return_value={'WALLEX_API_KEY': 'TEST-ONLY'}), patch('time.time', return_value=rules.START_AT + 3600):
            reads = Reads()
            agent = LiveAgent(cfg, reads, Path(directory) / 'orders.db')
            broker = FakeBroker(agent)
            agent.broker = broker
            agent.journal.broker = broker
            with patch('live_trading.decide', return_value={'action': 'BUY'}):
                self.assertEqual(agent.cycle(False)['status'], 'preflight_passed')
                self.assertEqual(len(broker.sent), 0)
                self.assertEqual(agent.cycle(True)['exchange_status'], 'FILLED')
            reads.base = broker.sent[0]['quantity']
            with patch('live_trading.decide', return_value={'action': 'SELL'}):
                self.assertEqual(agent.cycle(True)['exchange_status'], 'FILLED')
            self.assertEqual([x['side'] for x in broker.sent], ['BUY', 'SELL'])
            self.assertEqual(len(agent.journal.orders()), 2)
            records = agent.journal.audit_records()
            self.assertTrue(any(r['payload'].get('kind') == 'order_intent_link' for r in records))
            self.assertNotIn('TEST-ONLY', str(records))
            from judge_live_evidence import evaluate
            evidence = evaluate(Path(directory) / 'orders.db')
            self.assertTrue(evidence['fully_realized_tmn'])
            self.assertEqual(evidence['realized_profit_tmn'], '-2.00000000')
            self.assertEqual(evidence['unlinked_orders'], 0)
            import sqlite3
            from contextlib import closing
            with closing(sqlite3.connect(Path(directory) / 'orders.db')) as db, db:
                db.execute("UPDATE decision_audit SET payload='{}' WHERE id=1")
            with self.assertRaises(ValueError):
                evaluate(Path(directory) / 'orders.db')
            cfg['dedicated_key_sha256'] = 'wrong'
            with self.assertRaises(ValueError):
                agent.scope()

    def test_pre_deadline_does_not_call_model(self):
        import competition
        cfg = dict(enabled=True, symbol='BTCTMN', rule_sha256=rules.RULE_HASH,
                   dedicated_key_sha256=hashlib.sha256(b'TEST-ONLY').hexdigest(),
                   scope_evidence_verified=True, scope_evidence_reference='test-evidence',
                   credit_client_id='test-credit', verified_fee_rate='0.001', max_order_tmn=50000,
                   max_exposure_tmn=50000, fee_evidence_reference='test-only')
        class LatestReads(Reads):
            def call(self, name, args=None):
                if name == 'get_market_candles':
                    return {'candles': [dict(t=int((competition.END_AT - 300) // 900) * 900 - 900, c=100000)]}
                return super().call(name, args)
        with tempfile.TemporaryDirectory() as directory, patch('live_trading.settings', return_value={'WALLEX_API_KEY': 'TEST-ONLY'}), patch('time.time', return_value=competition.END_AT - 300):
            agent = LiveAgent(cfg, LatestReads(), Path(directory) / 'orders.db')
            with patch('live_trading.decide', side_effect=AssertionError('LLM/model must not run')):
                self.assertEqual(agent.cycle(False)['status'], 'hold')
            self.assertEqual(agent.journal.audit_records()[-1]['payload']['decision']['reason'], 'pre_deadline_no_new_entries')

    def test_dashboard_guard_and_broker_authorization_remain_effective(self):
        bridge = MCPBridge()
        with self.assertRaises(ValueError):
            bridge.call('wallex_spot_post_account_orders', {})
        def reject(payload):
            raise ValueError('preflight rejected')
        broker = OfficialSpotBroker(bridge, reject)
        with patch.object(broker, '_run') as remote:
            with self.assertRaises(ValueError):
                broker.submit_limit({})
            remote.assert_not_called()


if __name__ == '__main__':
    unittest.main()

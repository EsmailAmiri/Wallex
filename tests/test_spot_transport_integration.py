"""Actual agent/journal/broker code against an in-memory MCP peer; zero network."""
import asyncio
import copy
import hashlib
import json
import socket
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import challenge_rules as rules
from judge_live_evidence import evaluate
from live_trading import LiveAgent
from mcp_link import OFFICIAL_URL
from tests.test_live_trading import Reads


class MCPPeer(Reads):
    def __init__(self, lose_buy_response=False):
        self.calls, self.orders = [], {}
        self.base, self.cash = '0', Decimal('5000000')
        self.lose_buy_response = lose_buy_response
        self.info = {'provider': 'wallex-official'}
        self.remote_tools = {'wallex_spot_post_account_orders', 'wallex_spot_get_orders_client',
                             'wallex_spot_get_open_order', 'wallex_spot_delete_orders_client',
                             'wallex_margin_open_position'}
        self.session = self
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._thread)
        self.thread.start()
        if not self.ready.wait(5):
            raise RuntimeError('Mock MCP loop did not start.')

    def _thread(self):
        asyncio.run(self._serve())

    async def _serve(self):
        self.loop = asyncio.get_running_loop()
        self.call_lock, self.stop = asyncio.Lock(), asyncio.Event()
        self.ready.set()
        await self.stop.wait()

    def close(self):
        self.loop.call_soon_threadsafe(self.stop.set)
        self.thread.join(5)

    def connect(self):
        return self.info

    def call(self, name, args=None):
        if name == 'get_credit_balances':
            return {'TMN': {'available': str(self.cash), 'total': str(self.cash)},
                    'BTC': {'available': self.base, 'total': self.base}}
        return super().call(name, args)

    async def call_tool(self, tool, args):
        self.calls.append((tool, copy.deepcopy(args)))
        if tool == 'wallex_spot_get_open_order':
            return SimpleNamespace(isError=False, structuredContent={'success': True, 'result': {'orders': []}})
        if tool == 'wallex_spot_post_account_orders':
            if args['client_id'] in self.orders:
                raise AssertionError('Duplicate order submission.')
            qty, price = Decimal(args['quantity']), Decimal(args['price'])
            buy = args['side'] == 'BUY'
            self.base = str(Decimal(self.base) + (qty if buy else -qty))
            self.cash += (-qty * price if buy else qty * price) - 1
            result = dict(clientOrderId=args['client_id'], symbol=args['symbol'], side=args['side'],
                          type=args['type'], status='FILLED', executedQty=args['quantity'], fills=[
                              dict(quantity=args['quantity'], price=args['price'], fee='1', feeAsset='TMN', timestamp=1)])
            self.orders[args['client_id']] = result
            if buy and self.lose_buy_response:
                self.lose_buy_response = False
                raise TimeoutError('MCP peer accepted the order but lost its response.')
            return SimpleNamespace(isError=False, structuredContent={'success': True, 'result': result})
        if tool == 'wallex_spot_get_orders_client':
            # Exercise the JSON text fallback as well as structuredContent.
            return SimpleNamespace(isError=False, structuredContent=None,
                content=[SimpleNamespace(type='text', text=json.dumps({'success': True, 'result': self.orders[args['client_id']]}))])
        raise AssertionError('Unexpected financial operation in test.')


class SpotTransportIntegrationTests(unittest.TestCase):
    def config(self):
        return dict(enabled=True, symbol='BTCTMN', rule_sha256=rules.RULE_HASH,
                    dedicated_key_sha256=hashlib.sha256(b'TEST-ONLY').hexdigest(),
                    scope_evidence_verified=True, scope_evidence_reference='mock-only', credit_client_id='test-credit',
                    verified_fee_rate='0.001', fee_evidence_reference='mock-only', max_order_tmn=50000, max_exposure_tmn=50000)

    def exercise_roundtrip(self, lost_response):
        peer = MCPPeer(lost_response)
        try:
            with tempfile.TemporaryDirectory() as folder, patch('live_trading.settings', return_value={
                    'WALLEX_API_KEY': 'TEST-ONLY', 'WALLEX_MCP_URL': OFFICIAL_URL}), patch('time.time', return_value=rules.START_AT + 3600), patch.object(socket, 'create_connection', side_effect=AssertionError('Network forbidden')):
                journal = Path(folder) / 'mock-orders.sqlite'
                agent = LiveAgent(self.config(), peer, journal)
                with patch('live_trading.decide', return_value={'action': 'BUY', 'reason': 'mock execution candidate'}):
                    result = agent.cycle(True)
                if lost_response:
                    self.assertEqual(result['status'], 'uncertain')
                    # Construct a fresh agent over the existing ledger to test restart.
                    agent = LiveAgent(self.config(), peer, journal)
                    self.assertEqual(agent.cycle(True)['status'], 'reconciled')
                else:
                    self.assertEqual(result['exchange_status'], 'FILLED')
                with patch('live_trading.decide', return_value={'action': 'SELL', 'reason': 'mock exit candidate'}):
                    self.assertEqual(agent.cycle(True)['exchange_status'], 'FILLED')
                posts = [args for name, args in peer.calls if name == 'wallex_spot_post_account_orders']
                self.assertEqual([p['side'] for p in posts], ['BUY', 'SELL'])
                self.assertNotEqual(posts[0]['client_id'], posts[1]['client_id'])
                self.assertTrue(all(set(p) == {'client_id', 'symbol', 'side', 'type', 'price', 'quantity'} for p in posts))
                self.assertEqual(Decimal(peer.base), 0)
                self.assertEqual(peer.cash, Decimal('4999998'))
                evidence = evaluate(journal)
                self.assertTrue(evidence['fully_realized_tmn'])
                self.assertEqual(Decimal(evidence['realized_profit_tmn']), Decimal('-2'))
                self.assertEqual(evidence['unlinked_orders'], 0)
                with self.assertRaises(ValueError):
                    agent.broker._run('wallex_margin_open_position', {})
                self.assertFalse(any('margin' in name for name, _ in peer.calls))
        finally:
            peer.close()

    def test_buy_sell_through_actual_official_adapter_with_mock_peer(self):
        self.exercise_roundtrip(False)

    def test_lost_response_restart_lookup_then_sell_without_duplicate(self):
        self.exercise_roundtrip(True)


if __name__ == '__main__':
    unittest.main()

"""Tests for the live-order adapter – mock-only, no real exchange calls."""
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import challenge_rules as rules
from live_order import LiveOrderAdapter, OrderError, MARKET_DEFAULTS


def _mock_bridge(connected=True, official=True, tools=None):
    bridge = MagicMock()
    bridge.info = {
        'connected': connected,
        'provider': 'wallex-official' if official else 'local',
        'calls': 0,
        'last_call': None,
    }
    bridge.remote_tools = tools or {
        'wallex_spot_post_account_orders',
        'wallex_spot_get_orders_client',
        'wallex_spot_delete_orders_client',
        'wallex_spot_get_open_order',
    }
    # Default balance response
    bridge.call.return_value = {
        'result': {
            'balances': {
                'TMN': {'asset': 'TMN', 'value': '5000000', 'locked': '0'},
                'BTC': {'asset': 'BTC', 'value': '0.00000000', 'locked': '0'},
                'USDT': {'asset': 'USDT', 'value': '0.00000000', 'locked': '0'},
            }
        }
    }
    # Default order response
    bridge.order_call.return_value = {
        'result': {
            'clientOrderId': 'test-123',
            'symbol': 'USDTTMN',
            'side': 'BUY',
            'type': 'MARKET',
            'status': 'FILLED',
            'executedQty': '9.52',
            'avgPrice': '105000',
            'fee': '200',
        }
    }
    return bridge


def _config():
    return dict(symbol='USDTTMN', source='wallex_mcp', model='logistic',
                strategy='adaptive', capital=5_000_000, allocation=20,
                stop_loss=1.5, take_profit=3, max_drawdown=5, fee=0.2,
                slippage=0.05, confidence=56, seed=42)


def _adapter(bridge=None, db_path=None):
    b = bridge or _mock_bridge()
    p = db_path or Path(tempfile.mktemp(suffix='.sqlite3'))
    return LiveOrderAdapter(b, p), b, p


class TestLiveOrderAdapter:

    def test_init_creates_table(self):
        adapter, _, db_path = _adapter()
        with sqlite3.connect(db_path) as con:
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            assert 'live_orders' in tables

    def test_client_id_format(self):
        cid = LiveOrderAdapter._make_client_id('USDTTMN', 'BUY')
        assert cid.startswith('madar-USDTTMN-BUY-')
        assert len(cid) <= 36

    def test_client_id_unique(self):
        ids = {LiveOrderAdapter._make_client_id('BTCTMN', 'SELL')
               for _ in range(100)}
        assert len(ids) == 100

    def test_pre_checks_blocked_market(self):
        adapter, _, _ = _adapter()
        try:
            adapter._pre_checks('1BBABYDOGETMN', 'BUY', 100_000, _config())
            assert False, 'Should have raised'
        except (rules.PolicyViolation, OrderError):
            pass

    def test_pre_checks_invalid_side(self):
        adapter, _, _ = _adapter()
        try:
            adapter._pre_checks('USDTTMN', 'LONG', 100_000, _config())
            assert False, 'Should have raised'
        except OrderError:
            pass

    def test_pre_checks_mcp_not_connected(self):
        bridge = _mock_bridge(connected=False)
        adapter, _, _ = _adapter(bridge=bridge)
        try:
            adapter._pre_checks('USDTTMN', 'BUY', 100_000, _config())
            assert False, 'Should have raised'
        except OrderError as e:
            assert 'not connected' in str(e)

    def test_pre_checks_not_official(self):
        bridge = _mock_bridge(official=False)
        adapter, _, _ = _adapter(bridge=bridge)
        try:
            adapter._pre_checks('USDTTMN', 'BUY', 100_000, _config())
            assert False, 'Should have raised'
        except OrderError as e:
            assert 'official' in str(e).lower()

    def test_pre_checks_over_allocation(self):
        adapter, _, _ = _adapter()
        config = _config()
        # allocation=20%, capital=5M => max=1M, trying 2M
        try:
            adapter._pre_checks('USDTTMN', 'BUY', 2_000_000, config)
            assert False, 'Should have raised'
        except OrderError as e:
            assert 'allocation' in str(e).lower() or 'limit' in str(e).lower()

    def test_round_qty_usdttmn(self):
        adapter, _, _ = _adapter()
        qty = adapter._round_qty('USDTTMN', 9.527)
        assert qty == 9.52  # floor to 2 decimals

    def test_round_qty_btctmn(self):
        adapter, _, _ = _adapter()
        qty = adapter._round_qty('BTCTMN', 0.000161295)
        assert qty == 0.000161  # floor to 6 decimals

    def test_round_qty_below_minimum(self):
        adapter, _, _ = _adapter()
        try:
            adapter._round_qty('USDTTMN', 0.001)  # below min 0.5
            assert False, 'Should have raised'
        except OrderError as e:
            assert 'minimum' in str(e).lower()

    def test_place_order_buy_success(self):
        adapter, bridge, db_path = _adapter()
        config = _config()
        result = adapter.place_order('USDTTMN', 'BUY', 105_000, config)
        assert result['status'] in ('accepted', 'reconciled')
        assert result['symbol'] == 'USDTTMN'
        assert result['side'] == 'BUY'
        assert result['client_id'].startswith('madar-')
        # Check DB
        orders = adapter.all_orders()
        assert len(orders) == 1
        assert orders[0]['symbol'] == 'USDTTMN'
        assert orders[0]['side'] == 'BUY'
        # Check bridge was called
        bridge.order_call.assert_called_once()
        call_args = bridge.order_call.call_args
        assert call_args[0][0] == 'place_order'
        assert call_args[0][1]['side'] == 'BUY'
        assert call_args[0][1]['type'] == 'LIMIT'

    def test_place_order_sell_no_balance(self):
        adapter, bridge, _ = _adapter()
        config = _config()
        config['symbol'] = 'USDTTMN'
        try:
            adapter.place_order('USDTTMN', 'SELL', 105_000, config)
            assert False, 'Should have raised'
        except OrderError as e:
            assert 'No' in str(e) or 'sell' in str(e).lower()

    def test_place_order_sell_with_balance(self):
        bridge = _mock_bridge()
        bridge.call.return_value = {
            'result': {
                'balances': {
                    'TMN': {'asset': 'TMN', 'value': '4000000', 'locked': '0'},
                    'USDT': {'asset': 'USDT', 'value': '9.52', 'locked': '0'},
                }
            }
        }
        adapter, _, _ = _adapter(bridge=bridge)
        config = _config()
        result = adapter.place_order('USDTTMN', 'SELL', 105_000, config)
        assert result['status'] in ('accepted', 'reconciled')
        assert result['side'] == 'SELL'

    def test_place_order_mcp_error_reconcile(self):
        bridge = _mock_bridge()
        bridge.order_call.side_effect = [
            ValueError('Connection timeout'),  # place fails
            {'result': {'status': 'NOT_FOUND'}},  # reconcile
        ]
        adapter, _, _ = _adapter(bridge=bridge)
        config = _config()
        result = adapter.place_order('USDTTMN', 'BUY', 105_000, config)
        # Should have tried reconciliation
        assert result['status'] in ('reconciled', 'error')
        assert bridge.order_call.call_count == 2

    def test_persist_intent_before_submit(self):
        """Intent must be recorded BEFORE the MCP call."""
        call_order = []
        bridge = _mock_bridge()
        original_order_call = bridge.order_call

        def tracking_order_call(*args, **kwargs):
            call_order.append('mcp_call')
            return original_order_call(*args, **kwargs)

        bridge.order_call = tracking_order_call
        adapter, _, db_path = _adapter(bridge=bridge)

        # Patch _persist_intent to track call order
        original_persist = adapter._persist_intent
        def tracking_persist(*args, **kwargs):
            call_order.append('persist')
            return original_persist(*args, **kwargs)
        adapter._persist_intent = tracking_persist

        config = _config()
        adapter.place_order('USDTTMN', 'BUY', 105_000, config)
        assert call_order.index('persist') < call_order.index('mcp_call')

    def test_get_order_status(self):
        adapter, bridge, _ = _adapter()
        bridge.order_call.return_value = {
            'result': {'clientOrderId': 'test-123', 'status': 'FILLED',
                       'executedQty': '9.52', 'avgPrice': '105000'}
        }
        # First create an intent
        adapter._persist_intent('test-123', 'USDTTMN', 'BUY', 'MARKET',
                                9.52, None, 1_000_000)
        result = adapter.get_order_status('test-123')
        assert isinstance(result, dict)
        bridge.order_call.assert_called_with('get_order',
                                              {'client_id': 'test-123'})

    def test_cancel_order(self):
        adapter, bridge, _ = _adapter()
        adapter._persist_intent('test-456', 'USDTTMN', 'BUY', 'MARKET',
                                9.52, None, 1_000_000)
        adapter.cancel_order('test-456')
        bridge.order_call.assert_called_with('cancel_order',
                                              {'client_id': 'test-456'})
        orders = adapter.all_orders()
        assert orders[0]['status'] == 'cancelled'

    def test_verify_all_good(self):
        adapter, bridge, _ = _adapter()
        bridge.order_call.return_value = {'result': {'orders': []}}
        result = adapter.verify()
        assert result['ok'] is True
        assert result['tmn_available'] == 5_000_000

    def test_verify_not_connected(self):
        bridge = _mock_bridge(connected=False)
        adapter, _, _ = _adapter(bridge=bridge)
        result = adapter.verify()
        assert result['ok'] is False
        assert any('not connected' in e.lower() for e in result['errors'])

    def test_available_tmn(self):
        adapter, _, _ = _adapter()
        assert adapter.available_tmn() == 5_000_000

    def test_available_base_zero(self):
        adapter, _, _ = _adapter()
        assert adapter.available_base('USDTTMN') == 0

    def test_blocked_market_order(self):
        adapter, _, _ = _adapter()
        config = _config()
        try:
            adapter.place_order('1BBABYDOGETMN', 'BUY', 100, config)
            assert False, 'Should have raised'
        except (rules.PolicyViolation, OrderError):
            pass

    def test_refresh_markets_graceful(self):
        """refresh_markets should not crash on unexpected data."""
        adapter, bridge, _ = _adapter()
        bridge.call.return_value = {'unexpected': 'data'}
        adapter.refresh_markets()  # Should not raise
        assert len(adapter.markets) >= len(MARKET_DEFAULTS)


if __name__ == '__main__':
    import pytest
    sys.exit(pytest.main([__file__, '-v', '--tb=short']))

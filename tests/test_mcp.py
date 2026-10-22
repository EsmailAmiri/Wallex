import unittest
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError
from engine import demo_candles, validate
from mcp_link import MCPBridge
from server import source_bars
from wallex_mcp import official_get


class MCPTests(unittest.TestCase):
    def test_agent_reads_candles_through_mcp(self):
        bars = demo_candles(100)
        with patch('server.bridge.call', return_value=dict(symbol='BTCTMN', timeframe_seconds=900, candles=bars)) as call:
            result = source_bars(validate(dict(source='wallex_mcp')), 100)
        call.assert_called_once_with('get_market_candles', dict(symbol='BTCTMN', count=100))
        self.assertEqual(result, bars)

    def test_agent_rejects_mcp_symbol_or_timeframe_mismatch(self):
        for symbol, timeframe in [('ETHTMN', 900), ('BTCTMN', 60)]:
            with patch('server.bridge.call', return_value=dict(symbol=symbol, timeframe_seconds=timeframe)), self.assertRaises(ValueError):
                source_bars(validate(dict(source='wallex_mcp')), 100)

    def test_missing_key_does_not_make_private_request(self):
        with patch('wallex_mcp.settings', return_value={'WALLEX_API_KEY':''}), patch('wallex_mcp.build_opener') as network:
            with self.assertRaises(ValueError):
                official_get('/account/balances', authenticated=True)
        network.assert_not_called()

    def test_private_key_fixed_host_and_errors_redacted(self):
        sentinel = 'TEST-SECRET-NOT-A-REAL-KEY'
        opener = MagicMock()
        opener.open.side_effect = HTTPError('https://api.wallex.ir', 401, sentinel, {}, None)
        with patch('wallex_mcp.settings', return_value={'WALLEX_API_KEY':sentinel}), patch('wallex_mcp.build_opener', return_value=opener):
            with self.assertRaises(ValueError) as error:
                official_get('/account/balances', authenticated=True)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.wallex.ir/v1/account/balances')
        self.assertEqual(request.get_header('X-api-key'), sentinel)
        self.assertNotIn(sentinel, str(error.exception))

    def test_bridge_refuses_financial_actions(self):
        with self.assertRaises(ValueError):
            MCPBridge().call('place_order', {'side':'BUY'})


if __name__ == '__main__':
    unittest.main()

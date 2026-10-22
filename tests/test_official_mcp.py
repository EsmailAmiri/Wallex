import unittest
from unittest.mock import patch
import challenge_rules as rules
from mcp_link import OFFICIAL_URL, OFFICIAL_TOOLS, remote_headers, official_request, official_response, MCPBridge


class OfficialMCPTests(unittest.TestCase):
    def test_key_is_sent_only_to_exact_official_endpoint(self):
        config=dict(WALLEX_API_KEY='TEST-ONLY',WALLEX_MCP_TOKEN='TOKEN-ONLY')
        self.assertEqual(remote_headers(OFFICIAL_URL,config),{'X-API-Key':'TEST-ONLY'})
        for url in ('https://example.com/mcp','https://api.wallex.ir.evil.example/mcp','https://api.wallex.ir/other','http://api.wallex.ir/mcp'):
            self.assertNotIn('X-API-Key',remote_headers(url,config))

    def test_official_candle_mapping_uses_discovered_schema(self):
        name,args=official_request('get_market_candles',dict(symbol='btc/tmn',count=120),now=1000000)
        self.assertEqual(name,'wallex_basic_candle_history')
        self.assertEqual(args,{'symbol':'BTCTMN','resolution':'15','from':1000000-130*900,'to':1000000})
        with self.assertRaises(rules.PolicyViolation):
            official_request('get_market_candles',dict(symbol='FET/USDT',count=120))

    def test_official_envelope_unwrapped_without_mutating_balances(self):
        balances={'balances':{'TMN':{'value':'0'}}}
        self.assertEqual(official_response('get_account_balances',{'success':True,'result':balances},{}),balances)
        with self.assertRaises(ValueError):
            official_response('get_account_balances',{'success':False,'message':'do not echo'}, {})

    def test_official_markets_still_filtered_by_hackathon_rules(self):
        response=official_response('get_markets',{'result':{'symbols':{'BTCTMN':{},'FETUSDT':{},'1BBABYDOGEUSDT':{}}}}, {})
        self.assertEqual(set(response['symbols']),{'BTCTMN'})

    def test_remote_candles_resampled_and_unclosed_excluded(self):
        count=82*15
        raw={'s':'ok','t':[i*60 for i in range(count)]}
        raw.update({key:[value]*count for key,value in {'o':100,'h':102,'l':99,'c':101,'v':2}.items()})
        with patch('mcp_link.time.time',return_value=82*900):
            response=official_response('get_market_candles',raw,{'symbol':'BTCTMN','count':80})
        self.assertEqual(len(response['candles']),80)
        self.assertEqual(response['timeframe_seconds'],900)
        self.assertEqual(response['candles'][-1]['v'],30)

    def test_official_financial_tools_unavailable_even_if_discovered(self):
        bridge=MCPBridge()
        bridge.remote_tools={'wallex_spot_post_account_orders','wallex_credit_enable_user_package_renew','wallex_margin_open_position'}
        for tool in bridge.remote_tools:
            with self.assertRaises(ValueError):
                bridge.call(tool,{})
        self.assertTrue(all('post' not in name and 'margin' not in name for name in OFFICIAL_TOOLS.values()))


if __name__=='__main__':
    unittest.main()

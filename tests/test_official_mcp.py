import unittest
from unittest.mock import patch
import challenge_rules as rules
from mcp_link import OFFICIAL_URL, OFFICIAL_TOOLS, remote_headers, official_request, official_response, MCPBridge


class OfficialMCPTests(unittest.TestCase):
    def test_credit_inspection_only_allows_argument_free_reads(self):
        expected = dict(get_credit_sub_accounts='wallex_credit_active_sub_accounts_list',
                        get_credit_package='wallex_credit_active_user_package_details',
                        get_credit_overview='wallex_credit_active_user_packages_overview',
                        get_credit_history='wallex_credit_user_package_history_list',
                        get_credit_balances='wallex_credit_sub_account_balances_details')
        for name, native in expected.items():
            self.assertEqual(official_request(name, {}), (native, {}))
            with self.assertRaises(ValueError):
                official_request(name, {'subAccountClientId': 'different-account'})
        for name in ('wallex_credit_enable_user_package_renew',
                     'wallex_credit_activate_user_package', 'wallex_spot_post_account_orders'):
            with self.assertRaises(ValueError):
                official_request(name, {})

    def test_current_market_list_filters_both_representations(self):
        response=official_response('get_markets',{'markets':[
            {'symbol':'BTCTMN','is_spot':True},{'symbol':'FETUSDT'},{'symbol':'1BBABYDOGEUSDT'}]}, {})
        self.assertEqual(list(response['symbols']),['BTCTMN'])
        self.assertEqual([m['symbol'] for m in response['markets']],['BTCTMN'])
        for invalid in ({'markets':{}},{'markets':[{}]},{}):
            with self.assertRaises(ValueError):official_response('get_markets',invalid,{})

    def test_spot_market_tool_is_read_only_and_filters_denied_pairs(self):
        self.assertEqual(official_request('get_spot_markets', {}), ('wallex_spot_get_markets', {}))
        with self.assertRaises(ValueError):
            official_request('get_spot_markets', {'symbol': 'BTCTMN'})
        result = official_response('get_spot_markets', {'result': {'markets': [
            {'symbol': 'BTCTMN', 'is_spot': True}, {'symbol': 'FETUSDT', 'is_spot': True}]}}, {})
        self.assertEqual(list(result['symbols']), ['BTCTMN'])

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

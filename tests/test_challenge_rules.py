import unittest
import tempfile
from pathlib import Path
from dataclasses import asdict
from unittest.mock import patch
from engine import DEFAULTS, Account, validate, backtest, new_account, step
import challenge_rules as rules
from server import fresh
from mcp_link import MCPBridge
import server
import competition


class ChallengeRulesTests(unittest.TestCase):
    def test_competition_cutoff_settles_shadow_before_credit_expiry(self):
        state = fresh({'source': 'demo'})
        state['config'].update(source='wallex_mcp', model='hybrid_llm')
        account = new_account(state['config'], started_at=rules.START_AT)
        account.execute('BUY', state['bars'][-1]['c'], state['config'], 'entry', rules.START_AT)
        state.update(account=asdict(account), running=True)
        with tempfile.TemporaryDirectory() as temporary, patch.object(server, 'STATE', state), patch.object(server, 'DATA', Path(temporary)), patch('server.time.time', return_value=competition.END_AT), patch('server.source_bars', side_effect=AssertionError('Must not fetch')), patch('engine.decide', side_effect=AssertionError('Must not call model')):
            server.run_cycle()
        self.assertFalse(state['running'])
        self.assertEqual(state['account']['quantity'], 0)
        self.assertEqual(state['account']['expires_at'], rules.END_AT)
        self.assertLess(state['trades'][-1]['t'], rules.END_AT)

    def test_running_shadow_stops_at_expiry_without_waiting_for_market_data(self):
        state=fresh({'source':'demo'})
        state['config']['source']='wallex_mcp'
        account=new_account(state['config'],started_at=rules.START_AT)
        account.execute('BUY',state['bars'][-1]['c'],state['config'],'entry',rules.START_AT)
        state.update(account=asdict(account),running=True)
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(server,'STATE',state),patch.object(server,'DATA',Path(temporary)),\
                 patch('server.time.time',return_value=rules.END_AT),\
                 patch('server.source_bars',side_effect=AssertionError('Must not fetch more candles')):
                server.run_cycle()
        self.assertFalse(state['running'])
        self.assertEqual(state['account']['quantity'],0)
        self.assertTrue(state['account']['halted'])
        self.assertEqual(state['trades'][-1]['reference_price_time'],state['bars'][-1]['t'])

    def test_all_345_disabled_markets_blocked_at_engine_boundary(self):
        self.assertEqual(len(rules.BLOCKED_DISPLAY), 345)
        account = Account()
        for symbol in rules.BLOCKED_DISPLAY:
            with self.subTest(symbol=symbol), self.assertRaises(rules.PolicyViolation):
                account.execute('BUY',100,{**DEFAULTS,'symbol':symbol},'bypass',1)
        self.assertEqual(account.cash,5_000_000)
        self.assertEqual(account.quantity,0)

    def test_symbol_aliases_cannot_bypass_restrictions(self):
        for symbol in ('fet/usdt',' FETUSDT ','FET-USDT','FET_USDT','1BBABYDOGE/USDT','2Z/TMN'):
            with self.subTest(symbol=symbol), self.assertRaises(rules.PolicyViolation):
                rules.assert_market_allowed(symbol)
        self.assertEqual(validate({'symbol':'btc/tmn'})['symbol'],'BTCTMN')

    def test_principal_and_rules_cannot_be_changed_through_config(self):
        for payload in ({'capital':4_999_999},{'capital':5_000_001},{'capital':float('nan')},
                        {'policy_enabled':False},{'duration_seconds':999999},{'expires_at':0}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate(payload)

    def test_plan_boundaries_and_zero_credit_fee(self):
        self.assertEqual(rules.END_AT-rules.START_AT,72*3600)
        self.assertEqual(rules.CREDIT_FEE_TMN,0)
        rules.assert_plan_active(rules.START_AT)
        rules.assert_plan_active(rules.END_AT-1)
        for now in (rules.START_AT-1,rules.END_AT,rules.END_AT+1):
            with self.assertRaises(rules.PolicyViolation):
                rules.assert_plan_active(now)

    def test_resets_do_not_extend_actual_credit_window(self):
        bars=[dict(t=i*900,o=100,h=100,l=100,c=100,v=1) for i in range(120)]
        with patch('server.source_bars',return_value=bars):
            a=fresh({'source':'wallex_mcp'})
            b=fresh({'source':'wallex_mcp','seed':99})
        self.assertNotEqual(a['session_id'],b['session_id'])
        self.assertEqual(a['account']['expires_at'],rules.END_AT)
        self.assertEqual(b['account']['expires_at'],rules.END_AT)

    def test_backtest_is_limited_to_72h_and_settles_to_tmn(self):
        bars=[dict(t=i*900,o=100,h=100,l=100,c=100,v=1) for i in range(480)]
        with patch('engine.decide',return_value=dict(action='BUY',confidence=100,reason='test')):
            report=backtest(bars,validate({}))
        self.assertEqual(report['evaluated_until']-report['evaluated_from'],72*3600)
        self.assertEqual(report['account']['quantity'],0)
        self.assertTrue(all(t['t']<report['evaluated_until'] for t in report['trades'] if t['action']=='BUY'))
        self.assertEqual(report['trades'][-1]['action'],'SELL')

    def test_deadline_overrides_model_and_prevents_reentry(self):
        config=validate({})
        account=new_account(config,started_at=rules.START_AT)
        account.execute('BUY',100,config,'entry',rules.START_AT)
        with patch('engine.decide',return_value=dict(action='BUY',confidence=100,reason='test')):
            result=step(account,[],dict(t=rules.END_AT,o=100,c=100),config)
        self.assertEqual(result['action'],'SELL')
        self.assertEqual(account.quantity,0)
        self.assertTrue(account.halted)
        with self.assertRaises(ValueError):
            account.execute('BUY',100,config,'bypass',rules.END_AT)

    def test_account_features_and_mcp_financial_tools_blocked(self):
        for capability in rules.FORBIDDEN_CAPABILITIES:
            with self.subTest(capability=capability),self.assertRaises(rules.PolicyViolation):
                rules.assert_capability(capability)
        for tool in ('withdraw','transfer','create_margin_order','create_trading_bot','renew_credit','place_order'):
            with self.subTest(tool=tool),self.assertRaises(ValueError):
                MCPBridge().call(tool,{})


if __name__=='__main__':
    unittest.main()

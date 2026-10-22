import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import llm_gateway as gateway
import competition
import challenge_rules as rules
from engine import DEFAULTS, Account, decide, demo_candles, step, validate

VOTE = dict(approve=True, confidence=85, reason_code='trend')
CONFIG = dict(LLM_BASE_URL='http://127.0.0.1:20128/v1', LLM_API_KEY='test-secret',
              LLM_MODEL='test-model', LLM_TIMEOUT_SECONDS='2', LLM_MODE='live')


class LLMTests(unittest.TestCase):
    def test_cache_and_offline_replay_are_identical_and_do_not_store_key(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'cache.sqlite3'
            with patch.object(gateway, 'CACHE', path), patch.object(gateway, 'settings', return_value=CONFIG), patch.object(gateway, 'request_vote', return_value=VOTE) as request:
                first = gateway.review({'rsi': 60})
                replay = {**CONFIG, 'LLM_MODE': 'replay', 'LLM_API_KEY': ''}
                with patch.object(gateway, 'settings', return_value=replay):
                    self.assertEqual(first, gateway.review({'rsi': 60}))
                    self.assertFalse(gateway.review({'rsi': 70})['approve'])
                request.assert_called_once()
            self.assertNotIn(b'test-secret', path.read_bytes())

    def test_report_replay_uses_recorded_failure_without_network(self):
        features={'rsi':60}
        with patch.object(gateway,'settings',return_value=CONFIG):
            recorded=dict(approve=False,confidence=0,reason_code='uncertain',
                          status='unavailable',model=CONFIG['LLM_MODEL'],
                          prompt_version=gateway.VERSION,
                          request_sha256=gateway.request_sha256(CONFIG,features))
            replay=gateway.ReplayVotes([{'llm':recorded}])
            with patch.object(gateway,'request_vote',side_effect=AssertionError('No network')):
                self.assertEqual(replay(features),recorded)
            replay.assert_finished()
            with self.assertRaises(ValueError):
                gateway.ReplayVotes([{'llm':recorded}])({'rsi':61})

    def test_provider_timeout_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(gateway, 'CACHE', Path(folder)/'cache.sqlite3'), patch.object(gateway, 'settings', return_value=CONFIG), patch.object(gateway, 'request_vote', side_effect=TimeoutError('secret error body')):
            vote = gateway.review({'rsi': 60})
            self.assertFalse(vote['approve'])
            self.assertNotIn('secret', json.dumps(vote))

    def test_malformed_votes_rejected(self):
        for vote in (dict(VOTE, approve='true'), dict(VOTE, confidence=float('nan')),
                     dict(VOTE, confidence=True), dict(VOTE, confidence=101),
                     dict(VOTE, reason_code='buy now'), dict(VOTE, api_key='secret')):
            with self.assertRaises(ValueError):
                gateway.validate_vote(vote)

    def test_remote_http_and_embedded_credentials_rejected(self):
        for url in ('http://example.com/v1', 'https://user:pass@example.com/v1', 'https://example.com/v1?key=abc'):
            with self.assertRaises(ValueError):
                gateway.endpoint({**CONFIG, 'LLM_BASE_URL': url})

    def test_llm_can_veto_entry_but_does_not_receive_private_data(self):
        bars = demo_candles(80)
        data = dict(fast=102, slow=100, rsi=60, z=0, volatility=.01, features=[0]*4)
        config = validate({**DEFAULTS, 'model': 'hybrid_llm', 'strategy': 'momentum'})
        with patch('engine.indicators', return_value=data), patch('engine.predict', return_value=(.8, 50)), patch('llm_gateway.review', return_value=dict(VOTE, approve=False, status='ok')) as review:
            self.assertEqual(decide(bars, config)['action'], 'HOLD')
            self.assertNotIn('api_key', json.dumps(review.call_args.args[0]).lower())
            self.assertNotIn('symbol', review.call_args.args[0])

    def test_llm_can_approve_real_candidate(self):
        bars = demo_candles(80)
        features = dict(fast=102, slow=100, rsi=60, z=0, volatility=.01, features=[0]*4)
        config = validate({**DEFAULTS, 'model':'hybrid_llm', 'strategy':'momentum'})
        with patch('engine.indicators', return_value=features), patch('engine.predict', return_value=(.8, 50)), patch('llm_gateway.review', return_value={**VOTE, 'status':'ok'}):
            decision=decide(bars,config)
        self.assertEqual(decision['action'],'BUY')
        self.assertTrue(decision['llm']['approve'])

    def test_forced_exit_and_expiry_never_call_model(self):
        config = validate({**DEFAULTS, 'model': 'hybrid_llm'})
        for expired in (False, True):
            account = Account(expires_at=2 if expired else 100)
            account.execute('BUY', 100, config, 'entry', 1)
            with patch('engine.decide', side_effect=AssertionError('Model must not be called')):
                item = step(account, [], dict(t=2, o=90, c=90), config)
            self.assertEqual(item['trade']['action'], 'SELL')

    def test_competition_cutoff_is_earlier_and_does_not_change_credit_expiry(self):
        self.assertLess(competition.END_AT, rules.END_AT)
        competition.assert_active(competition.END_AT-1)
        with self.assertRaises(ValueError):
            competition.assert_active(competition.END_AT)
        rules.assert_plan_active(competition.END_AT)


if __name__ == '__main__':
    unittest.main()

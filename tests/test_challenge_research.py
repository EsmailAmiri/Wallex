import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

import challenge_research as c
import hybrid_research as h
from engine import demo_candles


class ChallengeResearchTests(unittest.TestCase):
    def test_json_protocol_is_nonstreaming_and_success_is_replayed(self):
        config=dict(LLM_MODEL='fake',LLM_API_KEY='secret-for-test',LLM_BASE_URL='http://localhost/v1')
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        response.read.return_value=json.dumps({'choices':[{'message':{'content':json.dumps(
            dict(approve=True,confidence=80,reason_code='trend'))}}]}).encode()
        opener=Mock();opener.open.return_value=response
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'votes.json'
            with patch('wallex_settings.settings',return_value=config):voter=c.Voter(path,'live')
            with patch('challenge_research.build_opener',return_value=opener):
                vote=voter({'rsi':55})
            body=json.loads(opener.open.call_args.args[0].data)
            self.assertIs(body['stream'],False)
            self.assertTrue(vote['approve']);self.assertEqual(vote['status'],'ok')
            with patch('challenge_research.build_opener',side_effect=AssertionError('Network forbidden')):
                self.assertEqual(vote,c.Voter(path,'replay')({'rsi':55}))

    def test_hourly_context_does_not_include_incomplete_or_future_hour(self):
        bars=demo_candles(300)
        # Align with true UTC hour boundaries for the fixture.
        for i,b in enumerate(bars):b['t']=i*900
        context=c.hourly_context(bars)
        self.assertNotIn(59*3600,context)
        self.assertIn(60*3600,context)
        before=context[60*3600]
        for b in bars[240:]:
            b['c']*=1.01;b['h']=max(b['h'],b['c'])
        self.assertEqual(before,c.hourly_context(bars)[60*3600])
        self.assertNotIn(60*3600,c.hourly_context(bars[:239]))

    def test_ev_gate_and_llm_veto_cannot_create_entry(self):
        scorer=Mock();scorer.score.return_value={'ensemble':-.1}
        voter=Mock(return_value=dict(approve=True,confidence=99,status='ok'))
        graph=c.graph_for(scorer,'ensemble',0.,voter)
        state=dict(symbol='BTCTMN',x=[0]*13,features={'rsi':55})
        self.assertFalse(graph.invoke(state)['accepted']);voter.assert_not_called()
        scorer.score.return_value={'ensemble':.3}
        voter.return_value=dict(approve=False,confidence=90,status='ok')
        self.assertFalse(graph.invoke(state)['accepted'])
        self.assertNotIn('symbol',voter.call_args.args[0])
        with self.assertRaises(ValueError):
            c.graph_for(scorer,'ensemble',-1)

    def test_portable_replay_no_credentials_no_network_and_failure_frozen(self):
        config=dict(LLM_MODEL='fake-model',LLM_API_KEY='test-secret-123',LLM_BASE_URL='http://localhost/v1')
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'votes.json'
            with patch('wallex_settings.settings',return_value=config):
                live=c.Voter(path,'live',1)
            with patch('challenge_research.build_opener',side_effect=TimeoutError('test-secret-123')) as network:
                result=live({'rsi':55})
                self.assertEqual(result,live({'rsi':55}));network.assert_called_once()
                self.assertFalse(result['approve'])
                with self.assertRaises(ValueError):live({'rsi':56})
            with (patch('wallex_settings.settings',side_effect=AssertionError('No settings')),
                  patch('challenge_research.build_opener',side_effect=AssertionError('No network'))):
                replay=c.Voter(path,'replay')
                self.assertEqual(result,replay({'rsi':55}))
                with self.assertRaises(ValueError):replay({'rsi':56})
            self.assertNotIn('test-secret-123',path.read_text())

    def test_regression_scaler_only_train_and_label_boundary_purged(self):
        train=[dict(t=i,end=i+1,x=[i%5,i%7],pnl=(i%5-2)*1000) for i in range(120)]
        cal=[dict(t=i,end=i+1,x=[1000,i%7],pnl=-1000) for i in range(200,224)]
        scorer=c.fit_returns(train,cal)
        self.assertLess(scorer.scaler.mean_[0],5)
        self.assertEqual(set(scorer.score([1,2])),set(c.VARIANTS))
        with self.assertRaises(ValueError):c.fit_returns(train,[dict(s,t=50) for s in cal])


if __name__=='__main__':unittest.main()

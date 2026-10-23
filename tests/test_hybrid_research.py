import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

import challenge_rules as rules
import hybrid_research as h
from freeze_research_history import convert
from engine import demo_candles


class HybridResearchTests(unittest.TestCase):
    def setUp(self):
        self.bars = demo_candles(100)
        self.config = h.config_for('BTCTMN')

    def test_causal_features_ignore_unseen_bar_and_match_prefix(self):
        bars = demo_candles(82)
        first = h.make_rows('BTCTMN', bars)[80]
        bars[80]['c'] *= 1.01
        bars[80]['h'] = max(bars[80]['h'], bars[80]['c'])
        second = h.make_rows('BTCTMN', bars)[80]
        self.assertEqual(first, second)
        self.assertEqual(first, h.feature_row(bars[:80]))

    def test_gap_is_never_imputed_and_warmup_restarts(self):
        bars = demo_candles(150)
        del bars[70]
        with self.assertRaises(ValueError):
            h.validate_bars('BTCTMN', bars)
        rows = h.make_rows('BTCTMN', bars)
        self.assertIn(69, rows)
        self.assertNotIn(70, rows)
        self.assertNotIn(129, rows)
        self.assertIn(130, rows)
        with self.assertRaises(ValueError):
            h.isolated_label('BTCTMN', bars, rows, 65)

    def test_policy_rejects_blocked_aliases_and_overrides(self):
        blocked = rules.BLOCKED_DISPLAY[0]
        for symbol in (blocked, blocked.lower(), blocked.replace('/', '_')):
            with self.assertRaises(rules.PolicyViolation):
                h.config_for(symbol)
        for config in (dict(self.config, capital=1), dict(self.config, expires_at=9999999999)):
            with self.assertRaises(ValueError):
                h.simulate('BTCTMN', self.bars, {}, self.bars[60]['t'], self.bars[80]['t'], config=config)

    def test_purge_removes_labels_crossing_boundary(self):
        samples = [dict(t=10, end=19), dict(t=11, end=20), dict(t=12, end=21), dict(t=20, end=22)]
        self.assertEqual(h.temporal_samples(samples, 10, 20), samples[:1])

    def test_invalid_or_repeated_data_rejected(self):
        for key, value in (('c', float('nan')), ('v', -1), ('t', self.bars[0]['t'])):
            bars = [dict(b) for b in self.bars]
            bars[1][key] = value
            with self.assertRaises(ValueError):
                h.validate_bars('BTCTMN', bars, allow_gaps=True)

    def test_public_minute_history_requires_complete_buckets(self):
        timestamps = list(range(0,1800,60))
        data = dict(s='ok', t=timestamps, **{k:[100.]*30 for k in ('o','h','l','c')}, v=[1.]*30)
        complete = convert(data,0,1800)
        self.assertEqual([b['t'] for b in complete], [0,900])
        self.assertEqual(complete[0]['v'], 15)
        for key in ('t','o','h','l','c','v'):
            del data[key][5]
        gapped = convert(data,0,1800)
        self.assertEqual([b['t'] for b in gapped], [900])
        self.assertEqual(convert(data,0,1799), [])

    def test_exit_is_independent_of_graph_and_final_tmn_has_costs(self):
        bars = [dict(t=i*900, o=100., c=100., h=100., l=100., v=1.) for i in range(90)]
        rows = {i: dict(x=[0]*11, eligible=True, exit=False) for i in range(60, 90)}
        # Entry only at 60; position is liquidated by risk at 61 without graph call.
        bars[61].update(o=90., c=90., h=90., l=90.)
        for i in range(62,90):
            rows[i]['eligible'] = False
        graph = Mock()
        graph.invoke.return_value = dict(accepted=True, reason='quant_pass', scores={})
        report = h.simulate('BTCTMN', bars, rows, bars[60]['t'], bars[80]['t'], graph)
        graph.invoke.assert_called_once()
        self.assertEqual([t['action'] for t in report['trades']], ['BUY', 'SELL'])
        self.assertEqual(report['account']['quantity'], 0)
        self.assertGreater(report['metrics']['fees_tmn'], 0)
        self.assertAlmostEqual(report['account']['cash']-rules.CREDIT_TMN, report['metrics']['pnl_tmn'])
        self.assertLess(report['metrics']['pnl_tmn'], -100_000)

    def test_terminal_and_horizon_close_charge_both_sides(self):
        bars = [dict(t=i*900, o=100., c=100., h=100., l=100., v=1.) for i in range(90)]
        rows = {i: dict(x=[0]*11, eligible=i == 60, exit=False) for i in range(60,90)}
        graph = Mock()
        graph.invoke.return_value = dict(accepted=True, reason='quant_pass', scores={})
        for end in (bars[65]['t'], bars[85]['t']):
            r = h.simulate('BTCTMN', bars, rows, bars[60]['t'], end, graph)
            self.assertEqual(r['account']['quantity'], 0)
            self.assertEqual(r['metrics']['round_trips'], 1)
            self.assertLess(r['metrics']['pnl_tmn'], 0)
            self.assertEqual(len(r['trades']), 2)
        y, pnl, _ = h.isolated_label('BTCTMN', bars, rows, 60)
        self.assertEqual(y, 0)
        self.assertAlmostEqual(pnl, r['metrics']['pnl_tmn'])

    def test_historical_72h_limit_cannot_be_extended(self):
        with self.assertRaises(ValueError):
            h.simulate('BTCTMN', self.bars, {}, self.bars[60]['t'], self.bars[60]['t']+rules.DURATION_SECONDS+1)

    def test_graph_llm_only_receives_candidate_and_can_only_veto(self):
        scorer = Mock()
        scorer.score.return_value = dict(logistic=.9, mlp=.9)
        veto = Mock(return_value=dict(approve=False, confidence=99, status='ok'))
        graph = h.entry_graph(scorer, 'both', .58, veto)
        data = dict(symbol='BTCTMN', x=[0]*11, eligible=False, features={'rsi': 55})
        self.assertFalse(graph.invoke(data)['accepted'])
        scorer.score.assert_not_called()
        veto.assert_not_called()
        self.assertFalse(graph.invoke(dict(data, eligible=True))['accepted'])
        self.assertEqual(set(veto.call_args.args[0]), {'rsi','scores'})
        scorer.score.return_value = dict(logistic=.1, mlp=.1)
        veto.reset_mock()
        self.assertFalse(graph.invoke(dict(data, eligible=True))['accepted'])
        veto.assert_not_called()

    def test_frozen_scaler_calibration_and_training_do_not_overlap(self):
        train = [dict(t=i, end=i+1, x=[i%7, i%2], y=i%2) for i in range(80)]
        cal = [dict(t=i, end=i+1, x=[10000+i, i%2], y=i%2) for i in range(100,124)]
        scorer = h.fit_scorer(train, cal)
        self.assertLess(scorer.scaler.mean_[0], 10)
        before = scorer.scaler.mean_.copy()
        probs = scorer.score([100000,1])
        np.testing.assert_array_equal(scorer.scaler.mean_, before)
        self.assertTrue(all(0 <= p <= 1 for p in probs.values()))
        with self.assertRaises(ValueError):
            h.fit_scorer(train, [dict(s, t=50) for s in cal])

    def test_research_vote_replay_is_exact_immutable_and_prompt_bound(self):
        config = dict(LLM_MODEL='test', LLM_API_KEY='secret-never-export',
                      LLM_BASE_URL='http://127.0.0.1:20128/v1', LLM_TIMEOUT_SECONDS='8')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'votes.sqlite3'
            with self.assertRaises(ValueError):
                h.ResearchVotes(path, 'replay', config=config)
            live = h.ResearchVotes(path, 'live', config=config)
            with patch('llm_gateway.request_vote', return_value=dict(approve=True, confidence=80, reason_code='trend')) as request:
                vote = live({'rsi':55})
                self.assertEqual(vote, live({'rsi':55}))
                request.assert_called_once()
            replay = h.ResearchVotes(path, 'replay', config=config)
            with patch('llm_gateway.request_vote', side_effect=AssertionError('No network')):
                self.assertEqual(vote, replay({'rsi':55}))
                with self.assertRaises(ValueError):
                    replay({'rsi':56})
                with patch('llm_gateway.SYSTEM', 'different prompt'):
                    with self.assertRaises(ValueError):
                        replay({'rsi':55})
            self.assertNotIn(b'secret-never-export', path.read_bytes())

    def test_failure_is_frozen_and_budget_never_silently_changes_outcome(self):
        config = dict(LLM_MODEL='test', LLM_API_KEY='secret', LLM_BASE_URL='http://localhost/v1')
        with tempfile.TemporaryDirectory() as folder:
            live = h.ResearchVotes(Path(folder)/'votes.sqlite3', 'live', max_calls=1, config=config)
            with patch('llm_gateway.request_vote', side_effect=TimeoutError('secret')) as request:
                vote = live({'rsi':55})
                self.assertFalse(vote['approve'])
                self.assertEqual(vote, live({'rsi':55}))
                request.assert_called_once()
                with self.assertRaises(ValueError):
                    live({'rsi':56})
            self.assertNotIn('secret', json.dumps(vote))


if __name__ == '__main__':
    unittest.main()

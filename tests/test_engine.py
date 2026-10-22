import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import DEFAULTS, Account, backtest, decide, demo_candles, new_account, step, validate


class TradingTests(unittest.TestCase):
    def setUp(self):
        self.config = validate(DEFAULTS)

    def test_round_trip_costs_and_position_cap(self):
        account = new_account(self.config)
        trade = account.execute('BUY', 100, self.config, 'test', 1)
        self.assertAlmostEqual(account.cost, self.config['capital'] * .2)
        self.assertGreaterEqual(account.cash, 0)
        self.assertGreater(trade['fee'], 0)
        exit_trade = account.execute('SELL', 100, self.config, 'test', 2)
        self.assertLess(exit_trade['pnl'], 0)
        self.assertAlmostEqual(account.cash - self.config['capital'], account.realized)
        self.assertEqual(account.quantity, 0)

    def test_no_future_information_and_next_open_execution(self):
        bars = demo_candles(100)
        decision = decide(bars[:80], self.config)
        altered = [dict(b) for b in bars]
        for b in altered[80:]:
            b['c'] *= 10
        self.assertEqual(decision, decide(altered[:80], self.config))
        account = new_account(self.config)
        with patch('engine.decide', return_value=dict(action='BUY', reason='signal', confidence=80)) as signal:
            result = step(account, bars[:80], bars[80], self.config)
        self.assertEqual(len(signal.call_args.args[0]), 80)
        self.assertAlmostEqual(result['trade']['price'], bars[80]['o'] * 1.0005)

    def test_stop_signal_survives_recovery_and_executes_at_next_open(self):
        account = new_account(self.config)
        account.execute('BUY', 100, self.config, 'entry', 0)
        with patch('engine.decide', return_value=dict(action='HOLD', reason='hold', confidence=60)):
            first = step(account, [], dict(t=1, o=100, c=97), self.config)
            self.assertIsNone(first['trade'])
            second = step(account, [], dict(t=2, o=103, c=103), self.config)
        self.assertEqual(second['trade']['action'], 'SELL')
        self.assertIn('حد ضرر', second['trade']['reason'])
        self.assertAlmostEqual(second['trade']['price'], 103 * .9995)

    def test_drawdown_halt_prevents_new_positions(self):
        account = Account(cash=4_000_000, peak=5_000_000)
        self.assertIsNotNone(account.risk(100, self.config))
        self.assertTrue(account.halted)
        self.assertIsNone(account.execute('BUY', 100, self.config, 'entry', 0))

    def test_backtest_deterministic_and_settles_with_costs(self):
        bars = demo_candles(100)
        first = backtest(bars, self.config)
        self.assertEqual(first, backtest(bars, self.config))
        self.assertEqual(first['account']['quantity'], 0)
        self.assertAlmostEqual(first['account']['cash'] - self.config['capital'], first['metrics']['realized'])
        self.assertEqual(first['metrics']['trades'], len(first['trades']))

    def test_invalid_inputs_rejected(self):
        for update in ({'capital': float('nan')}, {'allocation': 101}, {'model':'fake'}, {'source':'live'}):
            with self.assertRaises(ValueError):
                validate({**self.config, **update})

    def test_breakout_uses_previous_highs_and_next_open_fill(self):
        bars = demo_candles(81)
        for bar in bars[:80]:
            bar.update(o=100, h=101, l=99, c=100)
        bars[79].update(o=100, h=104, c=103)
        bars[80].update(o=105, h=106, l=104, c=105)
        config = validate({**self.config, 'strategy':'breakout'})
        features = dict(fast=102, slow=100, rsi=60, z=0, volatility=.01, features=[0]*4)
        with patch('engine.predict', return_value=(.8, 50)), patch('engine.indicators', return_value=features):
            decision = decide(bars[:80], config)
            report = step(new_account(config), bars[:80], bars[80], config)
        self.assertEqual(decision['action'], 'BUY')
        self.assertAlmostEqual(report['trade']['price'], 105 * 1.0005)

    def test_open_source_indicators_use_only_observed_bars(self):
        from ta_indicators import snapshot
        bars = demo_candles(100)
        observed = snapshot(bars[:80])
        altered = [dict(bar) for bar in bars]
        for bar in altered[80:]:
            bar.update(o=bar['o']*10, h=bar['h']*10, l=bar['l']*10, c=bar['c']*10)
        self.assertEqual(observed, snapshot(altered[:80]))
        self.assertGreater(observed['atr_pct'], 0)


if __name__ == '__main__':
    unittest.main()

import unittest

import challenge_rules as rules
import competition
from live_spot_policy import validate_limit_order


MARKET = {'symbol': 'BTCTMN', 'is_spot': True, 'is_market_type_enable': True,
          'amount_precision': 8, 'price_precision': 0,
          'minQty': '0.000001', 'minNotional': '100000'}


class LiveSpotPolicyTests(unittest.TestCase):
    def order(self, **changes):
        args = dict(symbol='BTC/TMN', side='BUY', price='22000000000',
                    quantity='0.00001', market=MARKET, available_tmn='5000000',
                    available_base='0.1', exposure_limit_tmn='250000',
                    now=rules.START_AT + 3600)
        args.update(changes)
        return validate_limit_order(**args)

    def test_validated_intent_uses_fixed_policy(self):
        intent = self.order()
        self.assertEqual(intent['symbol'], 'BTCTMN')
        self.assertEqual(intent['notional_tmn'], '220000.00000')
        self.assertEqual(intent['rule_version'], rules.RULE_VERSION)

    def test_current_mcp_metadata_without_spot_minimums_is_rejected(self):
        with self.assertRaises(ValueError):
            self.order(market={key: value for key, value in MARKET.items()
                               if key not in ('minQty', 'minNotional')} |
                       {'otc_min_notional': 90000})

    def test_denied_market_and_deadline_boundaries(self):
        with self.assertRaises(rules.PolicyViolation):
            self.order(symbol='FET/USDT')
        for timestamp in (rules.START_AT - 1, competition.END_AT, rules.END_AT):
            with self.assertRaises(ValueError):
                self.order(now=timestamp)

    def test_precision_minimum_funds_and_exposure(self):
        for change in ({'quantity': '0.000000001'}, {'quantity': '0.000001'},
                       {'price': '22000000000.1'}, {'available_tmn': '100000'},
                       {'exposure_limit_tmn': '100000'},
                       {'exposure_limit_tmn': '5000001'},
                       {'market': {**MARKET, 'is_spot': False}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.order(**change)
        with self.assertRaises(ValueError):
            self.order(side='SELL', available_base='0.000001')


if __name__ == '__main__':
    unittest.main()

import unittest
from unittest.mock import patch
from engine import demo_candles
from server import checked_bars, fetch_bars, normalize_exchange_bars


class CandleTests(unittest.TestCase):
    def test_ignored_resolution_resampled_without_partial_buckets(self):
        raw = [dict(t=i * 60, o=100+i, h=103+i, l=99+i, c=101+i, v=2) for i in range(34)]
        bars = normalize_exchange_bars(raw, 99999)
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0], dict(t=0, o=100, h=117, l=99, c=115, v=30))
        raw.pop(3)
        self.assertEqual(len(normalize_exchange_bars(raw, 99999)), 1)

    def test_csv_validation_rejects_duplicate_times_and_bad_prices(self):
        for field, value in [('t', 0), ('c', float('inf')), ('l', -1), ('h', 1), ('v', -1)]:
            bars = demo_candles(80)
            bars[-1][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                checked_bars(bars)

    def test_unclosed_exchange_candles_excluded(self):
        from io import BytesIO
        import json
        now = 100000
        data = dict(s='ok', t=[now - (81 - i) * 900 for i in range(82)])
        for field, value in dict(o=100, h=105, l=95, c=102, v=50).items():
            data[field] = [value] * 82
        with patch('server.time.time', return_value=now), patch('server.urlopen', return_value=BytesIO(json.dumps(data).encode())):
            bars = fetch_bars('BTCTMN', 100)
        self.assertEqual(len(bars), 81)
        self.assertTrue(all(bar['t'] + 900 <= now for bar in bars))


if __name__ == '__main__':
    unittest.main()

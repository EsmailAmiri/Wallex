import tempfile
import unittest
from pathlib import Path

import challenge_rules as rules
import competition
from spot_order_journal import SpotOrderJournal
from tests.test_live_spot_policy import MARKET


class Broker:
    def __init__(self):
        self.submissions = []
        self.reply = None

    def submit_limit(self, payload):
        self.submissions.append(payload)
        self.reply = dict(clientOrderId=payload['client_id'], symbol=payload['symbol'],
                          side=payload['side'], type='LIMIT', status='NEW', executedQty='0', fills=[])
        raise TimeoutError('simulated acceptance followed by lost response')

    def lookup(self, client_id):
        return self.reply


class JournalTests(unittest.TestCase):
    def test_risk_and_account_binding_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'orders.sqlite'
            journal = SpotOrderJournal(path, Broker())
            journal.bind_account('test-account')
            self.assertTrue(journal.risk_halted('4700000', 5))
            restarted = SpotOrderJournal(path, Broker())
            self.assertTrue(restarted.risk_halted('5100000', 5))
            with self.assertRaises(ValueError):
                restarted.bind_account('different-account')

    def test_cancel_must_reconcile_and_fill_records_must_match(self):
        inputs = dict(symbol='BTCTMN', side='BUY', price='22000000000', quantity='0.00001',
                      market=MARKET, available_tmn='5000000', available_base='0.1',
                      exposure_limit_tmn='250000', now=rules.START_AT + 3600)
        with tempfile.TemporaryDirectory() as directory:
            broker = Broker()
            journal = SpotOrderJournal(Path(directory) / 'orders.sqlite', broker)
            client_id = journal.prepare(**inputs)
            journal.submit(client_id, **inputs)
            def cancel(order_id):
                broker.reply = {**broker.reply, 'status': 'CANCELED'}
            broker.cancel = cancel
            self.assertEqual(journal.cancel(client_id)['confirmed']['status'], 'CANCELED')
            broker.reply = {**broker.reply, 'status': 'NEW'}
            self.assertEqual(journal.reconcile(client_id)['confirmed']['status'], 'CANCELED')

    def test_uncertain_submit_survives_restart_and_never_duplicates(self):
        inputs = dict(symbol='BTCTMN', side='BUY', price='22000000000', quantity='0.00001',
                      market=MARKET, available_tmn='5000000', available_base='0.1',
                      exposure_limit_tmn='250000', now=rules.START_AT + 3600)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'orders.sqlite'
            broker = Broker()
            journal = SpotOrderJournal(path, broker)
            client_id = journal.prepare(**inputs)
            self.assertEqual(journal.get(client_id)['state'], 'prepared')
            self.assertEqual(journal.submit(client_id, **inputs)['state'], 'uncertain')
            restarted = SpotOrderJournal(path, broker)
            result = restarted.submit(client_id, **inputs)
            self.assertEqual(len(broker.submissions), 1)
            self.assertEqual(result['confirmed']['executedQty'], '0')
            self.assertEqual(result['confirmed']['status'], 'NEW')
            broker.reply = {**broker.reply, 'clientOrderId': 'wrong'}
            self.assertEqual(restarted.reconcile(client_id)['confirmed']['status'], 'NEW')
            fresh_id = restarted.prepare(**inputs)
            with self.assertRaises(ValueError):
                restarted.submit(fresh_id, **{**inputs, 'now': competition.END_AT})
            self.assertEqual(len(broker.submissions), 1)


if __name__ == '__main__':
    unittest.main()

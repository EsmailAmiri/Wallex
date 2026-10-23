"""Persistent order lifecycle core. No exchange transport or runtime enablement.

A broker used here must expose submit_limit(payload) and lookup(client_id).
The deployed dashboard does not instantiate this class.
"""
import json
import sqlite3
import uuid
import time
import hashlib
from contextlib import closing
from decimal import Decimal

from live_spot_policy import validate_limit_order


class SpotOrderJournal:
    def __init__(self, path, broker):
        self.path, self.broker = str(path), broker
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS real_order_intents (
                client_id TEXT PRIMARY KEY, intent TEXT NOT NULL,
                state TEXT NOT NULL, confirmed TEXT)''')
            db.execute('CREATE TABLE IF NOT EXISTS order_created (client_id TEXT PRIMARY KEY, created_at REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS live_risk (id INTEGER PRIMARY KEY, peak TEXT, halted INTEGER)')
            db.execute("INSERT OR IGNORE INTO live_risk VALUES (1,'5000000',0)")
            db.execute('CREATE TABLE IF NOT EXISTS account_binding (id INTEGER PRIMARY KEY, digest TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS decision_audit (id INTEGER PRIMARY KEY, payload TEXT NOT NULL, previous_hash TEXT NOT NULL, digest TEXT NOT NULL)')

    def audit(self, payload):
        encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT digest FROM decision_audit ORDER BY id DESC LIMIT 1').fetchone()
            previous = previous[0] if previous else '0' * 64
            digest = hashlib.sha256((previous + encoded).encode()).hexdigest()
            cursor = db.execute('INSERT INTO decision_audit(payload,previous_hash,digest) VALUES (?,?,?)', (encoded, previous, digest))
        return cursor.lastrowid

    def audit_records(self):
        previous, records = '0' * 64, []
        with closing(sqlite3.connect(self.path)) as db, db:
            for identity, payload, prev, digest in db.execute('SELECT * FROM decision_audit ORDER BY id'):
                if prev != previous or hashlib.sha256((prev + payload).encode()).hexdigest() != digest:
                    raise ValueError('Decision audit chain is inconsistent.')
                records.append(dict(id=identity, payload=json.loads(payload), digest=digest))
                previous = digest
        return records

    def bind_account(self, digest):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT OR IGNORE INTO account_binding VALUES (1,?)', (digest,))
            if db.execute('SELECT digest FROM account_binding WHERE id=1').fetchone()[0] != digest:
                raise ValueError('Real order journal belongs to a different credit account or key.')

    def risk_halted(self, equity, maximum_drawdown):
        equity = Decimal(str(equity))
        if not equity.is_finite() or equity < 0:
            raise ValueError('Invalid exchange equity.')
        with closing(sqlite3.connect(self.path)) as db, db:
            peak, halted = db.execute('SELECT peak,halted FROM live_risk WHERE id=1').fetchone()
            peak = max(Decimal(peak), equity)
            halted = bool(halted) or (1 - equity / peak) * 100 >= Decimal(str(maximum_drawdown))
            db.execute('UPDATE live_risk SET peak=?,halted=? WHERE id=1', (str(peak), int(halted)))
        return halted

    def orders(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            ids = [row[0] for row in db.execute('SELECT client_id FROM real_order_intents ORDER BY rowid')]
        return [self.get(client_id) for client_id in ids]

    def get(self, client_id):
        with closing(sqlite3.connect(self.path)) as db, db:
            row = db.execute('SELECT intent,state,confirmed FROM real_order_intents WHERE client_id=?',
                             (client_id,)).fetchone()
        if row is None:
            raise ValueError('Unknown locally persisted order.')
        return {'client_id': client_id, 'intent': json.loads(row[0]), 'state': row[1],
                'confirmed': json.loads(row[2]) if row[2] else None}

    def _confirm(self, client_id, reply):
        intent = self.get(client_id)['intent']
        if not isinstance(reply, dict):
            raise ValueError('Unverified order response.')
        for field, expected in (('clientOrderId', client_id), ('symbol', intent['symbol']),
                                ('side', intent['side']), ('type', 'LIMIT')):
            if reply.get(field) != expected:
                raise ValueError('Order response identity mismatch.')
        if reply.get('status') not in ('NEW', 'PARTIALLY_FILLED', 'FILLED', 'CANCELED', 'REJECTED', 'EXPIRED'):
            raise ValueError('Unknown exchange order status.')
        executed = Decimal(str(reply.get('executedQty')))
        if not executed.is_finite() or not 0 <= executed <= Decimal(intent['quantity']):
            raise ValueError('Invalid exchange executed quantity.')
        if reply['status'] == 'FILLED' and executed != Decimal(intent['quantity']):
            raise ValueError('Filled status contradicts executed quantity.')
        if not isinstance(reply.get('fills'), list):
            raise ValueError('Missing exchange fill records.')
        previous = self.get(client_id)['confirmed']
        if previous and executed < Decimal(previous['executedQty']):
            raise ValueError('Executed quantity cannot decrease.')
        if previous and previous['status'] in ('FILLED', 'CANCELED', 'REJECTED', 'EXPIRED') and reply['status'] != previous['status']:
            raise ValueError('Terminal order status cannot regress.')
        fill_qty = Decimal('0')
        for fill in reply['fills']:
            if not isinstance(fill, dict):
                raise ValueError('Invalid fill record.')
            qty, price, fee = (Decimal(str(fill.get(key))) for key in ('quantity', 'price', 'fee'))
            if not all(x.is_finite() for x in (qty, price, fee)) or qty <= 0 or price <= 0 or fee < 0 or not isinstance(fill.get('feeAsset'), str):
                raise ValueError('Invalid confirmed fill accounting.')
            if fill['feeAsset'] not in ('TMN', intent['symbol'][:-3]):
                raise ValueError('Unexpected fill fee asset requires reconciliation.')
            if (intent['side'] == 'BUY' and price > Decimal(intent['price'])) or (intent['side'] == 'SELL' and price < Decimal(intent['price'])):
                raise ValueError('Fill violates limit price.')
            fill_qty += qty
        if fill_qty != executed:
            raise ValueError('Executed quantity does not match confirmed fills.')
        # Persist only explicit order fields, never arbitrary remote text or account data.
        confirmed = {key: reply[key] for key in ('clientOrderId', 'symbol', 'side', 'type',
                     'status', 'executedQty', 'fills')}
        confirmed['fills'] = [{key: fill[key] for key in
            ('price', 'quantity', 'sum', 'fee', 'feeAsset', 'timestamp') if key in fill}
            for fill in reply['fills'] if isinstance(fill, dict)]
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE real_order_intents SET state=?,confirmed=? WHERE client_id=?',
                       ('confirmed', json.dumps(confirmed), client_id))
        return self.get(client_id)

    def prepare(self, **policy_inputs):
        intent = validate_limit_order(**policy_inputs)
        client_id = 'madar-' + uuid.uuid4().hex
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT INTO real_order_intents VALUES (?,?,?,NULL)',
                       (client_id, json.dumps(intent), 'prepared'))
            db.execute('INSERT INTO order_created VALUES (?,?)', (client_id, time.time()))
        return client_id

    def age(self, client_id):
        with closing(sqlite3.connect(self.path)) as db, db:
            row = db.execute('SELECT created_at FROM order_created WHERE client_id=?', (client_id,)).fetchone()
        return time.time() - row[0] if row else float('inf')

    def submit(self, client_id, **fresh_policy_inputs):
        fresh_intent = validate_limit_order(**fresh_policy_inputs)
        if fresh_intent != self.get(client_id)['intent']:
            raise ValueError('Fresh preflight does not match the persisted intent.')
        # Claim and commit before the network call. Repeated calls never resubmit.
        with closing(sqlite3.connect(self.path)) as db, db:
            claimed = db.execute("UPDATE real_order_intents SET state='uncertain' WHERE client_id=? AND state='prepared'",
                                 (client_id,)).rowcount
        if not claimed:
            return self.reconcile(client_id)
        intent = self.get(client_id)['intent']
        payload = {key: intent[key] for key in ('symbol', 'side', 'type', 'price', 'quantity')}
        payload['client_id'] = client_id
        try:
            return self._confirm(client_id, self.broker.submit_limit(payload))
        except Exception:
            # A failed read or timeout says nothing about whether submission succeeded.
            return self.get(client_id)

    def reconcile(self, client_id):
        self.get(client_id)  # Do not query arbitrary, unjournaled exchange orders.
        try:
            return self._confirm(client_id, self.broker.lookup(client_id))
        except Exception:
            return self.get(client_id)

    def cancel(self, client_id):
        order = self.get(client_id)
        if order['state'] == 'prepared':
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("UPDATE real_order_intents SET state='local_canceled' WHERE client_id=? AND state='prepared'", (client_id,))
            return self.get(client_id)
        try:
            self.broker.cancel(client_id)
        except Exception:
            pass  # Cancellation acknowledgment is not final exchange status.
        return self.reconcile(client_id)

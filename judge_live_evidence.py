"""Offline, read-only verification of live decision evidence and realized TMN."""
import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import challenge_rules as rules


def evaluate(path):
    previous, records, orders = '0' * 64, [], []
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        for identity, payload, prev, digest in db.execute('SELECT * FROM decision_audit ORDER BY id'):
            if prev != previous or hashlib.sha256((prev + payload).encode()).hexdigest() != digest:
                raise ValueError('Decision audit hash chain failed.')
            records.append(dict(id=identity, payload=json.loads(payload), digest=digest))
            previous = digest
        for client_id, intent, state, confirmed in db.execute('SELECT * FROM real_order_intents ORDER BY rowid'):
            orders.append(dict(client_id=client_id, intent=json.loads(intent), state=state,
                               confirmed=json.loads(confirmed) if confirmed else None))
    quantities, costs, realized, volume, fills_count = {}, {}, Decimal(0), Decimal(0), 0
    unresolved = 0
    for order in orders:
        confirmed = order['confirmed']
        if not confirmed:
            unresolved += order['state'] != 'local_canceled'
            continue
        unresolved += confirmed['status'] in ('NEW', 'PARTIALLY_FILLED')
        symbol = order['intent']['symbol']
        quantities.setdefault(symbol, Decimal(0))
        costs.setdefault(symbol, Decimal(0))
        for fill in confirmed['fills']:
            qty, price, fee = (Decimal(str(fill[k])) for k in ('quantity', 'price', 'fee'))
            if fill['feeAsset'] not in ('TMN', symbol[:-3]):
                raise ValueError('Unsupported fee asset.')
            if not all(x.is_finite() for x in (qty, price, fee)) or qty <= 0 or price <= 0 or fee < 0:
                raise ValueError('Invalid confirmed fill.')
            volume += qty * price
            fills_count += 1
            if confirmed['side'] == 'BUY':
                quantities[symbol] += qty - (fee if fill['feeAsset'] == symbol[:-3] else 0)
                costs[symbol] += qty * price + (fee if fill['feeAsset'] == 'TMN' else 0)
            else:
                sold = qty + (fee if fill['feeAsset'] == symbol[:-3] else 0)
                if not 0 < sold <= quantities[symbol]:
                    raise ValueError('Sale exceeds journal-confirmed holdings.')
                basis = costs[symbol] * sold / quantities[symbol]
                realized += qty * price - (fee if fill['feeAsset'] == 'TMN' else 0) - basis
                costs[symbol] -= basis
                quantities[symbol] -= sold
    decision_ids = {r['id'] for r in records if r['payload'].get('kind') == 'decision'}
    links = {r['payload']['client_order_id'] for r in records if r['payload'].get('kind') == 'order_intent_link'
             and r['payload'].get('decision_id') in decision_ids}
    unlinked = sum(order['client_id'] not in links for order in orders)
    settled = bool(fills_count) and unresolved == 0 and all(q == 0 for q in quantities.values())
    return dict(execution='exchange_confirmed_records_only', audit_chain_valid=True, audit_records=len(records),
                order_count=len(orders), unlinked_orders=unlinked, unresolved_orders=unresolved,
                confirmed_fill_count=fills_count, confirmed_volume_tmn=str(volume), realized_profit_tmn=str(realized),
                fully_realized_tmn=settled, final_return_percent=str(realized / rules.CREDIT_TMN * 100) if settled else None,
                remaining_quantities={k: str(v) for k, v in quantities.items()}, rule_sha256=rules.RULE_HASH)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal', default='data/live-orders.sqlite3')
    args = parser.parse_args()
    try:
        print(json.dumps(evaluate(args.journal)))
    except Exception:
        print(json.dumps({'status': 'evidence_unverified', 'final_return_percent': None}))
        raise SystemExit(1)

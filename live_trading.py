"""Opt-in live spot agent. Dashboard remains paper/shadow.

Run without --execute for read-only preflight. Never prints raw account replies.
"""
import argparse
import asyncio
import hashlib
import json
import time
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

import challenge_rules as rules
import competition
from engine import validate, decide
from live_spot_policy import amount, validate_limit_order
from mcp_link import MCPBridge, OFFICIAL_URL
from spot_order_journal import SpotOrderJournal
from wallex_settings import settings


class OfficialSpotBroker:
    """Four explicit spot operations; no arbitrary tool execution or dashboard route."""
    ALLOWED_TOOLS = frozenset({'wallex_spot_post_account_orders', 'wallex_spot_get_orders_client',
                               'wallex_spot_delete_orders_client', 'wallex_spot_get_open_order'})
    def __init__(self, bridge, authorize, expected_key_sha256=None):
        self.bridge, self.authorize = bridge, authorize
        self.expected_key_sha256 = expected_key_sha256

    async def _request(self, tool, payload):
        if tool not in self.ALLOWED_TOOLS:
            raise ValueError('This MCP operation is outside the spot order adapter allowlist.')
        async with self.bridge.call_lock:
            if tool not in self.bridge.remote_tools:
                raise ValueError('Required official spot tool is unavailable.')
            reply = await self.bridge.session.call_tool(tool, payload)
            if reply.isError:
                raise ValueError('Official spot operation failed; reconcile before retry.')
            data = reply.structuredContent
            if data is None:
                data = json.loads(''.join(item.text for item in reply.content if item.type == 'text'))
            if not isinstance(data, dict) or data.get('success') is False:
                raise ValueError('Unverified official spot response.')
            return data.get('result', data)

    def _run(self, tool, payload):
        config = settings()
        if config['WALLEX_MCP_URL'] != OFFICIAL_URL or not config['WALLEX_API_KEY']:
            raise ValueError('Live execution requires the exact official endpoint and dedicated key.')
        if hashlib.sha256(config['WALLEX_API_KEY'].encode()).hexdigest() != self.expected_key_sha256:
            raise ValueError('Configured credit key changed; original account reconciliation is required.')
        self.bridge.connect()
        if self.bridge.info['provider'] != 'wallex-official':
            raise ValueError('Live execution requires official MCP.')
        task = asyncio.run_coroutine_threadsafe(self._request(tool, payload), self.bridge.loop)
        try:
            return task.result(timeout=40)
        except Exception:
            task.cancel()
            raise ValueError('Spot response uncertain; reconcile by the persisted client ID.') from None

    def submit_limit(self, payload):
        self.authorize(payload)  # Fresh account, funds, policy and market checks at submission.
        return self._run('wallex_spot_post_account_orders', payload)

    def lookup(self, client_id):
        return self._run('wallex_spot_get_orders_client', {'client_id': client_id})

    def cancel(self, client_id):
        return self._run('wallex_spot_delete_orders_client', {'client_id': client_id})

    def open_orders(self, symbol):
        rules.assert_market_allowed(symbol)
        result = self._run('wallex_spot_get_open_order', {'symbol': symbol, 'page': 1, 'per_page': 100})
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and all(isinstance(v, list) for v in result.values()):
            return [order for group in result.values() for order in group]
        raise ValueError('Unknown open-order schema; self-trade check unavailable.')


class LiveAgent:
    def __init__(self, config, bridge, journal_path):
        self.config, self.bridge = config, bridge
        self.symbol = rules.assert_market_allowed(config['symbol'])
        self.broker = OfficialSpotBroker(bridge, self.authorize, config.get('dedicated_key_sha256'))
        self.journal = SpotOrderJournal(journal_path, self.broker)
        self.journal.bind_account(hashlib.sha256((str(config.get('credit_client_id')) + ':' +
            str(config.get('dedicated_key_sha256'))).encode()).hexdigest())

    def scope(self):
        competition.assert_active()
        cfg = self.config
        key = settings()['WALLEX_API_KEY']
        if cfg.get('enabled') is not True or cfg.get('rule_sha256') != rules.RULE_HASH:
            raise ValueError('Live configuration is disabled or policy version differs.')
        if not key or cfg.get('dedicated_key_sha256') != hashlib.sha256(key.encode()).hexdigest():
            raise ValueError('Dedicated credit-key evidence does not match the configured key.')
        if cfg.get('scope_evidence_verified') is not True or not cfg.get('scope_evidence_reference'):
            raise ValueError('Independent dedicated credit-account scope evidence is required.')
        accounts = self.bridge.call('get_credit_sub_accounts')
        matches = [a for a in accounts if a.get('clientId') == cfg.get('credit_client_id')
                   and a.get('isActive') is True and a.get('isSuspend') is False and a.get('type') == 'prop']
        if len(matches) != 1:
            raise ValueError('Verified credit sub-account is not active.')
        package = self.bridge.call('get_credit_package')
        # Accept only the documented package record. Unknown schemas fail closed.
        if (package.get('client_id') != cfg['credit_client_id']
                or Decimal(str(package.get('loan'))) != rules.CREDIT_TMN
                or Decimal(str(package.get('assurance'))) != 0
                or package.get('package_title') != rules.PLAN_NAME):
            raise ValueError('Active credit package does not match the fixed challenge plan.')
        if (int(datetime.fromisoformat(package['created_at'].replace('Z', '+00:00')).timestamp()) != rules.START_AT
                or int(datetime.fromisoformat(package['expire_at'].replace('Z', '+00:00')).timestamp()) != rules.END_AT):
            raise ValueError('Credit package dates do not match the fixed challenge plan.')
        return self.bridge.call('get_credit_balances')

    def inputs(self, side, price, quantity):
        balances = self.scope()
        market = self.bridge.call('get_spot_markets')['symbols'].get(self.symbol)
        base = self.symbol[:-3]
        fee = Decimal(str(self.config['verified_fee_rate']))
        if not self.config.get('fee_evidence_reference') or not fee.is_finite() or not 0 <= fee <= Decimal('0.02'):
            raise ValueError('A verified exchange transaction fee rate is required.')
        cap = min(amount(self.config['max_order_tmn'], 'order cap'),
                  amount(self.config['max_exposure_tmn'], 'exposure cap'))
        if cap > rules.CREDIT_TMN:
            raise ValueError('Credit exposure cannot exceed fixed principal.')
        available = Decimal(str(balances['TMN']['available']))
        base_available = Decimal(str(balances.get(base, {}).get('available', '0')))
        if side == 'BUY':
            allocation = validate({**self.config.get('strategy', {}), 'symbol': self.symbol})['allocation']
            cap = min(cap, Decimal(rules.CREDIT_TMN) * Decimal(str(allocation)) / 100)
            # Do not silently adopt existing or untracked account positions.
            if any(Decimal(str(row.get('total', '0'))) != 0 for asset, row in balances.items() if asset != 'TMN'):
                raise ValueError('Existing account exposure must be reconciled before entry.')
            available = available / (1 + fee)
            cap = cap / (1 + fee)
        return dict(symbol=self.symbol, side=side, price=price, quantity=quantity,
                    market=market, available_tmn=str(available), available_base=str(base_available),
                    exposure_limit_tmn=str(cap))

    def authorize(self, payload):
        if set(payload) != {'client_id', 'symbol', 'side', 'type', 'price', 'quantity'}:
            raise ValueError('Unexpected live order fields.')
        if payload['symbol'] != self.symbol or payload['type'] != 'LIMIT':
            raise ValueError('Live order does not match configured spot policy.')
        order = self.journal.get(payload['client_id'])
        if order['state'] != 'uncertain':
            raise ValueError('Order intent must be committed before submission.')
        fresh = validate_limit_order(**self.inputs(payload['side'], payload['price'], payload['quantity']))
        if fresh != order['intent']:
            raise ValueError('Fresh policy differs from committed order intent.')
        # Refuse any pre-existing order, including manually placed account orders.
        if self.broker.open_orders(self.symbol):
            raise ValueError('Existing exchange orders must be reconciled before a fresh order.')

    def cycle(self, execute=False):
        # Always reconcile earlier uncertain/open orders, even outside entry deadlines.
        for order in self.journal.orders():
            if order['state'] == 'prepared':
                self.journal.cancel(order['client_id'])
                return {'status': 'stale_intent_canceled', 'new_order_submitted': False}
            if order['state'] == 'uncertain' or (order['confirmed'] and order['confirmed']['status'] in ('NEW', 'PARTIALLY_FILLED')):
                current = self.journal.reconcile(order['client_id'])
                if execute and current['confirmed'] and current['confirmed']['status'] in ('NEW', 'PARTIALLY_FILLED'):
                    if time.time() >= min(rules.END_AT, competition.END_AT) or self.journal.age(order['client_id']) >= self.config.get('order_ttl_seconds', 60):
                        self.journal.cancel(order['client_id'])
                return {'status': 'reconciled', 'new_order_submitted': False}
        balances = self.scope()
        bars = self.bridge.call('get_market_candles', {'symbol': self.symbol, 'count': 160})['candles']
        now = time.time()
        if not bars or not 0 <= now - (bars[-1]['t'] + 900) <= 900:
            raise ValueError('Closed-bar market data is stale or in the future.')
        strategy = validate({**self.config.get('strategy', {}), 'symbol': self.symbol, 'source': 'wallex_mcp',
                             'fee': float(Decimal(str(self.config['verified_fee_rate'])) * 100)})
        close_window = now >= min(rules.END_AT, competition.END_AT) - 600
        quantity_held, cost, last_exit = Decimal('0'), Decimal('0'), 0
        for order in self.journal.orders():
            confirmed = order['confirmed']
            if not confirmed:
                continue
            for fill in confirmed['fills']:
                qty, px, fee = (Decimal(str(fill[key])) for key in ('quantity', 'price', 'fee'))
                if confirmed['side'] == 'BUY':
                    quantity_held += qty - (fee if fill['feeAsset'] == self.symbol[:-3] else 0)
                    cost += qty * px + (fee if fill['feeAsset'] == 'TMN' else 0)
                else:
                    sold = qty + (fee if fill['feeAsset'] == self.symbol[:-3] else 0)
                    cost *= max(Decimal('0'), quantity_held - sold) / quantity_held if quantity_held else 0
                    quantity_held -= sold
                    last_exit = max(last_exit, now - self.journal.age(order['client_id']))
        if quantity_held < 0:
            raise ValueError('Confirmed position accounting is inconsistent.')
        equity = Decimal(str(balances['TMN']['total'])) + Decimal(str(balances.get(self.symbol[:-3], {}).get('total', '0'))) * Decimal(str(bars[-1]['c']))
        halted = self.journal.risk_halted(equity, strategy['max_drawdown'])
        risk_exit = halted and bool(quantity_held)
        if quantity_held and cost:
            change = (Decimal(str(bars[-1]['c'])) * quantity_held / cost - 1) * 100
            risk_exit = risk_exit or change <= -Decimal(str(strategy['stop_loss'])) or change >= Decimal(str(strategy['take_profit']))
        # Forced exits bypass the LLM; holding also prevents a fresh entry vote.
        if halted and not quantity_held:
            return {'status': 'risk_halted', 'new_order_submitted': False}
        in_cooldown = bool(last_exit and now - last_exit < 1800)
        forced_exit = (risk_exit or close_window) and bool(quantity_held)
        decision = ({'action': 'SELL', 'reason': 'risk_exit' if risk_exit else 'pre_deadline_tmn_exit'} if forced_exit
            else {'action': 'HOLD', 'reason': 'pre_deadline_no_new_entries'} if close_window
            else decide(bars, strategy, entry_allowed=not quantity_held and not in_cooldown,
                        holding_position=bool(quantity_held), in_cooldown=in_cooldown))
        code_hashes = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                       for name in ('engine.py', 'live_trading.py', 'live_spot_policy.py', 'spot_order_journal.py')}
        audit_id = self.journal.audit(dict(kind='decision', observed_at=now, execution='live_candidate' if execute else 'preflight',
            symbol=self.symbol, bars=bars, strategy=strategy, decision=decision, code_sha256=code_hashes,
            rule_sha256=rules.RULE_HASH, forced_exit=forced_exit, in_cooldown=in_cooldown))
        side = decision['action']
        if side == 'HOLD' or (side == 'SELL' and not quantity_held):
            return {'status': 'hold', 'new_order_submitted': False}
        market = self.bridge.call('get_spot_markets')['symbols'][self.symbol]
        precision = market['amount_precision']
        price = Decimal(str(market['price'])).quantize(Decimal(1).scaleb(-market['price_precision']), rounding=ROUND_DOWN)
        budget = min(amount(self.config['max_order_tmn'], 'order cap'), amount(self.config['max_exposure_tmn'], 'exposure cap'),
                     Decimal(rules.CREDIT_TMN) * Decimal(str(strategy['allocation'])) / 100)
        fee = Decimal(str(self.config['verified_fee_rate']))
        quantity = (budget / (price * (1 + fee)) if side == 'BUY' else quantity_held).quantize(Decimal(1).scaleb(-precision), rounding=ROUND_DOWN)
        inputs = self.inputs(side, str(price), str(quantity))
        validate_limit_order(**inputs)
        if not execute:
            return {'status': 'preflight_passed', 'new_order_submitted': False}
        client_id = self.journal.prepare(**inputs)
        self.journal.audit(dict(kind='order_intent_link', decision_id=audit_id, client_order_id=client_id))
        result = self.journal.submit(client_id, **self.inputs(side, str(price), str(quantity)))
        self.journal.audit(dict(kind='order_result', decision_id=audit_id, client_order_id=client_id,
                               state=result['state'], confirmed=result['confirmed']))
        return {'status': result['state'], 'client_order_id': client_id,
                'exchange_status': (result['confirmed'] or {}).get('status'),
                'execution': 'real_order_attempt'}


@contextmanager
def process_lock(path):
    """Serialize user-run processes sharing this real-order ledger."""
    path = Path(str(path) + '.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        handle.seek(0)
        handle.write(b'0')
        handle.flush()
        handle.seek(0)
        if __import__('os').name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if __import__('os').name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='data/live-config.json')
    parser.add_argument('--journal', default='data/live-orders.sqlite3')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--loop', action='store_true', help='Repeat every 15 seconds until stopped or blocked.')
    args = parser.parse_args()
    bridge = MCPBridge()
    try:
        cfg = json.loads(Path(args.config).read_text(encoding='utf-8'))
        with process_lock(args.journal):
            agent = LiveAgent(cfg, bridge, args.journal)
            while True:
                print(json.dumps(agent.cycle(args.execute)), flush=True)
                if not args.loop:
                    break
                time.sleep(15)
    except Exception:
        print(json.dumps({'status': 'blocked', 'new_order_submitted': False,
                          'reason': 'Live prerequisites or reconciliation need local review.'}))
        return 1
    finally:
        bridge.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

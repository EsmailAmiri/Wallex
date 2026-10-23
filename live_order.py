"""Live spot-order adapter for Wallex MCP credit account.

Implements the live-order pathway per AGENTS.md:
- Policy enforcement before every order
- Unique client_order_id with intent persistence
- Reconciliation on timeout/error
- Separate recording from paper/shadow
"""
import json
import math
import sqlite3
import time
import uuid
from pathlib import Path

import challenge_rules as rules

# Default precision/limits per supported symbol (updated by refresh_markets)
MARKET_DEFAULTS = {
    'BTCTMN':  dict(base_prec=6, quote_prec=0, min_base=0.000001, min_notional=50_000),
    'ETHTMN':  dict(base_prec=5, quote_prec=0, min_base=0.00001,  min_notional=50_000),
    'USDTTMN': dict(base_prec=2, quote_prec=0, min_base=0.5,      min_notional=50_000),
    'SOLTMN':  dict(base_prec=4, quote_prec=0, min_base=0.001,    min_notional=50_000),
    'XRPTMN':  dict(base_prec=2, quote_prec=0, min_base=1.0,      min_notional=50_000),
    'PEPETMN': dict(base_prec=0, quote_prec=0, min_base=1000,     min_notional=50_000),
}


class OrderError(ValueError):
    """Policy or adapter error – never triggers a retry."""
    pass


class LiveOrderAdapter:
    """Place, track and reconcile real spot orders via Wallex MCP."""

    def __init__(self, bridge, db_path):
        self.bridge = bridge
        self.db_path = Path(db_path)
        self.markets = dict(MARKET_DEFAULTS)
        self._init_db()

    # ── database ────────────────────────────────────────────────────

    def _init_db(self):
        with sqlite3.connect(self.db_path) as con:
            con.execute('''CREATE TABLE IF NOT EXISTS live_orders (
                client_id      TEXT PRIMARY KEY,
                symbol         TEXT NOT NULL,
                side           TEXT NOT NULL,
                order_type     TEXT NOT NULL,
                quantity       TEXT NOT NULL,
                price          TEXT,
                budget_tmn     REAL,
                status         TEXT DEFAULT 'intent',
                exchange_resp  TEXT,
                fill_qty       TEXT,
                fill_price     TEXT,
                fee            TEXT,
                created_at     INTEGER,
                submitted_at   INTEGER,
                confirmed_at   INTEGER,
                error          TEXT,
                rule_version   TEXT
            )''')

    def _persist_intent(self, client_id, symbol, side, order_type, quantity,
                        price, budget_tmn):
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                '''INSERT INTO live_orders
                   (client_id,symbol,side,order_type,quantity,price,
                    budget_tmn,status,created_at,rule_version)
                   VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (client_id, symbol, side, order_type, str(quantity),
                 str(price) if price else None, budget_tmn,
                 'intent', int(time.time()), rules.RULE_VERSION))

    def _update(self, client_id, status, **kw):
        with sqlite3.connect(self.db_path) as con:
            cols = ['status=?']
            vals = [status]
            for k, v in kw.items():
                cols.append(f'{k}=?')
                vals.append(v)
            vals.append(client_id)
            con.execute(f'UPDATE live_orders SET {",".join(cols)} '
                        f'WHERE client_id=?', vals)

    # ── client order id ─────────────────────────────────────────────

    @staticmethod
    def _make_client_id(symbol, side):
        ts = int(time.time() * 1000)
        short = uuid.uuid4().hex[:8]
        return f'madar-{symbol}-{side}-{ts}-{short}'[:36]

    # ── policy checks ───────────────────────────────────────────────

    def _pre_checks(self, symbol, side, budget_tmn, config):
        rules.assert_market_allowed(symbol)
        rules.assert_plan_active()
        rules.assert_capability('spot')

        if side not in ('BUY', 'SELL'):
            raise OrderError(f'Invalid side: {side}')

        max_exposure = config['capital'] * config['allocation'] / 100
        if budget_tmn > max_exposure * 1.05:
            raise OrderError(
                f'Budget {budget_tmn:.0f} exceeds allocation limit '
                f'{max_exposure:.0f} TMN')

        if not self.bridge.info.get('connected'):
            raise OrderError('MCP not connected')
        if self.bridge.info.get('provider') != 'wallex-official':
            raise OrderError('Live orders require official Wallex MCP')

    # ── quantity helpers ────────────────────────────────────────────

    def _round_qty(self, symbol, qty):
        info = self.markets.get(symbol, MARKET_DEFAULTS.get(symbol))
        if not info:
            raise OrderError(f'No market info for {symbol}')
        prec = info['base_prec']
        factor = 10 ** prec
        rounded = math.floor(qty * factor) / factor
        if rounded < info['min_base']:
            raise OrderError(
                f'Quantity {rounded} below minimum {info["min_base"]} '
                f'for {symbol}')
        return rounded

    def _check_notional(self, symbol, qty, price):
        info = self.markets.get(symbol, MARKET_DEFAULTS.get(symbol))
        notional = qty * price
        if notional < info.get('min_notional', 50_000):
            raise OrderError(
                f'Notional {notional:.0f} TMN below minimum '
                f'{info["min_notional"]} for {symbol}')

    # ── balance ─────────────────────────────────────────────────────

    def get_balances(self):
        """Return {asset: {value, locked}} from exchange."""
        data = self.bridge.call('get_account_balances')
        result = data.get('result', data)
        raw = result.get('balances', {})
        out = {}
        for asset, info in raw.items():
            if isinstance(info, dict):
                val = float(info.get('value', 0))
                locked = float(info.get('locked', 0))
                if val > 0 or locked > 0:
                    out[asset] = dict(value=val, locked=locked,
                                      available=val - locked)
        return out

    def available_tmn(self):
        bals = self.get_balances()
        tmn = bals.get('TMN', {})
        return tmn.get('available', 0)

    def available_base(self, symbol):
        """Return available quantity of base asset for *symbol*."""
        # BTCTMN → BTC, USDTTMN → USDT, etc.
        base = symbol.replace('TMN', '').replace('USDT', '')
        if symbol.endswith('TMN'):
            base = symbol[:-3]
        bals = self.get_balances()
        info = bals.get(base, {})
        return info.get('available', 0)

    # ── market info ─────────────────────────────────────────────────

    def refresh_markets(self):
        """Fetch spot market specs from exchange (precision, limits)."""
        try:
            data = self.bridge.call('get_spot_markets')
            result = data.get('result', data)
            symbols = result.get('symbols', {})
            if isinstance(result.get('markets'), list):
                symbols = {m['symbol']: m for m in result['markets']
                           if isinstance(m, dict) and 'symbol' in m}
            for sym, details in symbols.items():
                try:
                    sym = rules.assert_market_allowed(sym)
                except rules.PolicyViolation:
                    continue
                if not isinstance(details, dict):
                    continue
                prec = int(details.get('baseAssetPrecision',
                           details.get('basePrecision', 8)))
                min_b = float(details.get('baseMinSize',
                              details.get('minQty', 0)) or 0)
                min_n = float(details.get('minNotional', 50_000) or 50_000)
                self.markets[sym] = dict(
                    base_prec=prec, quote_prec=0,
                    min_base=min_b or 10 ** -prec,
                    min_notional=min_n)
        except Exception:
            pass  # Keep defaults

    # ── order lifecycle ─────────────────────────────────────────────

    def place_order(self, symbol, side, price_approx, config):
        """Place a live LIMIT order (Wallex does not support MARKET orders).

        For BUY: limit price = price_approx * 1.005 (0.5% above for immediate fill).
        For SELL: limit price = price_approx * 0.995 (0.5% below for immediate fill).
        """
        symbol = rules.assert_market_allowed(symbol)

        if side == 'BUY':
            avail = self.available_tmn()
            budget = min(avail,
                         config['capital'] * config['allocation'] / 100)
            if budget < 10_000:
                raise OrderError(
                    f'Insufficient TMN: {avail:.0f} available, need >=10000')
            # Limit price slightly above market for immediate fill
            limit_price = round(price_approx * 1.005)
            raw_qty = budget / limit_price
            qty = self._round_qty(symbol, raw_qty)
            self._check_notional(symbol, qty, limit_price)
        else:  # SELL
            budget = 0
            qty_avail = self.available_base(symbol)
            if qty_avail <= 0:
                raise OrderError(f'No {symbol} base asset to sell')
            qty = self._round_qty(symbol, qty_avail)
            # Limit price slightly below market for immediate fill
            limit_price = round(price_approx * 0.995)
            self._check_notional(symbol, qty, limit_price)

        self._pre_checks(symbol, side, budget if side == 'BUY' else
                         qty * limit_price, config)

        client_id = self._make_client_id(symbol, side)
        self._persist_intent(client_id, symbol, side, 'LIMIT', qty,
                             limit_price, budget if side == 'BUY' else 0)

        args = dict(symbol=symbol, side=side, type='LIMIT',
                    quantity=str(qty), price=str(limit_price),
                    client_id=client_id)

        try:
            self._update(client_id, 'submitted',
                         submitted_at=int(time.time()))
            result = self.bridge.order_call('place_order', args)
            resp_str = json.dumps(result, ensure_ascii=False)[:2000]
            self._update(client_id, 'accepted',
                         exchange_resp=resp_str,
                         confirmed_at=int(time.time()))
            # Try to extract fill info from immediate response
            self._extract_fill(client_id, result)
            return dict(client_id=client_id, symbol=symbol, side=side,
                        quantity=str(qty), status='accepted', result=result)
        except Exception as exc:
            self._update(client_id, 'submit_error',
                         error=str(exc)[:500])
            # Attempt reconciliation by client_id
            try:
                recon = self.reconcile(client_id)
                return dict(client_id=client_id, symbol=symbol, side=side,
                            quantity=str(qty), status='reconciled',
                            result=recon)
            except Exception:
                pass
            return dict(client_id=client_id, symbol=symbol, side=side,
                        quantity=str(qty), status='error',
                        error=str(exc)[:300])

    def _extract_fill(self, client_id, data):
        """Try to parse fill info from order response."""
        order = data.get('result', data) if isinstance(data, dict) else {}
        if not isinstance(order, dict):
            return
        fill_qty = order.get('executedQty', order.get('filled_quantity'))
        fill_price = order.get('avgPrice', order.get('price'))
        fee = order.get('fee', order.get('commission'))
        status = order.get('status', '')
        updates = {}
        if fill_qty:
            updates['fill_qty'] = str(fill_qty)
        if fill_price:
            updates['fill_price'] = str(fill_price)
        if fee:
            updates['fee'] = str(fee)
        if updates:
            self._update(client_id, status or 'filled', **updates)

    def get_order_status(self, client_id):
        """Query order status from exchange by client_id."""
        result = self.bridge.order_call('get_order',
                                        {'client_id': client_id})
        if isinstance(result, dict):
            resp = json.dumps(result, ensure_ascii=False)[:2000]
            self._update(client_id, 'checked',
                         exchange_resp=resp,
                         confirmed_at=int(time.time()))
            self._extract_fill(client_id, result)
        return result

    def cancel_order(self, client_id):
        """Cancel an open order by client_id."""
        result = self.bridge.order_call('cancel_order',
                                        {'client_id': client_id})
        self._update(client_id, 'cancelled',
                     exchange_resp=json.dumps(
                         result, ensure_ascii=False)[:2000],
                     confirmed_at=int(time.time()))
        return result

    def reconcile(self, client_id):
        """Re-check order after a timeout or uncertain response."""
        return self.get_order_status(client_id)

    def get_open_orders(self, symbol=None):
        args = dict(symbol=symbol) if symbol else {}
        return self.bridge.order_call('open_orders', args)

    # ── local ledger queries ────────────────────────────────────────

    def all_orders(self):
        with sqlite3.connect(self.db_path) as con:
            con.row_factory = sqlite3.Row
            return [dict(r) for r in con.execute(
                'SELECT * FROM live_orders ORDER BY created_at DESC')]

    def filled_orders(self):
        with sqlite3.connect(self.db_path) as con:
            con.row_factory = sqlite3.Row
            return [dict(r) for r in con.execute(
                "SELECT * FROM live_orders "
                "WHERE fill_qty IS NOT NULL AND fill_qty != '0' "
                "ORDER BY confirmed_at DESC")]

    # ── verification (non-financial) ────────────────────────────────

    def verify(self):
        """Pre-deployment check: can we read balances and open orders?"""
        errors = []

        # 1. MCP connected to official?
        if not self.bridge.info.get('connected'):
            errors.append('MCP not connected')
        elif self.bridge.info.get('provider') != 'wallex-official':
            errors.append('Not connected to official Wallex MCP')

        # 2. Can read balances?
        try:
            bals = self.get_balances()
            tmn = bals.get('TMN', {}).get('available', 0)
            if tmn <= 0:
                errors.append(f'No TMN available (got {tmn})')
        except Exception as e:
            errors.append(f'Balance read failed: {e}')

        # 3. Can read open orders?
        try:
            self.bridge.order_call('open_orders', {})
        except Exception as e:
            errors.append(f'Open orders read failed: {e}')

        # 4. Order tool exists?
        tool = 'wallex_spot_post_account_orders'
        if tool not in self.bridge.remote_tools:
            errors.append(f'Order tool {tool} not on server')

        # 5. Plan active?
        try:
            rules.assert_plan_active()
        except rules.PolicyViolation as e:
            errors.append(str(e))

        return dict(ok=not errors, errors=errors,
                    tmn_available=tmn if not errors else None,
                    order_tool_present=tool in self.bridge.remote_tools)


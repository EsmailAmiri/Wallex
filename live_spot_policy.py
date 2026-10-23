"""Fail-closed validation for a future official-MCP spot order adapter.

This module never calls the exchange. Exchange constraints must be supplied by
an independently verified, current spot-market source; OTC limits are ignored.
"""
from decimal import Decimal, InvalidOperation

import challenge_rules as rules
import competition


def amount(value, label):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f'Invalid {label}.') from None
    if not result.is_finite() or result <= 0:
        raise ValueError(f'Invalid {label}.')
    return result


def places(value):
    return max(0, -value.as_tuple().exponent)


def validate_limit_order(*, symbol, side, price, quantity, market, available_tmn,
                         available_base, exposure_limit_tmn, now=None):
    """Return a normalized intent only when every live spot constraint is known."""
    competition.assert_active(now)
    normalized = rules.assert_market_allowed(symbol)
    if not normalized.endswith('TMN') or side not in ('BUY', 'SELL'):
        raise ValueError('Only permitted TMN spot BUY/SELL orders are supported.')
    if not isinstance(market, dict) or rules.normalize_symbol(market.get('symbol', '')) != normalized:
        raise ValueError('Current spot market metadata is missing or mismatched.')
    if market.get('is_spot') is not True or market.get('is_market_type_enable') is not True:
        raise ValueError('This market is not currently enabled for spot trading.')
    price, quantity = amount(price, 'price'), amount(quantity, 'quantity')
    min_qty = amount(market.get('minQty'), 'spot minQty')
    min_notional = amount(market.get('minNotional'), 'spot minNotional')
    qty_precision, price_precision = market.get('amount_precision'), market.get('price_precision')
    if not isinstance(qty_precision, int) or not 0 <= qty_precision <= 18:
        raise ValueError('Spot quantity precision is unverified.')
    if not isinstance(price_precision, int) or not 0 <= price_precision <= 18:
        raise ValueError('Spot price precision is unverified.')
    if places(quantity) > qty_precision or places(price) > price_precision:
        raise ValueError('Order exceeds exchange precision.')
    notional = price * quantity
    if quantity < min_qty or notional < min_notional:
        raise ValueError('Order is below current spot minimums.')
    if market.get('maxQty') is not None and quantity > amount(market['maxQty'], 'spot maxQty'):
        raise ValueError('Order exceeds current spot maximum quantity.')
    if market.get('maxNotional') is not None and notional > amount(market['maxNotional'], 'spot maxNotional'):
        raise ValueError('Order exceeds current spot maximum notional.')
    cap = amount(exposure_limit_tmn, 'exposure limit')
    if cap > rules.CREDIT_TMN or (side == 'BUY' and notional > cap):
        raise ValueError('Order exceeds the configured credit exposure limit.')
    if side == 'BUY' and notional > amount(available_tmn, 'available TMN'):
        raise ValueError('Insufficient confirmed available TMN.')
    if side == 'SELL' and quantity > amount(available_base, 'available base asset'):
        raise ValueError('Insufficient confirmed available base asset.')
    return {'symbol': normalized, 'side': side, 'type': 'LIMIT',
            'price': format(price, 'f'), 'quantity': format(quantity, 'f'),
            'notional_tmn': format(notional, 'f'), 'rule_version': rules.RULE_VERSION}

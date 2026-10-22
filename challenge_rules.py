"""Mandatory, non-configurable user-supplied Starkoch/Wallex credit-plan policy."""
import hashlib
import re
import time
from datetime import datetime
from pathlib import Path

PLAN_NAME = 'هکاتون استارکوچ | والکس'
RULE_VERSION = 'starkoch-wallex-1405-07-29-v1'
INPUT_CAPITAL = 0
CREDIT_TMN = 5_000_000
DURATION_SECONDS = 3 * 24 * 60 * 60
CREDIT_FEE_TMN = 0  # Exact plan notification supplied by the user, not a general Wallex rate.
START_ISO = '2026-10-21T20:32:00+03:30'
END_ISO = '2026-10-24T20:32:00+03:30'
START_AT = int(datetime.fromisoformat(START_ISO).timestamp())
END_AT = int(datetime.fromisoformat(END_ISO).timestamp())
assert END_AT - START_AT == DURATION_SECONDS
FORBIDDEN_CAPABILITIES = frozenset({'margin', 'leverage', 'native_trading_bot', 'deposit', 'withdrawal', 'transfer', 'renew_credit', 'activate_credit', 'stop_credit'})
_path = Path(__file__).resolve().parent / 'rules' / 'blocked_markets.txt'
_raw = _path.read_text(encoding='utf-8')
BLOCKED_DISPLAY = tuple(line.strip() for line in _raw.splitlines() if line.strip() and not line.startswith('#'))
BLOCKED = frozenset(symbol.replace('/', '') for symbol in BLOCKED_DISPLAY)
assert len(BLOCKED) == len(BLOCKED_DISPLAY), 'Duplicate blocked markets'
RULE_HASH = hashlib.sha256((RULE_VERSION + START_ISO + END_ISO + str(CREDIT_TMN) + _raw).encode()).hexdigest()


class PolicyViolation(ValueError):
    pass


def normalize_symbol(value):
    if not isinstance(value, str):
        raise PolicyViolation('نماد بازار نامعتبر است.')
    match = re.fullmatch(r'([A-Z0-9]+)(?:[/_-]?)(USDT|TMN)', value.strip().upper())
    if not match:
        raise PolicyViolation('نماد باید مانند BTC/TMN یا BTCTMN باشد.')
    return ''.join(match.groups())


def assert_market_allowed(symbol):
    symbol = normalize_symbol(symbol)
    if symbol in BLOCKED:
        raise PolicyViolation('این بازار در حساب اعتباری هکاتون غیرفعال و ممنوع است: ' + symbol)
    return symbol


def assert_capital(capital):
    try:
        valid = float(capital) == CREDIT_TMN
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise PolicyViolation('سرمایه مبنای طرح ثابت است: صفر تومان آورده و ۵٬۰۰۰٬۰۰۰ تومان اعتبار.')


def assert_capability(capability):
    if capability not in ('spot', 'instant', 'read', 'manage_api'):
        raise PolicyViolation('این قابلیت در حساب اعتباری طرح مجاز نیست: ' + str(capability))


def assert_plan_active(now=None):
    now = time.time() if now is None else now
    if now < START_AT:
        raise PolicyViolation('طرح هکاتون هنوز شروع نشده است.')
    if now >= END_AT:
        raise PolicyViolation('مهلت طرح هکاتون در ۱۴۰۵/۰۸/۰۲ ساعت ۲۰:۳۲ پایان یافته است؛ شروع یا تمدید خودکار ممنوع است.')


def snapshot(now=None, include_markets=False):
    now = int(time.time() if now is None else now)
    result = dict(name=PLAN_NAME, version=RULE_VERSION, sha256=RULE_HASH, mandatory=True,
                  input_capital=INPUT_CAPITAL, credit_tmn=CREDIT_TMN, initial_capital=CREDIT_TMN,
                  duration_seconds=DURATION_SECONDS, credit_fee_tmn=CREDIT_FEE_TMN,
                  starts_at=START_AT, expires_at=END_AT, starts_iso=START_ISO, expires_iso=END_ISO,
                  starts_fa='۱۴۰۵/۰۷/۲۹ ساعت ۲۰:۳۲', expires_fa='۱۴۰۵/۰۸/۰۲ ساعت ۲۰:۳۲',
                  timezone='Asia/Tehran', now=now, remaining_seconds=max(0, END_AT-now),
                  status='scheduled' if now < START_AT else 'active' if now < END_AT else 'expired',
                  blocked_market_count=len(BLOCKED), prohibited_capabilities=sorted(FORBIDDEN_CAPABILITIES),
                  allowed_capabilities=['spot', 'instant', 'read', 'manage_api'],
                  transaction_fee_level=1, transaction_fee_percent_verified=False,
                  account_type='credit', dedicated_key_required=True, account_type_verified=False,
                  basic_kyc_required=True, enrollment_requirements_verified=False,
                  credit_liquidation_handled_by='wallex', auto_renew=False,
                  source='user_provided_plan_and_notification')
    if include_markets:
        result['blocked_markets'] = list(BLOCKED_DISPLAY)
    return result

"""Local-only dashboard API, worker, SQLite audit trail. Python 3.11+, no dependencies."""
import csv
import io
import json
import os
import sqlite3
import threading
import time
import traceback
import uuid
import hashlib
import platform
import statistics
from contextlib import contextmanager
from dataclasses import asdict
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, urlencode
from urllib.request import urlopen, Request
from engine import DEFAULTS, BAR_SECONDS, Account, validate, demo_candles, new_account, step, backtest
from mcp_link import bridge
from wallex_settings import dashboard_port, settings
import challenge_rules as rules
import competition

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
LOCK = threading.RLock()
STATE = None
CONNECTION_CHECK = dict(market_verified=False, account_verified=False, account_message='هنوز بررسی نشده', checked_at=None)


@contextmanager
def db():
    connection = sqlite3.connect(DATA / 'wallex.sqlite3', timeout=10)
    try:
        with connection:
            connection.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, kind TEXT, payload TEXT)')
            connection.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, payload TEXT)')
            yield connection
    finally:
        connection.close()


def event(kind, payload):
    with db() as con:
        con.execute('INSERT INTO events(kind,payload) VALUES(?,?)', (kind, json.dumps(payload, ensure_ascii=False)))


def save():
    with db() as con:
        con.execute('INSERT OR REPLACE INTO state VALUES(1,?)', (json.dumps(STATE, ensure_ascii=False),))


def fresh(config=None):
    config = validate(config or DEFAULTS)
    bars = demo_candles(120, config['seed'], config['symbol']) if config['source'] == 'demo' else source_bars(config, 120)
    start = bars[-1]['t'] + BAR_SECONDS if config['source'] == 'demo' else rules.START_AT
    return dict(session_id=uuid.uuid4().hex, config=config, account=asdict(new_account(config, started_at=start)), bars=bars,
                decisions=[], trades=[], curve=[], running=False, error=None, cycles=0,
                last_update=None, mode='paper' if config['source'] == 'demo' else 'shadow')


def source_bars(config, count=480):
    symbol = rules.assert_market_allowed(config['symbol'])
    if config['source'] != 'wallex_mcp':
        return fetch_bars(config['symbol'], count)
    result = bridge.call('get_market_candles', dict(symbol=config['symbol'], count=count))
    if result.get('timeframe_seconds') != BAR_SECONDS or result.get('symbol') != config['symbol']:
        raise ValueError('نماد یا بازه پاسخ MCP با درخواست یکسان نیست.')
    return checked_bars(result['candles'])[-count:]


def mcp_status():
    status = {**bridge.status(), **CONNECTION_CHECK, 'real_orders_enabled': False}
    if not status['api_key_configured']:
        status.update(account_verified=False, account_message='کلید در فایل .env وارد نشده')
    return status


def fetch_bars(symbol, count=480):
    symbol = rules.assert_market_allowed(symbol)
    now = int(time.time())
    query = urlencode(dict(symbol=symbol, resolution='15', **{'from': now - (count + 10) * BAR_SECONDS, 'to': now}))
    request = Request('https://api.wallex.ir/v1/udf/history?' + query, headers={'User-Agent': 'WallexLab/1.0'})
    with urlopen(request, timeout=15) as response:
        data = json.load(response)
    if data.get('s') != 'ok':
        raise ValueError('والکس داده کندل معتبر برنگرداند؛ دوباره تلاش کنید.')
    raw = [dict(t=int(t), **{k: float(data[k][i]) for k in ('o', 'h', 'l', 'c', 'v')})
           for i, t in enumerate(data['t'])]
    bars = normalize_exchange_bars(raw, now)
    if len(bars) < 80:
        raise ValueError('تعداد کندل‌های بسته‌شده برای تحلیل کافی نیست.')
    return checked_bars(bars)[-count:]


def normalize_exchange_bars(raw, now):
    """Some UDF responses ignore resolution: validate and resample complete 1m groups."""
    checked_bars(raw, minimum=2, maximum=50000)
    interval = int(statistics.median([b['t'] - a['t'] for a, b in zip(raw, raw[1:])]))
    if interval == BAR_SECONDS:
        return [bar for bar in raw if bar['t'] + BAR_SECONDS <= now]
    if interval <= 0 or interval > BAR_SECONDS or BAR_SECONDS % interval:
        raise ValueError('فاصله زمانی کندل‌های والکس با بازه مورد انتظار سازگار نیست.')
    groups = {}
    for bar in raw:
        bucket = int(bar['t'] // BAR_SECONDS) * BAR_SECONDS
        groups.setdefault(bucket, []).append(bar)
    bars = []
    for timestamp, group in groups.items():
        expected = list(range(timestamp, timestamp + BAR_SECONDS, interval))
        if timestamp + BAR_SECONDS > now or [bar['t'] for bar in group] != expected:
            continue  # Exclude incomplete buckets, including missing sub-candles.
        bars.append(dict(t=timestamp, o=group[0]['o'], c=group[-1]['c'],
                         h=max(bar['h'] for bar in group), l=min(bar['l'] for bar in group),
                         v=sum(bar['v'] for bar in group)))
    return bars


def checked_bars(bars, minimum=80, maximum=5000):
    import math
    if not minimum <= len(bars) <= maximum:
        raise ValueError('تعداد کندل باید بین ۸۰ و ۵۰۰۰ باشد.')
    previous = -1
    for bar in bars:
        if not all(math.isfinite(float(bar[k])) for k in ('t', 'o', 'h', 'l', 'c', 'v')):
            raise ValueError('داده غیرعددی یا نامتناهی است.')
        if min(bar[k] for k in ('o', 'h', 'l', 'c')) <= 0 or bar['v'] < 0 or bar['t'] <= previous:
            raise ValueError('قیمت باید مثبت و زمان کندل‌ها صعودی و یکتا باشد.')
        if bar['h'] < max(bar['o'], bar['c']) or bar['l'] > min(bar['o'], bar['c']) or bar['h'] < bar['l']:
            raise ValueError('مقادیر OHLC سازگار نیستند.')
        previous = bar['t']
    return bars


def run_cycle():
    global STATE
    # The lock serializes pause/config/reset and a single worker; no duplicate cycles.
    with LOCK:
        if not STATE['running']:
            return
        config = STATE['config']
        rules.assert_market_allowed(config['symbol'])
        if config['source'] != 'demo' and not rules.START_AT <= time.time() < min(rules.END_AT, competition.END_AT):
            account = Account(**STATE['account'])
            now = int(time.time())
            trade = account.execute('SELL', STATE['bars'][-1]['c'], config,
                                    'پایان مهلت مسابقه یا طرح؛ تسویه فرضی با آخرین قیمت مشاهده‌شده؛ تسویه واقعی با والکس است', now)
            if trade:
                trade['reference_price_time'] = STATE['bars'][-1]['t']
                STATE['trades'].append(trade)
            account.halted = True
            STATE.update(account=asdict(account), running=False, error='مهلت مسابقه یا طرح فعال نیست؛ اجرای سایه متوقف است.')
            event('challenge_deadline_stop', dict(rule_version=rules.RULE_VERSION, t=now, trade=trade))
            save()
            return
        if config['source'] == 'demo':
            bars = demo_candles(121 + STATE['cycles'], config['seed'], config['symbol'])
            next_bars = [bars[-1]]
        else:
            latest = source_bars(config, 160)
            # Never fill missed candles after downtime at historical prices.
            if latest[-1]['t'] <= STATE['bars'][-1]['t']:
                return
            if latest[-1]['t'] - STATE['bars'][-1]['t'] > BAR_SECONDS * 2:
                STATE['running'] = False
                raise ValueError('وقفه در داده؛ برای جلوگیری از اجرای سفارش عقب‌افتاده، جلسه را بازنشانی کنید.')
            next_bars = [latest[-1]]
        account = Account(**STATE['account'])
        for bar in next_bars:
            item = step(account, STATE['bars'][-150:], bar, config)
            item['observed_at'] = int(time.time())
            if config['source'] != 'demo':
                # Shadow trades are hypothetical; no authenticated order endpoint is used.
                item['execution'] = 'hypothetical_next_open'
            STATE['bars'].append(bar)
            STATE['bars'] = STATE['bars'][-600:]
            STATE['decisions'] = (STATE['decisions'] + [item])[-500:]
            if item['trade']:
                STATE['trades'].append(item['trade'])
            STATE['curve'] = (STATE['curve'] + [dict(t=bar['t'], equity=item['equity'])])[-600:]
            STATE['cycles'] += 1
            event('decision', dict(session_id=STATE['session_id'], config=config, bar=bar, decision=item))
        STATE['account'] = asdict(account)
        STATE['last_update'] = int(time.time())
        STATE['error'] = None
        if account.halted and not account.quantity:
            STATE['running'] = False
        save()


def worker():
    while True:
        try:
            run_cycle()
        except Exception as exc:
            with LOCK:
                STATE['error'] = 'دریافت یا پردازش داده ناموفق بود: ' + str(exc)[:180]
                STATE['running'] = False
                event('error', dict(message=STATE['error'], t=int(time.time())))
                save()
        time.sleep(3)


def snapshot():
    with LOCK:
        result = json.loads(json.dumps(STATE))
    account, config = result['account'], result['config']
    equity = account['cash'] + account['quantity'] * result['bars'][-1]['c']
    sells = [t for t in result['trades'] if t['pnl'] is not None]
    result['metrics'] = dict(equity=equity, net_return=(equity / config['capital'] - 1) * 100,
        realized=account['realized'], unrealized=equity - account['cash'] - account['cost'],
        max_drawdown=account['drawdown'], fees=account['fees'], trades=len(result['trades']),
        win_rate=100 * sum(t['pnl'] > 0 for t in sells) / len(sells) if sells else 0,
        volume=sum(t['notional'] for t in result['trades']))
    result['mcp'] = mcp_status()
    result['challenge'] = rules.snapshot()
    result['competition'] = competition.snapshot()
    votes = [item['llm'] for item in result['decisions'] if 'llm' in item]
    result['ai'] = dict(model=settings()['LLM_MODEL'], key_configured=bool(settings()['LLM_API_KEY']),
                        votes=len(votes), approved=sum(bool(v['approve']) for v in votes),
                        unavailable=sum(v['status'] != 'ok' for v in votes))
    return result


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / 'static'), **kwargs)

    def log_message(self, fmt, *args):
        if '/api/state' not in args[0]:
            super().log_message(fmt, *args)

    def reply(self, data, code=200, attachment=False):
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if attachment:
            self.send_header('Content-Disposition', 'attachment; filename="wallex-audit.json"')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/state':
            return self.reply(snapshot())
        if path == '/api/mcp/status':
            return self.reply(mcp_status())
        if path == '/api/challenge/rules':
            return self.reply(rules.snapshot(include_markets=True))
        if path == '/api/export':
            with LOCK, db() as con:
                records = [dict(id=row[0], kind=row[1], payload=json.loads(row[2]))
                           for row in con.execute('SELECT id,kind,payload FROM events ORDER BY id')]
            hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in
                      ('engine.py', 'server.py', 'challenge_rules.py', 'competition.py',
                       'llm_gateway.py', 'ta_indicators.py', 'requirements.txt', 'rules/blocked_markets.txt')}
            return self.reply(dict(version=1, python=platform.python_version(), code_sha256=hashes,
                                   state=snapshot(), events=records), attachment=True)
        if path.startswith('/api/'):
            return self.reply(dict(error='مسیر پیدا نشد'), 404)
        super().do_GET()

    def do_POST(self):
        global STATE
        # JSON-only, loopback Host and same-origin requests prevent cross-site mutations.
        host = self.headers.get('Host', '')
        origin = self.headers.get('Origin')
        port = self.server.server_address[1]
        if host not in (f'127.0.0.1:{port}', f'localhost:{port}') or (origin and origin not in ('http://' + host,)):
            return self.reply(dict(error='درخواست از مبدأ نامعتبر'), 403)
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.reply(dict(error='JSON لازم است'), 415)
        try:
            length = int(self.headers.get('Content-Length', 0))
            if length > 2_000_000:
                raise ValueError('فایل بیش از حد بزرگ است')
            payload = json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(payload, dict):
                raise ValueError('بدنه درخواست باید شیء JSON باشد')
            path = urlparse(self.path).path
            if path == '/api/mcp/connect':
                bridge.connect()
                with LOCK:
                    symbol = STATE['config']['symbol']
                bars = source_bars(dict(source='wallex_mcp', symbol=symbol), 120)
                CONNECTION_CHECK.update(market_verified=True, checked_at=int(time.time()),
                                        account_verified=False, account_message='کلید در فایل .env وارد نشده')
                if bridge.status()['api_key_configured']:
                    try:
                        result = bridge.call('get_account_balances')
                        if not isinstance(result.get('balances'), (dict, list)):
                            raise ValueError('ساختار پاسخ موجودی معتبر نیست.')
                        CONNECTION_CHECK.update(account_verified=True, account_message='خواندن موجودی حساب تأیید شد')
                    except ValueError:
                        CONNECTION_CHECK.update(account_message='احراز هویت ناموفق؛ کلید، دسترسی خواندن، تاریخ و IP را بررسی کنید')
                with LOCK:
                    event('mcp_check', {**mcp_status(), 'bars': len(bars)})
                return self.reply(mcp_status())
            if path == '/api/backtest':
                with LOCK:
                    config = validate({**STATE['config'], **payload.get('config', {})})
                if payload.get('csv'):
                    rows = list(csv.DictReader(io.StringIO(payload['csv'].lstrip('\ufeff'))))
                    bars = checked_bars([{k: float(row[k]) for k in ('t', 'o', 'h', 'l', 'c', 'v')} for row in rows])
                    provenance = 'csv'
                elif config['source'] != 'demo':
                    bars, provenance = source_bars(config), config['source']
                else:
                    bars, provenance = demo_candles(480, config['seed'], config['symbol']), 'synthetic'
                result = backtest(bars, config)
                result['source'] = provenance
                result['bars'] = bars
                with LOCK:
                    event('backtest', result)
                return self.reply(result)
            with LOCK:
                if path == '/api/control':
                    command = payload.get('command')
                    if command == 'start':
                        validate(STATE['config'])
                        if STATE['config']['source'] != 'demo':
                            competition.assert_active()
                        if STATE['account']['halted']:
                            raise ValueError('توقف ریسک فعال است؛ جلسه را بازنشانی کنید.')
                        STATE['running'], STATE['error'] = True, None
                    elif command == 'pause':
                        STATE['running'] = False
                    elif command == 'settle':
                        STATE['running'] = False
                        account = Account(**STATE['account'])
                        trade = account.execute('SELL', STATE['bars'][-1]['c'], STATE['config'],
                                                'تسویه جلسه آزمایشی به تومان', STATE['bars'][-1]['t'])
                        if trade:
                            STATE['trades'].append(trade)
                            event('settlement', trade)
                            if STATE['curve']:
                                STATE['curve'][-1]['equity'] = account.cash
                        STATE['account'] = asdict(account)
                    elif command == 'reset':
                        if STATE['running']:
                            raise ValueError('ابتدا ایجنت را متوقف کنید.')
                        event('session_end', snapshot())
                        STATE = fresh(STATE['config'])
                        event('session_start', STATE)
                    else:
                        raise ValueError('فرمان نامعتبر')
                    event('control', dict(command=command, t=int(time.time())))
                elif path == '/api/config':
                    if STATE['running']:
                        raise ValueError('برای تغییر تنظیمات ابتدا ایجنت را متوقف کنید.')
                    config = validate({**STATE['config'], **payload})
                    changed = any(config[k] != STATE['config'][k] for k in ('source', 'symbol', 'capital', 'seed'))
                    if changed:
                        if STATE['account']['quantity']:
                            raise ValueError('ابتدا موقعیت باز را با دکمه تسویه ببندید.')
                        event('session_end', snapshot())
                        STATE = fresh(config)
                        event('session_start', STATE)
                    else:
                        STATE['config'] = config
                    event('config', dict(config=config, t=int(time.time())))
                else:
                    return self.reply(dict(error='مسیر پیدا نشد'), 404)
                save()
                return self.reply(snapshot())
        except (ValueError, KeyError, TypeError) as exc:
            self.reply(dict(error=str(exc)[:200]), 400)
        except Exception:
            traceback.print_exc()
            self.reply(dict(error='عملیات انجام نشد؛ اتصال اینترنت و وضعیت سرویس را بررسی کنید.'), 502)


def main():
    global STATE
    DATA.mkdir(exist_ok=True)
    with db() as con:
        row = con.execute('SELECT payload FROM state WHERE id=1').fetchone()
    STATE = json.loads(row[0]) if row else fresh()
    validate(STATE['config'])
    if not STATE['account'].get('expires_at'):
        STATE['account']['expires_at'] = (STATE['bars'][0]['t'] + 120 * BAR_SECONDS + rules.DURATION_SECONDS
                                         if STATE['config']['source'] == 'demo' else rules.END_AT)
    STATE.setdefault('session_id', uuid.uuid4().hex)
    if STATE.get('rule_version') != rules.RULE_VERSION:
        STATE['rule_version'] = rules.RULE_VERSION
        event('policy_applied', rules.snapshot())
    if not row:
        event('session_start', STATE)
    STATE['running'] = False  # Explicit resume after every process restart.
    port = dashboard_port()
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    (ROOT / 'static' / 'runtime.json').write_text(json.dumps(dict(port=port)), encoding='utf-8')
    threading.Thread(target=worker, daemon=True).start()
    print(f'Wallex Agent Lab: http://127.0.0.1:{port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        with LOCK:
            STATE['running'] = False
            save()
        server.server_close()
        bridge.close()


if __name__ == '__main__':
    import sys
    import subprocess
    virtual_python = ROOT / '.venv' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    if virtual_python.exists() and Path(sys.executable).resolve() != virtual_python.resolve():
        raise SystemExit(subprocess.call([str(virtual_python), str(ROOT / 'server.py')]))
    main()

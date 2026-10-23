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
import signal
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
DATA = Path(os.environ.get('MADAR_DATA_DIR', str(ROOT / 'data')))
LOCK = threading.RLock()
STATE = None
LIVE_ADAPTER = None          # LiveOrderAdapter, created on demand
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


def commit_event(kind, payload):
    """Commit ledger snapshot and its decision event in one SQLite transaction."""
    with db() as con:
        con.execute('INSERT INTO events(kind,payload) VALUES(?,?)', (kind,json.dumps(payload,ensure_ascii=False)))
        con.execute('INSERT OR REPLACE INTO state VALUES(1,?)', (json.dumps(STATE,ensure_ascii=False),))


def deadline_stop(now=None):
    now = time.time() if now is None else now
    with LOCK:
        if not STATE or not STATE['running'] or STATE['config']['source']=='demo':
            return False
        cutoff = rules.END_AT if STATE.get('live_trading') else min(rules.END_AT, competition.END_AT)
        if rules.START_AT <= now < cutoff:
            return False
        account=Account(**STATE['account'])
        trade=account.execute('SELL',STATE['bars'][-1]['c'],STATE['config'],
            'پایان مهلت؛ تسویه فرضی با آخرین قیمت مشاهده‌شده؛ تسویه واقعی با والکس است',int(now))
        if trade:
            trade['reference_price_time']=STATE['bars'][-1]['t']
            STATE['trades'].append(trade)
        account.halted=True
        STATE.update(account=asdict(account),running=False,error='مهلت مسابقه یا طرح فعال نیست؛ اجرای سایه متوقف است.')
        STATE['revision']=STATE.get('revision',0)+1
        if STATE['curve']:STATE['curve'][-1]['equity']=account.cash
        commit_event('challenge_deadline_stop',dict(t=int(now),trade=trade,rule_version=rules.RULE_VERSION))
        return True


def deadline_watchdog():
    while True:
        try:deadline_stop()
        except Exception:
            with LOCK:
                if STATE:STATE.update(running=False,error='خطای ثبت توقف؛ بررسی دفتر محلی لازم است.')
        time.sleep(.5)


def fresh(config=None):
    config = validate(config or DEFAULTS)
    bars = demo_candles(120, config['seed'], config['symbol']) if config['source'] == 'demo' else source_bars(config, 120)
    start = bars[-1]['t'] + BAR_SECONDS if config['source'] == 'demo' else rules.START_AT
    return dict(session_id=uuid.uuid4().hex, config=config, account=asdict(new_account(config, started_at=start)), bars=bars,
                decisions=[], trades=[], curve=[], running=False, error=None, cycles=0,
                last_update=None, mode='paper' if config['source'] == 'demo' else 'shadow', live_trading=False)


def source_bars(config, count=480):
    symbol = rules.assert_market_allowed(config['symbol'])
    if config['source'] != 'wallex_mcp':
        return fetch_bars(config['symbol'], count)
    result = bridge.call('get_market_candles', dict(symbol=config['symbol'], count=count))
    if result.get('timeframe_seconds') != BAR_SECONDS or result.get('symbol') != config['symbol']:
        raise ValueError('نماد یا بازه پاسخ MCP با درخواست یکسان نیست.')
    return checked_bars(result['candles'])[-count:]


def _get_live_adapter():
    global LIVE_ADAPTER
    if LIVE_ADAPTER is None:
        from live_order import LiveOrderAdapter
        LIVE_ADAPTER = LiveOrderAdapter(bridge, DATA / 'live-orders.sqlite3')
    return LIVE_ADAPTER


def mcp_status():
    with LOCK:
        live = bool(STATE and STATE.get('live_trading'))
    status = {**bridge.status(), **CONNECTION_CHECK, 'real_orders_enabled': live}
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
    if deadline_stop():return
    with LOCK:
        if not STATE['running']:return
        draft=json.loads(json.dumps(STATE))
        revision=STATE.get('revision',0)
    config=draft['config']
    rules.assert_market_allowed(config['symbol'])
    if config['source']=='demo':
        bar=demo_candles(121+draft['cycles'],config['seed'],config['symbol'])[-1]
    else:
        latest=source_bars(config,160)
        if latest[-1]['t']<=draft['bars'][-1]['t']:return
        if latest[-1]['t']-draft['bars'][-1]['t']>BAR_SECONDS*2:
            raise ValueError('وقفه در داده؛ بازنشانی جلسه لازم است. اجرای عقب‌افتاده ممنوع است.')
        bar=latest[-1]
    account=Account(**draft['account'])
    item=step(account,draft['bars'][-150:],bar,config)
    item['observed_at']=int(time.time())
    if config['source']!='demo':item['execution']='hypothetical_next_open'
    # ── live order placement ────────────────────────────────────
    live_trading = draft.get('live_trading', False)
    if live_trading and config['source'] != 'demo' and item.get('trade'):
        trade = item['trade']
        try:
            adapter = _get_live_adapter()
            live_result = adapter.place_order(
                symbol=config['symbol'],
                side=trade['action'],
                price_approx=bar['c'],
                config=config)
            item['live_order'] = live_result
            commit_event('live_order', dict(
                session_id=draft['session_id'],
                client_id=live_result.get('client_id'),
                symbol=config['symbol'],
                side=trade['action'],
                status=live_result.get('status'),
                t=int(time.time())))
        except Exception as exc:
            item['live_order'] = dict(status='error', error=str(exc)[:300])
            commit_event('live_order_error', dict(
                session_id=draft['session_id'],
                error=str(exc)[:300], t=int(time.time())))
    with LOCK:
        if deadline_stop():return
        if not STATE['running'] or STATE['session_id']!=draft['session_id'] or STATE.get('revision',0)!=revision:
            return  # Late model/network response has no authority after pause/reset/config.
        STATE['bars']=(STATE['bars']+[bar])[-600:]
        STATE['decisions']=(STATE['decisions']+[item])[-500:]
        if item['trade']:STATE['trades'].append(item['trade'])
        STATE['curve']=(STATE['curve']+[dict(t=bar['t'],equity=item['equity'])])[-600:]
        STATE.update(account=asdict(account),cycles=STATE['cycles']+1,last_update=int(time.time()),error=None)
        if account.halted and not account.quantity:STATE['running']=False
        STATE['revision']=revision+1
        commit_event('decision',dict(session_id=STATE['session_id'],config=config,bar=bar,decision=item))


def worker():
    while True:
        with LOCK:
            revision=STATE.get('revision',0)
            session=STATE['session_id']
        try:
            run_cycle()
        except Exception as exc:
            with LOCK:
                if STATE.get('revision',0)!=revision or STATE['session_id']!=session:continue
                STATE['error'] = 'دریافت یا پردازش داده ناموفق بود؛ اتصال و ورودی‌ها را بررسی کنید.'
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
    result['live_trading'] = result.get('live_trading', False)
    if result['live_trading']:
        try:
            adapter = _get_live_adapter()
            result['live_orders'] = adapter.all_orders()[-20:]
            result['live_fills'] = adapter.filled_orders()[-20:]
        except Exception:
            result['live_orders'] = []
            result['live_fills'] = []
    return result


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / 'static'), **kwargs)

    def log_message(self, fmt, *args):
        if not args or '/api/state' not in str(args[0]):
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
        if not self.valid_origin():return self.reply(dict(error='مبدأ نامعتبر'),403)
        if path == '/api/health':
            return self.reply(dict(status='ok',version='2.0.0',real_orders_enabled=False))
        if path == '/runtime.json':
            return self.reply(dict(port=int(os.environ.get('MADAR_PUBLIC_PORT',dashboard_port()))))
        if path == '/api/product':
            from product_status import release_info
            return self.reply(release_info())
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

    def valid_origin(self):
        host = self.headers.get('Host', '')
        origin = self.headers.get('Origin')
        port = self.server.server_address[1]
        ports={str(port),os.environ.get('MADAR_PUBLIC_PORT',str(port))}
        if os.environ.get('MADAR_BIND') == '0.0.0.0' or 'liara.run' in host:
            return True
        return host in {f'{name}:{p}' for name in ('127.0.0.1','localhost') for p in ports} and (not origin or origin=='http://'+host)

    def do_POST(self):
        global STATE
        if not self.valid_origin():
            return self.reply(dict(error='درخواست از مبدأ نامعتبر'), 403)
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.reply(dict(error='JSON لازم است'), 415)
        try:
            length = int(self.headers.get('Content-Length', 0))
            if length < 0 or length > 2_000_000:
                raise ValueError('فایل بیش از حد بزرگ است')
            payload = json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(payload, dict):
                raise ValueError('بدنه درخواست باید شیء JSON باشد')
            path = urlparse(self.path).path
            if path == '/api/preflight':
                from product_status import preflight
                return self.reply(preflight())
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
            if path == '/api/live/verify':
                adapter = _get_live_adapter()
                adapter.refresh_markets()
                return self.reply(adapter.verify())
            if path == '/api/live/orders':
                adapter = _get_live_adapter()
                return self.reply(dict(orders=adapter.all_orders(),
                                       fills=adapter.filled_orders()))
            if path == '/api/live/balance':
                adapter = _get_live_adapter()
                return self.reply(dict(balances=adapter.get_balances()))
            if path == '/api/live/enable':
                with LOCK:
                    if STATE['running']:
                        raise ValueError('ابتدا ایجنت را متوقف کنید.')
                    if STATE['config']['source'] == 'demo':
                        raise ValueError('حالت دمو امکان معامله واقعی ندارد؛ منبع را به wallex_mcp تغییر دهید.')
                    adapter = _get_live_adapter()
                    adapter.refresh_markets()
                    check = adapter.verify()
                    if not check['ok']:
                        raise ValueError('پیش‌شرط‌های معامله واقعی: ' + '; '.join(check['errors']))
                    STATE['live_trading'] = True
                    STATE['mode'] = 'live'
                    event('live_enabled', dict(t=int(time.time()), verify=check))
                    save()
                return self.reply(dict(ok=True, mode='live', verify=check))
            if path == '/api/live/disable':
                with LOCK:
                    STATE['live_trading'] = False
                    STATE['mode'] = 'shadow'
                    event('live_disabled', dict(t=int(time.time())))
                    save()
                return self.reply(dict(ok=True, mode='shadow'))
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
                STATE['revision']=STATE.get('revision',0)+1
                if path == '/api/control':
                    command = payload.get('command')
                    if command == 'start':
                        validate(STATE['config'])
                        if STATE['config']['source'] != 'demo' and not STATE.get('live_trading'):
                            competition.assert_active()
                        elif STATE['config']['source'] != 'demo':
                            rules.assert_plan_active()
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
                    elif command == 'enable_live':
                        if STATE['running']:
                            raise ValueError('ابتدا ایجنت را متوقف کنید.')
                        if STATE['config']['source'] == 'demo':
                            raise ValueError('حالت دمو امکان معامله واقعی ندارد.')
                        adapter = _get_live_adapter()
                        adapter.refresh_markets()
                        check = adapter.verify()
                        if not check['ok']:
                            raise ValueError('پیش‌شرط: ' + '; '.join(check['errors']))
                        STATE['live_trading'] = True
                        STATE['mode'] = 'live'
                    elif command == 'disable_live':
                        STATE['live_trading'] = False
                        STATE['mode'] = 'shadow'
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
            self.reply(dict(error='عملیات انجام نشد؛ اتصال اینترنت و وضعیت سرویس را بررسی کنید.'), 502)


def main():
    global STATE
    DATA.mkdir(parents=True,exist_ok=True)
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
    # Autorun when deployed in cloud (e.g. Liara)
    if os.environ.get('MADAR_AUTORUN') == '1':
        try:
            symbol = os.environ.get('MADAR_SYMBOL', 'USDTTMN')
            strategy = os.environ.get('MADAR_STRATEGY', 'ta_trend')
            model = os.environ.get('MADAR_MODEL', 'hybrid_llm')
            source = 'wallex_mcp'
            target_cfg = dict(STATE['config'], symbol=symbol, strategy=strategy, model=model, source=source)
            validate(target_cfg)
            if any(target_cfg[k] != STATE['config'][k] for k in ('source', 'symbol', 'capital', 'seed')):
                STATE = fresh(target_cfg)
            else:
                STATE['config'] = target_cfg
            if os.environ.get('MADAR_LIVE_TRADING') == '1':
                bridge.connect()
                adapter = _get_live_adapter()
                adapter.refresh_markets()
                check = adapter.verify()
                if check['ok']:
                    STATE['live_trading'] = True
                    STATE['mode'] = 'live'
                    STATE['running'] = True
                    event('autorun_live_started', dict(t=int(time.time()), config=STATE['config']))
                else:
                    print('AUTORUN WARNING: Live preconditions not fully met:', check['errors'], flush=True)
                    STATE['running'] = True
            else:
                STATE['running'] = True
            save()
        except Exception as e:
            print('AUTORUN ERROR:', e, flush=True)
    port = dashboard_port()
    bind=os.environ.get('MADAR_BIND','127.0.0.1')
    if bind not in ('127.0.0.1','0.0.0.0'):raise ValueError('Unsupported bind address')
    server = ThreadingHTTPServer((bind, port), Handler)
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=deadline_watchdog, daemon=True).start()
    signal.signal(signal.SIGTERM,lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
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

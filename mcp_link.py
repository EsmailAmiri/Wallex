"""Persistent MCP client: actual initialize -> tools/list -> tools/call over stdio/HTTP."""
import asyncio
import json
import sys
import threading
import time
import hashlib
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse
from wallex_settings import settings
import challenge_rules as rules

ROOT = Path(__file__).resolve().parent
OFFICIAL_URL = 'https://api.wallex.ir/mcp'
OFFICIAL_TOOLS = dict(get_markets='wallex_basic_get_markets', get_market_candles='wallex_basic_candle_history',
                      get_account_balances='wallex_basic_get_account_balances', get_account_fees='wallex_basic_get_account_fees')
READ_TOOLS = {'health', 'get_markets', 'get_market_candles', 'get_account_balances', 'get_challenge_rules', 'check_challenge_market', 'get_account_fees'}


def remote_headers(url, config):
    """Only the exact user-verified official MCP endpoint receives the exchange key."""
    if url.rstrip('/') == OFFICIAL_URL:
        return {'X-API-Key': config['WALLEX_API_KEY']} if config['WALLEX_API_KEY'] else {}
    return {'Authorization': 'Bearer ' + config['WALLEX_MCP_TOKEN']} if config['WALLEX_MCP_TOKEN'] else {}


def official_request(name, arguments, now=None):
    if name not in OFFICIAL_TOOLS:
        raise ValueError('This official MCP tool is not allowed.')
    if name == 'get_market_candles':
        symbol = rules.assert_market_allowed(arguments.get('symbol', 'BTCTMN'))
        count = arguments.get('count', 160)
        if not isinstance(count, int) or not 80 <= count <= 480:
            raise ValueError('Candle count must be 80..480.')
        now = int(time.time() if now is None else now)
        return OFFICIAL_TOOLS[name], dict(symbol=symbol, resolution='15', **{'from': now-(count+10)*900, 'to': now})
    if arguments:
        raise ValueError('Unexpected MCP arguments.')
    return OFFICIAL_TOOLS[name], {}


def official_response(name, data, arguments):
    if not isinstance(data, dict) or data.get('success') is False:
        raise ValueError('پاسخ MCP رسمی ناموفق یا نامعتبر بود.')
    result = data.get('result', data)
    if name == 'get_market_candles':
        from server import normalize_exchange_bars, checked_bars
        if result.get('s') != 'ok':
            raise ValueError('MCP رسمی کندل معتبر برنگرداند.')
        raw = [dict(t=int(timestamp), **{key: float(result[key][i]) for key in ('o','h','l','c','v')})
               for i, timestamp in enumerate(result['t'])]
        bars = checked_bars(normalize_exchange_bars(raw, int(time.time())))
        return dict(symbol=rules.assert_market_allowed(arguments.get('symbol','BTCTMN')), timeframe_seconds=900,
                    candles=bars[-arguments.get('count',160):], source='wallex-official-mcp', transport='mcp')
    if name == 'get_markets':
        allowed={}
        for symbol, details in result.get('symbols',{}).items():
            try:
                rules.assert_market_allowed(symbol)
                allowed[symbol]=details
            except rules.PolicyViolation:
                continue
        return {**result, 'symbols': allowed, 'rule_version': rules.RULE_VERSION}
    return result


class MCPBridge:
    def __init__(self):
        self.thread = None
        self.loop = self.session = self.stop = None
        self.ready = threading.Event()
        self.guard = threading.Lock()
        self.fingerprint = None
        self.remote_tools = set()
        self.info = dict(connected=False, transport='stdio', provider='project-owned-local', tools=[], calls=0, last_call=None, error=None)

    def status(self):
        return {**self.info, 'api_key_configured': bool(settings()['WALLEX_API_KEY'])}

    def connect(self):
        config=settings()
        fingerprint=hashlib.sha256(json.dumps({k:v for k,v in config.items() if k.startswith('WALLEX_')},sort_keys=True).encode()).digest()
        with self.guard:
            if self.thread and self.thread.is_alive() and self.fingerprint != fingerprint:
                self.close()
                if self.thread.is_alive():
                    raise ValueError('نشست قبلی MCP در حال بسته‌شدن است؛ دوباره تلاش کنید.')
            if not self.thread or not self.thread.is_alive():
                self.fingerprint=fingerprint
                self.ready.clear()
                self.info.update(connected=False, error=None, tools=[])
                self.thread = threading.Thread(target=self._thread, daemon=True, name='wallex-mcp-client')
                self.thread.start()
        if not self.ready.wait(30) or not self.info['connected']:
            raise ValueError(self.info['error'] or 'MCP connection timed out.')
        return self.status()

    def _thread(self):
        try:
            asyncio.run(self._serve())
        except Exception:
            self.info.update(connected=False, error='اتصال MCP برقرار نشد؛ وابستگی‌ها، آدرس و تنظیمات را بررسی کنید.')
        finally:
            self.info['connected'] = False
            self.ready.set()

    async def _serve(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        self.loop = asyncio.get_running_loop()
        self.stop, self.call_lock = asyncio.Event(), asyncio.Lock()
        config = settings()
        async with AsyncExitStack() as stack:
            if config['WALLEX_MCP_URL']:
                import httpx
                from mcp.client.streamable_http import streamable_http_client
                url = config['WALLEX_MCP_URL']
                if urlparse(url).scheme != 'https' or urlparse(url).username or urlparse(url).password:
                    raise ValueError('Remote MCP requires HTTPS without credentials in its URL.')
                headers = remote_headers(url,config)
                client = await stack.enter_async_context(httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(30,read=60), follow_redirects=False))
                read, write, _ = await stack.enter_async_context(streamable_http_client(url, http_client=client))
                self.info.update(transport='streamable-http', provider='wallex-official' if url.rstrip('/')==OFFICIAL_URL else 'configured-remote')
            else:
                python = ROOT / '.venv' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
                params = StdioServerParameters(command=str(python if python.exists() else sys.executable), args=[str(ROOT / 'wallex_mcp.py')], cwd=str(ROOT))
                read, write = await stack.enter_async_context(stdio_client(params))
                self.info.update(transport='stdio', provider='project-owned-local')
            self.session = await stack.enter_async_context(ClientSession(read, write, read_timeout_seconds=timedelta(seconds=35)))
            initialized=await self.session.initialize()
            result = await self.session.list_tools()
            self.remote_tools={tool.name for tool in result.tools}
            tools=sorted(self.remote_tools.intersection(OFFICIAL_TOOLS.values())) if self.info['provider']=='wallex-official' else sorted(self.remote_tools.intersection(READ_TOOLS))
            self.info.update(connected=True, tools=tools, discovered_tools_count=len(self.remote_tools),
                             server_name=initialized.serverInfo.name, server_version=initialized.serverInfo.version, error=None)
            self.ready.set()
            await self.stop.wait()

    async def _call(self, name, arguments):
        async with self.call_lock:
            started = time.monotonic()
            official=self.info['provider']=='wallex-official'
            native_name, native_arguments=official_request(name,arguments) if official else (name,arguments)
            if native_name not in self.remote_tools:
                raise ValueError('ابزار موردنیاز در MCP مقصد وجود ندارد: '+native_name)
            result = await self.session.call_tool(native_name, native_arguments)
            if result.isError:
                # Tool errors may echo credentials on a remote server; never relay raw errors to the UI.
                raise ValueError('ابزار MCP ناموفق بود: ' + name + '؛ کلید، مجوز و اتصال را بررسی کنید.')
            data = result.structuredContent
            if data is None:
                data = json.loads(''.join(item.text for item in result.content if item.type == 'text'))
            self.info['calls'] += 1
            self.info['last_call'] = dict(tool=native_name, timestamp=int(time.time()), duration_ms=round((time.monotonic()-started)*1000))
            return official_response(name,data,arguments) if official else data

    def call(self, name, arguments=None):
        if name not in READ_TOOLS:
            raise ValueError('This dashboard MCP client permits read-only tools only.')
        if name=='get_challenge_rules':
            return rules.snapshot(include_markets=True)
        if name=='check_challenge_market':
            symbol=rules.normalize_symbol((arguments or {})['symbol'])
            return dict(symbol=symbol,excluded=symbol in rules.BLOCKED,rule_version=rules.RULE_VERSION)
        self.connect()
        if self.info['provider']=='wallex-official' and name=='health':
            return dict(bridge='wallex-official',server=self.info['server_name'],real_orders_enabled=False)
        required=OFFICIAL_TOOLS.get(name,name) if self.info['provider']=='wallex-official' else name
        if required not in self.info['tools']:
            raise ValueError('ابزار موردنیاز در این MCP وجود ندارد: ' + name)
        future = asyncio.run_coroutine_threadsafe(self._call(name, arguments or {}), self.loop)
        try:
            return future.result(timeout=40)
        except TimeoutError:
            future.cancel()
            raise ValueError('مهلت دریافت پاسخ MCP تمام شد.') from None

    def close(self):
        if self.loop and self.loop.is_running() and self.stop:
            self.loop.call_soon_threadsafe(self.stop.set)
        if self.thread:
            self.thread.join(timeout=5)


bridge = MCPBridge()

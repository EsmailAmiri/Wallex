"""Project-owned MCP bridge to official Wallex REST, NOT an official Wallex MCP server."""
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from wallex_settings import settings, dashboard_port
import challenge_rules as rules

mcp = FastMCP('Madar Wallex bridge', instructions=(
    'Project-owned bridge to official api.wallex.ir. Market data is live; dashboard accounts '
    'and executions are paper/shadow. No real order, withdrawal or transfer tools exist. '
    f'Mandatory hackathon policy {rules.RULE_VERSION}: credit capital 5000000 TMN, no personal funding, '
    f'expiry {rules.END_ISO}, zero credit fee for this plan, excluded markets must never be traded. '
    'No margin, native exchange bots, deposits, withdrawals, transfers, renewal or activation. '
    'Use get_challenge_rules before any plan-related decision.'), log_level='WARNING')
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def official_get(path, authenticated=False):
    # Fixed origin and no redirects: keys never leave the official Wallex API host.
    headers = {'Accept': 'application/json', 'Content-Type': 'application/json', 'User-Agent': 'Madar-MCP/1.0'}
    if authenticated:
        key = settings()['WALLEX_API_KEY']
        if not key:
            raise ValueError('WALLEX_API_KEY is missing in the local .env file.')
        headers['X-API-Key'] = key
    try:
        request = Request('https://api.wallex.ir/v1' + path, headers=headers)
        with build_opener(NoRedirect()).open(request, timeout=15) as response:
            data = json.load(response)
    except HTTPError as exc:
        # Do not echo response bodies, request headers or credentials.
        raise ValueError(f'Wallex HTTP {exc.code}; check key, permissions, expiry and IP restrictions.') from None
    except (URLError, TimeoutError):
        raise ValueError('Wallex network request failed.') from None
    if data.get('success') is False:
        raise ValueError('Wallex rejected the request. Check credentials and permissions.')
    return data.get('result', data)


@mcp.tool(annotations=READ)
def health() -> dict:
    """Report bridge identity, credential presence and the fact that trading is disabled."""
    return dict(bridge='project-owned-local', exchange_origin='https://api.wallex.ir',
                api_key_configured=bool(settings()['WALLEX_API_KEY']), real_orders_enabled=False,
                rule_version=rules.RULE_VERSION, rule_sha256=rules.RULE_HASH, plan_expires_at=rules.END_AT)


@mcp.tool(annotations=READ)
def get_markets() -> dict:
    """Read live public Wallex markets. Requires no API key."""
    result = official_get('/markets')
    symbols = result.get('symbols', {})
    allowed = {}
    for symbol, details in symbols.items():
        try:
            rules.assert_market_allowed(symbol)
            allowed[symbol] = details
        except rules.PolicyViolation:
            continue
    return {**result, 'symbols': allowed, 'rule_version': rules.RULE_VERSION,
            'excluded_by_policy': len(symbols) - len(allowed)}


@mcp.tool(annotations=READ)
def get_market_candles(symbol: str = 'BTCTMN', count: int = 160) -> dict:
    """Read complete normalized 15-minute OHLCV candles from official Wallex REST."""
    from engine import SYMBOLS
    from server import fetch_bars
    symbol = rules.assert_market_allowed(symbol)
    if symbol not in SYMBOLS or not 80 <= count <= 480:
        raise ValueError('Supported research symbol and candle count 80..480 required.')
    return dict(source='wallex', transport='mcp', timeframe_seconds=900, symbol=symbol,
                candles=fetch_bars(symbol, count))


@mcp.tool(annotations=READ)
def get_account_balances() -> dict:
    """Read dedicated account balances using the key from local .env. No orders are submitted."""
    return official_get('/account/balances', authenticated=True)


def dashboard(path, payload=None):
    headers = {'Content-Type': 'application/json'}
    body = json.dumps(payload).encode() if payload is not None else None
    try:
        with build_opener(NoRedirect()).open(Request(f'http://127.0.0.1:{dashboard_port()}/api/' + path, data=body, headers=headers), timeout=90) as response:
            result = json.load(response)
    except (HTTPError, URLError, TimeoutError):
        raise ValueError('Local dashboard unavailable or command rejected. Start the dashboard and check its status.') from None
    # Keep snapshots sent to agents small. Do not return secrets or audit history.
    return {key: result[key] for key in ('config', 'mode', 'running', 'metrics', 'cycles', 'error', 'challenge') if key in result}


@mcp.tool(annotations=READ)
def get_agent_status() -> dict:
    """Inspect the local Madar paper/shadow agent and its metrics."""
    return dashboard('state')


@mcp.tool(annotations=READ)
def get_challenge_rules() -> dict:
    """Read mandatory credit amount, exact expiry, disabled markets and prohibited account features."""
    return rules.snapshot(include_markets=True)


@mcp.tool(annotations=READ)
def check_challenge_market(symbol: str) -> dict:
    """Check whether a pair is excluded by the user-supplied hackathon plan. Does not assert exchange availability."""
    normalized = rules.normalize_symbol(symbol)
    return dict(symbol=normalized, excluded=normalized in rules.BLOCKED,
                rule_version=rules.RULE_VERSION, exchange_availability_verified=False)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
def start_shadow_agent() -> dict:
    """Start the local paper/shadow agent. This CANNOT place real exchange orders."""
    return dashboard('control', {'command': 'start'})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
def pause_agent() -> dict:
    """Pause the local paper/shadow agent; no real exchange order is cancelled."""
    return dashboard('control', {'command': 'pause'})


if __name__ == '__main__':
    mcp.run(transport='stdio')

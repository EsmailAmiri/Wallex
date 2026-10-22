"""Read only documented settings. Never execute/interpolate .env values."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KEYS = ('WALLEX_API_KEY', 'WALLEX_MCP_URL', 'WALLEX_MCP_TOKEN', 'WALLEX_DASHBOARD_PORT',
        'LLM_BASE_URL', 'LLM_API_KEY', 'LLM_MODEL', 'LLM_TIMEOUT_SECONDS', 'LLM_MODE')


def settings():
    values = {}
    path = ROOT / '.env'
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key, value = key.strip(), value.strip()
            if key in KEYS:
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                values[key] = value
    # .env is re-read for every account request, so adding a key needs no restart.
    return {key: os.environ.get(key) or values.get(key, '') for key in KEYS}


def dashboard_port():
    port = int(settings()['WALLEX_DASHBOARD_PORT'] or '8765')
    if not 1024 <= port <= 65535:
        raise ValueError('Invalid dashboard port.')
    return port

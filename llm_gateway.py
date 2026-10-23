"""9router entry veto. No exchange tools, credentials, account data or order sizing."""
import hashlib
import json
import math
import sqlite3
import threading
import os
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from wallex_settings import settings

VERSION = 'entry-veto-v2-prompt-hash'
CACHE = Path(__file__).resolve().parent / 'data' / 'llm-votes.sqlite3'
LOCK = threading.Lock()
SYSTEM = '''You are the quantitative trading intelligence agent for spot trading on Wallex.
Evaluate candidate long spot entries on 15-minute crypto bars.
Round-trip friction is approximately ~0.70% (taker fees 0.30% each side + 0.05% slippage each side).
Mandate: capital preservation first, asymmetric profit second.

Evaluation Rules:
1. REGIME: Approve ONLY if the market demonstrates a valid upward regime (positive trend_pct, ADX >= 20, price structure holding above key moving averages).
2. EXHAUSTION GUARD: REJECT ("weak_edge") if RSI > 66, or if price is extended far above the fast EMA, or right after an abrupt vertical spike. Best entries occur on pullbacks in an active trend.
3. FEE HURDLE: REJECT ("costs") if the expected volatility (ATR%) is too low (< 0.5%) to comfortably overcome 0.70% round-trip friction.
4. UNCERTAINTY: If signals conflict or momentum is waning, REJECT ("uncertain"). Prefer cash (0% loss) over low-conviction trades.
5. APPROVAL: Approve ("trend") ONLY when trend momentum is strong, not overextended (RSI between 45 and 65), and expected move clearly exceeds fee friction.

Return strictly JSON only:
{"approve": boolean, "confidence": number from 0 to 100, "reason_code": "trend" | "weak_edge" | "uncertain" | "costs"}'''


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def validate_vote(value):
    if not isinstance(value, dict) or set(value) != {'approve', 'confidence', 'reason_code'}:
        raise ValueError('Invalid vote schema')
    confidence = value['confidence']
    if type(value['approve']) is not bool or type(confidence) not in (int, float):
        raise ValueError('Invalid vote types')
    if not math.isfinite(confidence) or not 0 <= confidence <= 100:
        raise ValueError('Invalid confidence')
    if value['reason_code'] not in ('trend', 'weak_edge', 'uncertain', 'costs'):
        raise ValueError('Invalid reason code')
    return value


def endpoint(config):
    base = config['LLM_BASE_URL'].rstrip('/')
    parts = urlsplit(base)
    if (parts.username or parts.password or parts.query or parts.fragment or not parts.hostname
            or not (parts.scheme == 'https' or (parts.scheme == 'http' and
                (parts.hostname in ('127.0.0.1', 'localhost', '::1') or
                 (os.environ.get('MADAR_ALLOW_DOCKER_ROUTER')=='1' and parts.hostname=='host.docker.internal'))))):
        raise ValueError('Invalid LLM URL')
    return base + '/chat/completions'


def request_vote(config, features):
    if not config['LLM_API_KEY'] or not config['LLM_MODEL']:
        raise ValueError('LLM not configured')
    timeout = float(config['LLM_TIMEOUT_SECONDS'] or 20)
    if not math.isfinite(timeout) or not 1 <= timeout <= 30:
        raise ValueError('Invalid timeout')
    body = dict(model=config['LLM_MODEL'], temperature=0, max_tokens=256, stream=False,
                response_format={'type': 'json_object'}, messages=[
                    {'role': 'system', 'content': SYSTEM},
                    {'role': 'user', 'content': json.dumps(features, allow_nan=False, sort_keys=True)}])
    request = Request(endpoint(config), data=json.dumps(body).encode(), headers={
        'Authorization': 'Bearer ' + config['LLM_API_KEY'], 'Content-Type': 'application/json'})
    # Never print errors/bodies: a provider can echo credentials or internal information.
    with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError('Oversized response')
    result = json.loads(raw)
    content = result['choices'][0]['message']['content'].strip()
    if content.startswith('```'):
        content = content.split('```')[1]
        if content.startswith('json'):
            content = content[4:]
        content = content.strip()
    return validate_vote(json.loads(content))


def request_sha256(config, features):
    return hashlib.sha256(json.dumps(dict(version=VERSION, model=config['LLM_MODEL'],
        prompt_sha256=hashlib.sha256(SYSTEM.encode()).hexdigest(),
        endpoint=config['LLM_BASE_URL'], features=features), sort_keys=True, allow_nan=False).encode()).hexdigest()


class ReplayVotes:
    """Use the exact recorded LLM outputs, including failures, without network or cache."""
    def __init__(self, decisions):
        self.votes = [item['llm'] for item in decisions if 'llm' in item]
        self.index = 0

    def __call__(self, features):
        if self.index >= len(self.votes):
            raise ValueError('REPLAY MISMATCH: unexpected LLM request')
        vote = self.votes[self.index]
        if vote['request_sha256'] != request_sha256(settings(), features) or vote['prompt_version'] != VERSION:
            raise ValueError('REPLAY MISMATCH: LLM input, model or prompt changed')
        self.index += 1
        return vote

    def assert_finished(self):
        if self.index != len(self.votes):
            raise ValueError('REPLAY MISMATCH: recorded LLM requests were skipped')


def review(features):
    """Successful votes are immutable per exact features/model/endpoint/prompt version.

    Replay never calls the network; missing/invalid votes always veto entry.
    """
    config = settings()
    key = request_sha256(config, features)
    metadata = dict(model=config['LLM_MODEL'], prompt_version=VERSION, request_sha256=key)
    try:
        if config['LLM_MODE'] not in ('live', 'replay'):
            raise ValueError('Invalid LLM mode')
        with LOCK:
            CACHE.parent.mkdir(exist_ok=True)
            with closing(sqlite3.connect(CACHE)) as con, con:
                con.execute('CREATE TABLE IF NOT EXISTS votes (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                row = con.execute('SELECT value FROM votes WHERE key=?', (key,)).fetchone()
                if row:
                    vote = validate_vote(json.loads(row[0]))
                    return {**vote, **metadata, 'status': 'ok'}
                if config['LLM_MODE'] == 'replay':
                    return dict(approve=False, confidence=0, reason_code='uncertain', **metadata, status='cache_miss')
                vote = request_vote(config, features)
                con.execute('INSERT INTO votes VALUES (?,?)', (key, json.dumps(vote)))
                return {**vote, **metadata, 'status': 'ok'}
    except Exception:
        return dict(approve=False, confidence=0, reason_code='uncertain', **metadata, status='unavailable')


if __name__ == '__main__':
    config = settings()
    try:
        vote = request_vote(config, dict(test=True, instruction='No trade proposed. Reject entry.'))
        print(json.dumps(dict(connected=True, model=config['LLM_MODEL'], structured_output_valid=True)))
    except Exception:
        print(json.dumps(dict(connected=False, model=config['LLM_MODEL'],
                              message='Set LLM_API_KEY in .env and check the selected provider in 9router.')))
        raise SystemExit(1)

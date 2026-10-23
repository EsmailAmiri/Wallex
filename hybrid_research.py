"""Offline-only hybrid research. No exchange adapter, scheduler or real orders.

Prices and fills are hypothetical. Models are frozen within each time fold.
The historical clock here never authorizes an active challenge session.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import warnings
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypedDict

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.neural_network import MLPClassifier
from sklearn.preprocessing import StandardScaler

import challenge_rules as rules
from engine import Account, BAR_SECONDS, DEFAULTS, new_account, validate
from ta_indicators import snapshot

VERSION = 'hybrid-offline-v3'
HORIZON = 16
SEED = 42
THRESHOLDS = (.50, .58, .65)
FEATURES = ('ret1', 'ret4', 'ret12', 'ema_gap', 'ema_distance', 'rsi',
            'adx', 'di_gap', 'atr_pct', 'relative_volume', 'range_pct')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def config_for(symbol, slippage=.05):
    return validate({**DEFAULTS, 'symbol': rules.assert_market_allowed(symbol),
                     'source': 'wallex', 'strategy': 'ta_trend', 'model': 'ensemble',
                     'fee': .30, 'slippage': slippage})


def validate_bars(symbol, bars, allow_gaps=False):
    rules.assert_market_allowed(symbol)
    previous = None
    for bar in bars:
        if not all(type(bar[k]) in (float, int) and math.isfinite(bar[k]) for k in ('t', 'o', 'h', 'l', 'c', 'v')):
            raise ValueError('Non-finite or invalid OHLCV')
        if (bar['t'] != int(bar['t']) or min(bar[k] for k in ('o', 'h', 'l', 'c')) <= 0
                or bar['v'] < 0 or bar['l'] > min(bar['o'], bar['c'])
                or bar['h'] < max(bar['o'], bar['c']) or bar['l'] > bar['h']):
            raise ValueError('Invalid OHLCV range')
        if previous is not None:
            delta = bar['t'] - previous
            if delta <= 0 or delta % BAR_SECONDS or (not allow_gaps and delta != BAR_SECONDS):
                raise ValueError('Missing, repeated or unordered closed bar')
        previous = bar['t']


def feature_row(history):
    """Identical causal calculation for fit and inference; input ends at signal close."""
    tech = snapshot(history)
    close = history[-1]['c']
    volume = sum(b['v'] for b in history[-20:]) / 20
    x = [100 * (close / history[-1-lag]['c'] - 1) for lag in (1, 4, 12)]
    x += [100 * (tech['ema_fast'] / tech['ema_slow'] - 1),
          100 * (close / tech['ema_fast'] - 1), tech['rsi'], tech['adx'],
          tech['plus_di'] - tech['minus_di'], tech['atr_pct'],
          history[-1]['v'] / volume if volume else 0,
          100 * (history[-1]['h'] - history[-1]['l']) / close]
    if not np.isfinite(x).all():
        raise ValueError('Invalid features')
    # Same deterministic ta_trend candidate, without the legacy next-close scorer.
    eligible = (tech['ema_fast'] > tech['ema_slow'] and close > tech['ema_fast']
                and tech['adx'] >= 22 and tech['plus_di'] > tech['minus_di']
                and 45 <= tech['rsi'] <= 78 and 4 * tech['atr_pct'] > 1.2 * .70)
    return dict(x=x, eligible=bool(eligible),
                exit=bool(tech['ema_fast'] < tech['ema_slow'] or close < tech['ema_slow']))


def make_rows(symbol, bars):
    validate_bars(symbol, bars, allow_gaps=True)
    rows, segment_start = {}, 0
    for i in range(1, len(bars)):
        if bars[i]['t'] - bars[i-1]['t'] != BAR_SECONDS:
            segment_start = i
        if i-segment_start >= 60:
            rows[i] = feature_row(bars[max(segment_start, i-150):i])
    return rows


def isolated_label(symbol, bars, rows, entry, config=None):
    """Meta-label: next-open entry; identical close-risk/next-open exit rules.

    Label end is conservatively the full horizon, even for an earlier exit.
    Caller must purge this whole interval across every temporal boundary.
    """
    if entry + HORIZON > len(bars):
        raise ValueError('Incomplete label')
    if any(j not in rows for j in range(entry, entry+HORIZON)):
        raise ValueError('Label crosses missing data or warmup')
    config = config or config_for(symbol)
    account = new_account(config, bars[entry]['t'])
    account.execute('BUY', bars[entry]['o'], config, 'isolated label entry', bars[entry]['t'])
    for j in range(entry, entry + HORIZON):
        bar = bars[j]
        opening_risk = account.risk(bar['o'], config)
        risk = account.pending_exit or opening_risk
        account.pending_exit = ''
        if j > entry and (risk or rows[j]['exit']):
            account.execute('SELL', bar['o'], config, 'label next-open exit', bar['t'])
            break
        account.pending_exit = account.risk(bar['c'], config) or ''
    if account.quantity:
        bar = bars[entry + HORIZON - 1]
        account.execute('SELL', bar['c'], config, 'label horizon close', bar['t'] + BAR_SECONDS)
    pnl = account.cash - rules.CREDIT_TMN
    return int(pnl > 0), pnl, bars[entry + HORIZON - 1]['t'] + BAR_SECONDS


def samples_for(markets, rows):
    samples = []
    for symbol, bars in markets.items():
        for i in range(60, len(bars) - HORIZON + 1):
            if i not in rows[symbol] or not rows[symbol][i]['eligible']:
                continue
            if any(j not in rows[symbol] for j in range(i, i+HORIZON)):
                continue
            label, pnl, end = isolated_label(symbol, bars, rows[symbol], i)
            samples.append(dict(symbol=symbol, index=i, t=bars[i]['t'], end=end,
                                x=rows[symbol][i]['x'], y=label, pnl=pnl))
    return sorted(samples, key=lambda s: (s['t'], s['symbol']))


def temporal_samples(samples, start, end):
    # Strict end: label close on a boundary is not train/calibration evidence.
    return [s for s in samples if start <= s['t'] < end and s['end'] < end]


@dataclass
class FrozenScorer:
    scaler: object
    models: dict
    calibrators: dict
    manifest: dict

    def score(self, x):
        z = np.clip(self.scaler.transform([x]), -8, 8)
        result = {}
        for name, model in self.models.items():
            raw = float(model.predict_proba(z)[0, 1])
            logit = math.log(max(1e-6, raw) / max(1e-6, 1-raw))
            result[name] = float(self.calibrators[name].predict_proba([[logit]])[0, 1])
        return result


def fit_scorer(train, calibration):
    for name, data, minimum in (('train', train, 60), ('calibration', calibration, 20)):
        counts = {label: sum(s['y'] == label for s in data) for label in (0, 1)}
        if len(data) < minimum or min(counts.values()) < 5:
            raise ValueError(f'insufficient_evidence:{name}:{counts}')
    if max(s['end'] for s in train) >= min(s['t'] for s in calibration):
        raise ValueError('Overlapping train/calibration labels')
    scaler = StandardScaler().fit([s['x'] for s in train])
    x = np.clip(scaler.transform([s['x'] for s in train]), -8, 8)
    y = [s['y'] for s in train]
    models = dict(logistic=LogisticRegression(C=1, max_iter=500, random_state=SEED),
                  mlp=MLPClassifier(hidden_layer_sizes=(16,), alpha=1, solver='lbfgs',
                                    max_iter=1000, tol=.001, random_state=SEED, early_stopping=False))
    # No random validation or online refit. Every model sees the same causal samples.
    calibrators, fit_warnings = {}, {}
    cx = np.clip(scaler.transform([s['x'] for s in calibration]), -8, 8)
    for name, model in models.items():
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            model.fit(x, y)
        fit_warnings[name] = [type(w.message).__name__ for w in caught]
        p = np.clip(model.predict_proba(cx)[:, 1], 1e-6, 1-1e-6)
        calibrators[name] = LogisticRegression(C=1, random_state=SEED).fit(
            np.log(p / (1-p)).reshape(-1, 1), [s['y'] for s in calibration])
    manifest = dict(version=VERSION, feature_names=FEATURES, seed=SEED, horizon=HORIZON,
                    train_n=len(train), calibration_n=len(calibration),
                    train_last_label=max(s['end'] for s in train),
                    calibration_first=min(s['t'] for s in calibration),
                    calibration_last_label=max(s['end'] for s in calibration),
                    train_hash=digest(train), calibration_hash=digest(calibration),
                    fit_warnings=fit_warnings,
                    models={k: v.get_params() for k, v in models.items()})
    manifest['id'] = digest(manifest)
    return FrozenScorer(scaler, models, calibrators, manifest)


class EntryState(TypedDict, total=False):
    symbol: str
    x: list[float]
    eligible: bool
    scores: dict
    accepted: bool
    reason: str
    features: dict
    vote: dict


def entry_graph(scorer, variant, threshold, voter=None):
    """Pure entry graph. Forced exits are outside this graph and never call voter."""
    from langgraph.graph import StateGraph, START, END

    if variant not in ('rules', 'logistic', 'mlp', 'both') or threshold not in THRESHOLDS:
        raise ValueError('Unregistered experiment')

    def policy(state):
        rules.assert_market_allowed(state['symbol'])
        return {'accepted': False, 'reason': 'no_candidate'}

    def score(state):
        scores = scorer.score(state['x']) if variant != 'rules' else {}
        accepted = variant == 'rules' or (scores[variant] >= threshold if variant != 'both'
                                         else min(scores.values()) >= threshold)
        return dict(scores=scores, accepted=accepted, reason='quant_pass' if accepted else 'quant_veto')

    def veto(state):
        if not voter:
            return {}
        vote = voter({**state['features'], 'scores': state['scores']})
        accepted = (vote['status'] == 'ok' and vote['approve'] and vote['confidence'] >= 56)
        return dict(vote=vote, accepted=accepted, reason='llm_pass' if accepted else 'llm_veto')

    def final(state):
        rules.assert_market_allowed(state['symbol'])
        return {}

    graph = StateGraph(EntryState)
    for name, node in (('policy', policy), ('score', score), ('veto', veto), ('final', final)):
        graph.add_node(name, node)
    graph.add_edge(START, 'policy')
    graph.add_conditional_edges('policy', lambda s: 'score' if s['eligible'] else END)
    graph.add_conditional_edges('score', lambda s: 'veto' if s['accepted'] else END)
    graph.add_edge('veto', 'final')
    graph.add_edge('final', END)
    # No live resume/checkpointer: these are independent offline pure decisions.
    return graph.compile()


def simulate(symbol, bars, rows, start, end, graph=None, config=None):
    """[start,end) historical window, at most 72h. Every fill is hypothetical."""
    config = validate(config or config_for(symbol))
    if config['symbol'] != rules.assert_market_allowed(symbol):
        raise ValueError('Config symbol mismatch')
    selected = [i for i in range(60, len(bars)) if start <= bars[i]['t'] < end]
    if not selected or end - start > rules.DURATION_SECONDS:
        raise ValueError('Empty or excessive historical window')
    account = new_account(config, start)
    trades, decisions, curve = [], [], []
    entered = None
    for i in selected:
        bar = bars[i]
        opening_risk = account.risk(bar['o'], config)
        risk = account.pending_exit or opening_risk
        account.pending_exit = ''
        if i not in rows:
            risk = risk or 'missing data/warmup: first observed open exit'
        decision = dict(t=bar['t'], index=i, action='HOLD')
        if account.quantity:
            if risk or rows[i]['exit']:
                trade = account.execute('SELL', bar['o'], config, risk or 'technical next-open exit', bar['t'])
                trades.append(trade)
                decision.update(action='SELL', reason=risk or 'technical_exit')
        elif not account.halted and bar['t'] < account.expires_at and not risk:
            cooldown = account.last_exit_time and bar['t'] - account.last_exit_time < 2 * BAR_SECONDS
            if graph is not None and i in rows and rows[i]['eligible'] and not cooldown:
                x = rows[i]['x']
                features = dict(rsi=x[5], adx=x[6], atr_pct=x[8],
                                trend_pct=x[3], dist_from_fast_pct=x[4],
                                round_trip_cost_pct=2*(config['fee']+config['slippage']),
                                horizon_bars=HORIZON, normalized_features=dict(zip(FEATURES, x)))
                result = graph.invoke(dict(symbol=symbol, x=x, eligible=True, features=features))
                decision.update({k: result[k] for k in ('scores', 'reason', 'vote') if k in result})
                if result['accepted']:
                    trade = account.execute('BUY', bar['o'], config, 'offline hybrid next-open entry', bar['t'])
                    trades.append(trade)
                    decision['action'] = 'BUY'
                    entered = i
        close_risk = account.risk(bar['c'], config)
        # Explicit bounded-horizon research strategy, matching the isolated labels.
        if account.quantity and entered is not None and i-entered+1 >= HORIZON:
            trade = account.execute('SELL', bar['c'], config, 'predeclared horizon close', bar['t']+BAR_SECONDS)
            trades.append(trade)
            decision['horizon_close'] = True
        elif close_risk and account.quantity:
            account.pending_exit = close_risk
        decisions.append(decision)
        curve.append(dict(t=bar['t']+BAR_SECONDS, equity=account.equity(bar['c'])))
    last = bars[selected[-1]]
    if account.quantity:
        trades.append(account.execute('SELL', last['c'], config, 'terminal hypothetical TMN settlement',
                                      min(end, last['t']+BAR_SECONDS)))
        curve[-1]['equity'] = account.cash
    account.risk(last['c'], config)
    sells = [t for t in trades if t and t['action'] == 'SELL']
    return dict(symbol=symbol, mode='offline_research', real_orders=False, config=config,
                from_t=start, until_t=end, policy_hash=rules.RULE_HASH,
                account=asdict(account), decisions=decisions, trades=trades, curve=curve,
                metrics=dict(net_return_pct=100*(account.cash/rules.CREDIT_TMN-1),
                             pnl_tmn=account.cash-rules.CREDIT_TMN, fees_tmn=account.fees,
                             max_drawdown_pct=account.drawdown, round_trips=len(sells),
                             win_rate_pct=100*sum(t['pnl'] > 0 for t in sells)/len(sells) if sells else None))


class ResearchVotes:
    """Isolated immutable experiment cache. Replay NEVER calls a provider.

    Failures are cached too. Identity includes exact prompt and public payload.
    No account settings/keys, model responses or exception bodies are exported.
    """
    def __init__(self, path, mode='replay', max_calls=60, config=None):
        from wallex_settings import settings
        if mode not in ('replay', 'live'):
            raise ValueError('Invalid vote mode')
        self.config = config if config is not None else settings()
        self.mode, self.max_calls, self.calls = mode, max_calls, 0
        self.path = Path(path)
        if mode == 'replay' and not self.path.exists():
            raise ValueError('Missing replay votes; no network fallback')
        if mode == 'live':
            if not self.config.get('LLM_API_KEY') or not self.config.get('LLM_MODEL'):
                raise ValueError('Configure independent LLM credentials locally')
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as con, con:
            if mode == 'live':
                con.execute('CREATE TABLE IF NOT EXISTS votes (key TEXT PRIMARY KEY, value TEXT NOT NULL)')

    def __call__(self, features):
        import time
        from llm_gateway import SYSTEM, request_vote, validate_vote, endpoint
        safe_config = self.config
        identity = dict(version=VERSION, model=safe_config.get('LLM_MODEL', ''),
                        endpoint_sha256=digest(endpoint(safe_config)), prompt_sha256=digest(SYSTEM),
                        features=features)
        key = digest(identity)
        with closing(sqlite3.connect(self.path)) as con:
            row = con.execute('SELECT value FROM votes WHERE key=?', (key,)).fetchone()
            if row:
                return json.loads(row[0])
        if self.mode == 'replay':
            raise ValueError('Missing exact replay vote; no network fallback')
        if self.calls >= self.max_calls:
            raise ValueError('Experiment call budget exhausted; evaluation incomplete')
        self.calls += 1
        started = time.monotonic()
        try:
            vote = validate_vote(request_vote({**safe_config, 'LLM_TIMEOUT_SECONDS': '8'}, features))
            vote = {**vote, 'status': 'ok'}
        except Exception:
            vote = dict(approve=False, confidence=0, reason_code='uncertain', status='unavailable')
        vote.update(request_sha256=key, prompt_sha256=identity['prompt_sha256'],
                    model=identity['model'], elapsed_ms=round(1000*(time.monotonic()-started)))
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute('INSERT OR IGNORE INTO votes VALUES (?,?)', (key, json.dumps(vote, sort_keys=True)))
            # Return canonical stored winner if another process inserted first.
            return json.loads(con.execute('SELECT value FROM votes WHERE key=?', (key,)).fetchone()[0])

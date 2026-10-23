"""Score-oriented OFFLINE experiment. No account tools or exchange orders.

Hourly regime + 15m pullback; expected NET return regressors + LLM veto.
All historical data is exploratory, including the previously seen benchmark.
"""
import argparse
import importlib.metadata
import json
import math
import os
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict
from urllib.request import Request, build_opener

os.environ['LANGSMITH_TRACING'] = 'false'
os.environ['LANGCHAIN_TRACING_V2'] = 'false'
import joblib
import numpy as np
from sklearn.linear_model import Ridge
from sklearn.neural_network import MLPRegressor
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.preprocessing import StandardScaler
import hybrid_research as h
from run_hybrid_research import cached_features, load_data, fold_schedule, summarize, write
from llm_gateway import endpoint, NoRedirect, validate_vote

ROOT = Path(__file__).resolve().parent
VERSION = 'challenge-expected-return-v2'
VARIANTS = ('ridge', 'mlp', 'tree', 'ensemble')
MARGINS = (0., .15)
PROMPT = '''You may ONLY veto an existing proposed long SPOT entry. You cannot create
an entry, change risk, order size, exits or policy. Inputs contain past closed-bar
market features and estimated NET trade returns in percentage points, not calibrated
probabilities. Costs are already subtracted from the forecasts. Assess hourly trend,
15-minute pullback, overextension, disagreement of models and weak evidence.
ATR is a volatility measure, not proof of direction or expected profit. Prefer rejection
if evidence is weak. Never invent news, account state or current prices.
Return JSON only: {"approve": boolean, "confidence": number 0..100,
"reason_code": "trend" | "weak_edge" | "uncertain" | "costs"}.'''


def hourly_context(bars):
    """Only complete, contiguous closed 1h groups; reset history across gaps."""
    groups = {}
    for bar in bars:
        groups.setdefault(bar['t']//3600*3600, []).append(bar)
    result, history = {}, []
    previous = None
    for t, group in sorted(groups.items()):
        if [b['t'] for b in group] != list(range(t, t+3600, 900)):
            history, previous = [], None
            continue
        if previous is not None and t-previous != 3600:
            history = []
        history.append(dict(t=t,o=group[0]['o'],c=group[-1]['c'],h=max(b['h'] for b in group),
                            l=min(b['l'] for b in group),v=sum(b['v'] for b in group)))
        previous = t
        if len(history) >= 60:
            tech = h.snapshot(history[-150:])
            result[t+3600] = dict(trend=100*(tech['ema_fast']/tech['ema_slow']-1),
                adx=tech['adx'], bullish=tech['ema_fast']>tech['ema_slow'] and tech['plus_di']>tech['minus_di'])
    return result


def strategy_rows(bars, base):
    context = hourly_context(bars)
    rows = {}
    for i,row in base.items():
        t = bars[i]['t']
        hour = context.get(t//3600*3600)
        if hour is None:
            continue
        x = row['x']
        # At most one scheduled opportunity per hour. No volume/activity target.
        candidate = (t % 3600 == 0 and hour['bullish'] and hour['adx'] >= 20
                     and 40 <= x[5] <= 68 and -.5 <= x[4] <= 1.0 and 4*x[8] > .70)
        rows[i] = dict(x=x+[hour['trend'],hour['adx']],eligible=bool(candidate),exit=not hour['bullish'])
    return rows


@dataclass
class ReturnScorer:
    scaler: object
    models: dict
    bias: dict
    manifest: dict

    def score(self, x):
        z = np.clip(self.scaler.transform([x]),-8,8)
        scores = {name:float(model.predict(z)[0]+self.bias[name]) for name,model in self.models.items()}
        scores['ensemble'] = float(np.median(list(scores.values())))
        if not all(math.isfinite(v) for v in scores.values()):
            raise ValueError('Invalid net-return forecast')
        return scores


def fit_returns(train, calibration):
    if len(train)<100 or len(calibration)<20:
        raise ValueError('insufficient_evidence')
    if max(s['end'] for s in train) >= min(s['t'] for s in calibration):
        raise ValueError('Temporal overlap')
    scaler = StandardScaler().fit([s['x'] for s in train])
    x = np.clip(scaler.transform([s['x'] for s in train]),-8,8)
    cx = np.clip(scaler.transform([s['x'] for s in calibration]),-8,8)
    # Every label is one isolated 20%-allocation trade on a fixed 5M-TMN baseline.
    y = np.array([s['pnl']/1_000_000*100 for s in train])
    cy = np.array([s['pnl']/1_000_000*100 for s in calibration])
    models = dict(ridge=Ridge(alpha=10),
        mlp=MLPRegressor(hidden_layer_sizes=(16,),alpha=1,solver='lbfgs',max_iter=1000,
                         tol=.001,random_state=42,early_stopping=False),
        tree=HistGradientBoostingRegressor(max_iter=100,max_leaf_nodes=7,max_depth=3,
               min_samples_leaf=30,l2_regularization=10,early_stopping=False,random_state=42))
    bias, messages = {}, {}
    for name,model in models.items():
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            model.fit(x,y)
        messages[name] = [type(w.message).__name__ for w in caught]
        bias[name] = float(np.mean(cy-model.predict(cx)))
    manifest = dict(version=VERSION,train_n=len(train),calibration_n=len(calibration),
        train_hash=h.digest(train),calibration_hash=h.digest(calibration),fit_warnings=messages,
        train_label_last=max(s['end'] for s in train),calibration_first=min(s['t'] for s in calibration),
        bias=bias,model_params={k:v.get_params() for k,v in models.items()})
    return ReturnScorer(scaler,models,bias,manifest)


class Voter:
    """Portable exact-payload replay, no .env or network required for replay."""
    def __init__(self,path,mode,max_calls=48,model=None,timeout=25):
        if not 1 <= timeout <= 30:raise ValueError('Invalid timeout')
        self.timeout=timeout
        self.path,self.mode,self.max_calls,self.calls = Path(path),mode,max_calls,0
        self.payload = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else None
        if mode == 'replay':
            if self.payload is None or self.payload['prompt_sha256'] != h.digest(PROMPT):
                raise ValueError('Missing/mismatched immutable vote bundle')
            self.config = None
        elif mode == 'live':
            from wallex_settings import settings
            self.config = settings()
            if model:self.config={**self.config,'LLM_MODEL':model}
            if not self.config['LLM_API_KEY'] or not self.config['LLM_MODEL']:
                raise ValueError('Independent LLM credentials not configured')
            identity = dict(prompt_sha256=h.digest(PROMPT),model=self.config['LLM_MODEL'],
                            endpoint_sha256=h.digest(endpoint(self.config)),timeout_seconds=timeout)
            if self.payload and any(self.payload[k]!=v for k,v in identity.items()):
                raise ValueError('Existing bundle belongs to different prompt/model/endpoint')
            self.payload = self.payload or dict(**identity,votes={})
        else:
            raise ValueError('Unknown LLM mode')

    def __call__(self,features):
        key = h.digest(features)
        if key in self.payload['votes']:
            return self.payload['votes'][key]
        if self.mode == 'replay':
            raise ValueError('Missing replay vote; network forbidden')
        if self.calls >= self.max_calls:
            raise ValueError('Call budget exhausted: evaluation incomplete')
        self.calls += 1
        body = dict(model=self.config['LLM_MODEL'],temperature=0,max_tokens=256,stream=False,
                    response_format={'type':'json_object'},messages=[dict(role='system',content=PROMPT),
                    dict(role='user',content=json.dumps(features,sort_keys=True,allow_nan=False))])
        try:
            req = Request(endpoint(self.config),data=json.dumps(body).encode(),headers={
                'Authorization':'Bearer '+self.config['LLM_API_KEY'],'Content-Type':'application/json'})
            with build_opener(NoRedirect()).open(req,timeout=self.timeout) as response:
                raw = response.read(65537)
            if len(raw)>65536:
                raise ValueError('Oversized response')
            content = json.loads(raw)['choices'][0]['message']['content'].strip()
            if content.startswith('```json') and content.endswith('```'):
                content = content[7:-3].strip()
            vote = dict(**validate_vote(json.loads(content)),status='ok')
        except Exception:
            vote = dict(approve=False,confidence=0,reason_code='uncertain',status='unavailable')
        vote.update(request_sha256=h.digest(dict(prompt=self.payload['prompt_sha256'],
                    model=self.payload['model'],endpoint=self.payload['endpoint_sha256'],
                    timeout_seconds=self.timeout,features=features)))
        self.payload['votes'][key] = vote
        temporary = self.path.with_suffix('.tmp')
        with temporary.open('w',encoding='utf-8') as stream:
            stream.write(json.dumps(self.payload,sort_keys=True,allow_nan=False,ensure_ascii=False))
            stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,self.path)  # Commit each response/failure before ledger use.
        return vote


class State(TypedDict,total=False):
    symbol:str
    x:list
    features:dict
    scores:dict
    accepted:bool
    reason:str
    vote:dict


def graph_for(scorer,variant,margin,voter=None):
    from langgraph.graph import StateGraph,START,END
    if variant not in (*VARIANTS,'rules') or margin not in MARGINS:
        raise ValueError('Unregistered trial')
    def quant(s):
        h.rules.assert_market_allowed(s['symbol'])
        scores = scorer.score(s['x']) if variant!='rules' else {}
        accepted = variant=='rules' or scores[variant]>margin
        return dict(scores=scores,accepted=accepted,reason='positive_net_ev' if accepted else 'net_ev_veto')
    def veto(s):
        payload = dict(**s['features'],expected_net_return_pct=s['scores'],
                       hourly_trend_pct=s['x'][-2],hourly_adx=s['x'][-1])
        vote = voter(payload)
        return dict(vote=vote,accepted=vote['status']=='ok' and vote['approve'] and vote['confidence']>=56,
                    reason='llm_veto' if not vote['approve'] else 'llm_review')
    def final(s):
        h.rules.assert_market_allowed(s['symbol'])
        return {}
    g = StateGraph(State)
    g.add_node('quant',quant);g.add_node('veto',veto);g.add_node('final',final)
    g.add_edge(START,'quant')
    g.add_conditional_edges('quant',lambda s:'veto' if s['accepted'] and voter else 'final')
    g.add_edge('veto','final');g.add_edge('final',END)
    return g.compile()


def run(markets,rows,fold,graph,fee=.30,slippage=.05):
    result = {}
    for symbol,bars in markets.items():
        cfg = h.config_for(symbol,slippage);cfg['fee']=fee
        result[symbol] = h.simulate(symbol,bars,rows[symbol],fold['start'],fold['end'],graph,cfg)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,default=ROOT/'reports/challenge-ev-v4')
    p.add_argument('--llm',choices=('live','replay'))
    p.add_argument('--model',help='Explicit experimental LLM model; never changes .env')
    args = p.parse_args()
    out = args.output
    h.HORIZON = 48  # Isolated CLI process; never used by the running server.
    markets,hashes = load_data(ROOT/'data/hybrid-history-60d')
    code = {name:h.hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
            ('challenge_research.py','hybrid_research.py','engine.py','ta_indicators.py','llm_gateway.py',
             'run_hybrid_research.py','challenge_rules.py','competition.py')}
    if args.llm:
        study = json.loads((out/'study.json').read_text(encoding='utf-8'))
        if study['data']!=hashes or study['code']!=code:
            raise ValueError('Frozen study data/code changed')
        if args.llm=='live' and (out/'llm-summary.json').exists():
            raise ValueError('Already evaluated; use replay')
    else:
        if (out/'study.json').exists():
            raise ValueError('Preserve prior study; use a new output path')
        out.mkdir(parents=True,exist_ok=True)
        study = dict(version=VERSION,data=hashes,code=code,policy_hash=h.rules.RULE_HASH,
            folds=fold_schedule(markets,True),horizon=48,variants=VARIANTS,margins=MARGINS,
            llm_window='first development fold, 72h, BTC and ETH; first eligible candidate per UTC day; exploratory ablation',
            llm_comparators=['selected','rules','mlp'],
            limitations=['Historical exploratory data, no fresh holdout','Hypothetical next-open fills',
                          'Independent 5M-TMN accounts, NOT combined portfolio',
                          '0.30% historical fee scenario, not current account verification'],
            packages={k:importlib.metadata.version(k) for k in ('scikit-learn','langgraph','ta')})
        write(out/'study.json',study)
    base = cached_features(markets,hashes)
    rows = {}
    for symbol,bars in markets.items():
        rows[symbol] = strategy_rows(bars,base[symbol])
        print(json.dumps(dict(symbol=symbol,candidates=sum(r['eligible'] for r in rows[symbol].values()))),flush=True)
    if args.llm:
        fold = study['folds'][0]
        selection = json.loads((out/fold['name']/'selection.json').read_text())
        scorer = joblib.load(out/fold['name']/'model.joblib')
        selected = {s:markets[s] for s in ('BTCTMN','ETHTMN')}
        window = dict(start=fold['start'],end=fold['end'])
        # Fixed diagnostic opportunity schedule shared by baseline and LLM.
        # First candidate per UTC date is selected causally, before model/LLM vote.
        limited = {}
        for symbol,bars in selected.items():
            days=set();limited[symbol]={}
            for i,row in rows[symbol].items():
                row=dict(row)
                if window['start'] <= bars[i]['t'] < window['end'] and row['eligible']:
                    day=bars[i]['t']//86400
                    row['eligible']=day not in days;days.add(day)
                limited[symbol][i]=row
        voter = Voter(out/'votes.json',args.llm,max_calls=144,model=args.model)
        summaries = {}
        for variant in study['llm_comparators']:
            v,m = (selection['variant'],selection['margin']) if variant=='selected' else (variant,0.)
            report = run(selected,limited,window,graph_for(scorer,v,m,voter))
            result = dict(reports=report,summary=summarize(report),real_orders=False)
            target = out/f'hybrid-{variant}.json'
            if args.llm=='replay':
                if h.digest(result)!=h.digest(json.loads(target.read_text(encoding='utf-8'))):
                    raise ValueError('Replay mismatch')
            else:
                baseline = run(selected,limited,window,graph_for(scorer,v,m))
                write(out/f'llm-baseline-{variant}.json',baseline)
                write(target,result)
            summaries[variant]=result['summary']
            print(json.dumps(dict(variant=variant,provider_calls=voter.calls,**result['summary'])),flush=True)
        if args.llm=='live':
            write(out/'llm-summary.json',dict(summaries=summaries,provider_calls=voter.calls,promoted=False))
            if not (out/'votes.json').exists():write(out/'votes.json',voter.payload)
        return
    samples = h.samples_for(markets,rows)
    summaries = []
    for fold in study['folds']:
        folder = out/fold['name'];folder.mkdir()
        train = h.temporal_samples(samples,0,fold['calibration_start'])
        calibration = h.temporal_samples(samples,fold['calibration_start'],fold['tuning_start'])
        try:
            scorer = fit_returns(train,calibration)
        except ValueError as exc:
            if str(exc)!='insufficient_evidence':raise
            write(folder/'selection.json',dict(status='insufficient_evidence'))
            summaries.append(dict(fold=fold['name'],status='insufficient_evidence'));continue
        joblib.dump(scorer,folder/'model.joblib');write(folder/'model-manifest.json',scorer.manifest)
        tune = dict(start=fold['tuning_start'],end=fold['start'])
        trials = [dict(variant=v,margin=m,**summarize(run(markets,rows,tune,graph_for(scorer,v,m))))
                  for v in VARIANTS for m in MARGINS]
        best = max(trials,key=lambda t:(t['mean_independent_return_pct'],-t['total_independent_round_trips']))
        write(folder/'selection.json',dict(status='fitted',**best,trials=trials))
        results = {}
        for variant in ('rules',*VARIANTS,'selected'):
            v,m = (best['variant'],best['margin']) if variant=='selected' else (variant,0.)
            report = run(markets,rows,fold,graph_for(scorer,v,m))
            write(folder/f'{variant}.json',report);results[variant]=summarize(report)
        g = graph_for(scorer,best['variant'],best['margin'])
        # Same selected decisions: distinguish signal losses from cost drag.
        results['selected_zero_friction'] = summarize(run(markets,rows,fold,g,0.,0.))
        results['selected_slippage_stress'] = summarize(run(markets,rows,fold,g,.30,.15))
        summary = dict(fold=fold['name'],selected=dict(variant=best['variant'],margin=best['margin']),
                       tuning_profitable=best['mean_independent_return_pct']>0,results=results)
        summaries.append(summary);write(folder/'summary.json',summary)
        print(json.dumps(dict(fold=fold['name'],selected=summary['selected'],
                             result=results['selected']['mean_independent_return_pct'])),flush=True)
    write(out/'summary.json',dict(folds=summaries,promoted=False,real_orders=False))


if __name__=='__main__':
    main()

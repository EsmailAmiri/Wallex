"""Explicit offline research CLI; never starts the server or an exchange session."""
import argparse
import importlib.metadata
import json
import os
import inspect
from pathlib import Path

# Local graph execution only; do not enable external tracing from inherited env.
os.environ['LANGCHAIN_TRACING_V2'] = 'false'
os.environ['LANGSMITH_TRACING'] = 'false'

import joblib
import hybrid_research as h

ROOT = Path(__file__).resolve().parent


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def cached_features(markets, hashes):
    identity = h.digest(dict(data=hashes, feature_names=h.FEATURES,
                             source=inspect.getsource(h.make_rows)+inspect.getsource(h.feature_row)
                                    +inspect.getsource(h.snapshot)))
    path = ROOT/'data/hybrid-feature-cache'/f'{identity}.json'
    if path.exists():
        payload = json.loads(path.read_text(encoding='utf-8'))
        if payload['identity'] != identity or payload['rows_sha256'] != h.digest(payload['rows']):
            raise ValueError('Feature cache integrity mismatch')
        return {s: {int(i): row for i,row in rows.items()} for s,rows in payload['rows'].items()}
    rows = {}
    for symbol,bars in markets.items():
        rows[symbol] = h.make_rows(symbol,bars)
        print(json.dumps(dict(feature_market=symbol,eligible_bars=sum(r['eligible'] for r in rows[symbol].values()))),flush=True)
    serial = {s: {str(i):row for i,row in values.items()} for s,values in rows.items()}
    path.parent.mkdir(parents=True,exist_ok=True)
    write(path,dict(identity=identity,rows_sha256=h.digest(serial),rows=serial))
    return rows


def load_data(directory=ROOT/'research_data'):
    markets, hashes = {}, {}
    for path in sorted(directory.glob('*-15m.json')):
        data = json.loads(path.read_text(encoding='utf-8'))
        symbol = h.rules.assert_market_allowed(data['symbol'])
        if data['timeframe_seconds'] != h.BAR_SECONDS or not symbol.endswith('TMN'):
            raise ValueError('Unsupported research data')
        h.validate_bars(symbol, data['bars'], allow_gaps=True)
        markets[symbol] = data['bars']
        hashes[path.name] = h.digest(data)
    if not markets:
        raise ValueError('No frozen data')
    return markets, hashes


def fold_schedule(markets, extended=False):
    # Use UTC timestamps, not row numbers: market files have different first bars.
    end = min(bars[-1]['t']+h.BAR_SECONDS for bars in markets.values())
    legacy_start = end - 288*h.BAR_SECONDS
    folds = []
    size = 288 if extended else 192
    for n in range(3):
        start = legacy_start - (3-n)*size*h.BAR_SECONDS
        folds.append(dict(name=f'development_{n+1}', start=start, end=start+size*h.BAR_SECONDS))
    folds.append(dict(name='previously_seen_legacy_benchmark', start=legacy_start, end=end))
    for fold in folds:
        fold['calibration_start'] = fold['start']-(576 if extended else 192)*h.BAR_SECONDS
        fold['tuning_start'] = fold['start']-(192 if extended else 96)*h.BAR_SECONDS
    return folds


def evaluate(markets, rows, fold, graph, slippage=.05):
    return {symbol: h.simulate(symbol, bars, rows[symbol], fold['start'], fold['end'], graph,
                              h.config_for(symbol, slippage)) for symbol, bars in markets.items()}


def summarize(reports):
    metrics = {s: r['metrics'] for s, r in reports.items()}
    return dict(per_market=metrics,
                mean_independent_return_pct=sum(m['net_return_pct'] for m in metrics.values())/len(metrics),
                total_independent_round_trips=sum(m['round_trips'] for m in metrics.values()),
                note='Independent 5M-TMN experiments; mean is not a deployable portfolio return.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'reports/hybrid-v3-h16')
    parser.add_argument('--data-dir', type=Path, default=ROOT/'research_data')
    parser.add_argument('--extended', action='store_true', help='Four-day calibration and two-day tuning for longer history')
    parser.add_argument('--horizon',type=int,choices=(16,48),default=16,
                        help='Predeclared four-hour or twelve-hour hypothetical maximum hold')
    parser.add_argument('--llm', choices=('live', 'replay'))
    parser.add_argument('--max-calls', type=int, default=60)
    parser.add_argument('--votes', type=Path, default=ROOT/'data/hybrid-v3-votes.sqlite3')
    args = parser.parse_args()
    h.HORIZON = args.horizon
    markets, data_hashes = load_data(args.data_dir)
    out = args.output.resolve()
    if args.llm:
        plan = json.loads((out/'study.json').read_text(encoding='utf-8'))
        if plan['data_hashes'] != data_hashes or plan['code_hashes'] != code_hashes():
            raise ValueError('Data/code changed since frozen quantitative run')
        if plan['horizon_bars'] != h.HORIZON:
            raise ValueError('Replay/evaluation horizon differs from frozen study')
        if args.llm == 'live' and (out/'llm-summary.json').exists():
            raise ValueError('LLM evaluation already exists; use replay, never replace outcomes')
    else:
        if (out/'study.json').exists():
            raise ValueError('Study already exists; choose a new output path, preserve experiments')
        out.mkdir(parents=True, exist_ok=True)
        plan = dict(version=h.VERSION, mode='offline_research', real_orders=False,
                    data_hashes=data_hashes, code_hashes=code_hashes(),
                    policy_hash=h.rules.RULE_HASH, folds=fold_schedule(markets,args.extended),
                    data_directory=str(args.data_dir.resolve()),
                    feature_names=h.FEATURES, horizon_bars=h.HORIZON,
                    profile=h.config_for(next(iter(markets))),
                    fee_status='Historical scenario 0.30% per side, NOT verification of current account fee',
                    slippage_status='Assumed 0.05% per side; stress 0.15%',
                    trial_budget=dict(variants=['logistic','mlp','both'], thresholds=h.THRESHOLDS,
                                      predeclared_horizons=[16,48],
                                      seeds=[h.SEED], market_selection='none; report all supplied markets'),
                    packages={p: importlib.metadata.version(p) for p in ('scikit-learn','langgraph','ta','numpy','pandas')},
                    data_gaps={symbol: [{'after_t': a['t'], 'next_t': b['t'], 'missing_bars': (b['t']-a['t'])//h.BAR_SECONDS-1}
                                        for a,b in zip(bars,bars[1:]) if b['t']-a['t'] != h.BAR_SECONDS]
                               for symbol,bars in markets.items()},
                    limitations=['All supplied data has been available to prior research.',
                                 'Gaps are not imputed; labels crossing gaps excluded, 60 closed-bar warmup restarts.',
                                 'No fresh untouched holdout exists in this study.',
                                 'Labels and windows overlap; trade/sample counts are not independent evidence.',
                                 'Horizon close is a new offline strategy, not a modification of the live engine.',
                                 'No live latency, order book or real fill simulation.'])
        # Record bounded experiments before looking at evaluation outcomes.
        write(out/'study.json', plan)
    print('Building causal features from frozen public data...', flush=True)
    rows = cached_features(markets,data_hashes)
    print('Building cost-aware candidate labels...', flush=True)
    samples = h.samples_for(markets, rows)
    write(out/'sample-counts.json', dict(total=len(samples), positive=sum(s['y'] for s in samples))) if not args.llm else None
    summaries = []
    voter = h.ResearchVotes(args.votes, args.llm, args.max_calls) if args.llm else None
    for fold in plan['folds']:
        folder = out/fold['name']
        if args.llm:
            selection = json.loads((folder/'selection.json').read_text(encoding='utf-8'))
            if selection.get('status') != 'fitted':
                summaries.append(dict(fold=fold['name'], status=selection['status']))
                continue
            scorer = joblib.load(folder/'scorer.joblib')  # Locally generated trusted artifact only.
            graph = h.entry_graph(scorer, selection['nn_variant'], selection['nn_threshold'], voter)
            reports = evaluate(markets, rows, fold, graph)
            summary = dict(fold=fold['name'], **summarize(reports))
            for symbol, report in reports.items():
                path = folder/f'{symbol}-hybrid.json'
                if args.llm == 'replay':
                    prior = json.loads(path.read_text(encoding='utf-8'))
                    if h.digest(report) != h.digest(prior):
                        raise ValueError(f'Offline replay mismatch: {fold["name"]}/{symbol}')
                else:
                    write(path, report)
            summaries.append(summary)
            print(json.dumps(summary), flush=True)
            continue
        folder.mkdir(parents=True, exist_ok=False)
        train = h.temporal_samples(samples, 0, fold['calibration_start'])
        calibration = h.temporal_samples(samples, fold['calibration_start'], fold['tuning_start'])
        try:
            scorer = h.fit_scorer(train, calibration)
        except ValueError as exc:
            if not str(exc).startswith('insufficient_evidence:'):
                raise
            selection = dict(status='insufficient_evidence', reason=str(exc))
            write(folder/'selection.json', selection)
            summaries.append(dict(fold=fold['name'], **selection))
            print(json.dumps(summaries[-1]), flush=True)
            continue
        joblib.dump(scorer, folder/'scorer.joblib')
        write(folder/'model-manifest.json', scorer.manifest)
        # Threshold selection uses tuning only, never evaluation or old holdout.
        tuning_fold = dict(start=fold['tuning_start'], end=fold['start'])
        trials = []
        for variant in ('logistic', 'mlp', 'both'):
            for threshold in h.THRESHOLDS:
                result = summarize(evaluate(markets, rows, tuning_fold,
                                             h.entry_graph(scorer, variant, threshold)))
                trials.append(dict(variant=variant, threshold=threshold, **result))
        eligible = [t for t in trials if t['variant'] in ('mlp', 'both')]
        best = max(eligible, key=lambda t: (t['mean_independent_return_pct'], -t['total_independent_round_trips']))
        selection = dict(status='fitted', nn_variant=best['variant'], nn_threshold=best['threshold'],
                         tuning_return_pct=best['mean_independent_return_pct'],
                         tuning_profitable=best['mean_independent_return_pct'] > 0,
                         diagnostic_only=True, trials=trials)
        write(folder/'selection.json', selection)
        results = {}
        # Fixed .58 ablations plus preselected NN gate; not selected on eval PnL.
        for variant in ('rules', 'logistic', 'mlp', 'both', 'selected_nn'):
            which = selection['nn_variant'] if variant == 'selected_nn' else variant
            threshold = selection['nn_threshold'] if variant == 'selected_nn' else .58
            reports = evaluate(markets, rows, fold, h.entry_graph(scorer, which, threshold))
            for symbol, report in reports.items():
                write(folder/f'{symbol}-{variant}.json', report)
            results[variant] = summarize(reports)
        # Same chosen gate with adverse slippage, without re-selection.
        results['selected_nn_slippage_stress'] = summarize(evaluate(markets, rows, fold,
            h.entry_graph(scorer, selection['nn_variant'], selection['nn_threshold']), .15))
        comparators = {}
        for symbol, bars in markets.items():
            indices = [i for i in range(60,len(bars)) if fold['start'] <= bars[i]['t'] < fold['end']]
            cfg = h.config_for(symbol)
            account = h.new_account(cfg, fold['start'])
            account.execute('BUY', bars[indices[0]]['o'], cfg, 'buy hold hypothetical', bars[indices[0]]['t'])
            account.execute('SELL', bars[indices[-1]]['c'], cfg, 'terminal hypothetical', fold['end'])
            comparators[symbol] = dict(cash_return_pct=0,
                buy_hold_20pct_return_pct=100*(account.cash/h.rules.CREDIT_TMN-1))
        summary = dict(fold=fold['name'], results=results, comparators=comparators)
        diagnostic = h.temporal_samples(samples, fold['start'], fold['end'])
        scores = [scorer.score(s['x']) for s in diagnostic]
        summary['label_diagnostics'] = dict(n=len(diagnostic), positives=sum(s['y'] for s in diagnostic),
            brier={name: sum((p[name]-s['y'])**2 for p,s in zip(scores,diagnostic))/len(diagnostic)
                   for name in ('logistic','mlp')} if diagnostic else {},
            warning='Overlapping hypothetical candidate labels; not independent executed trades.')
        write(folder/'summary.json', summary)
        summaries.append(summary)
        print(json.dumps(dict(fold=fold['name'], train=len(train), calibration=len(calibration),
                              selected=selection['nn_variant'], threshold=selection['nn_threshold'],
                              evaluation_return=results['selected_nn']['mean_independent_return_pct'])), flush=True)
    result = dict(version=h.VERSION, folds=summaries, real_orders=False, promoted=False,
                  verdict='Research only: fresh future evaluation and operational readiness still required.',
                  llm_mode=args.llm or 'not_evaluated')
    if args.llm == 'replay':
        prior = json.loads((out/'llm-summary.json').read_text(encoding='utf-8'))
        if h.digest(result['folds']) != h.digest(prior['folds']):
            raise ValueError('Summary replay mismatch')
        print('Exact offline LLM replay passed; zero provider calls.', flush=True)
    else:
        write(out/('llm-summary.json' if args.llm else 'quant-summary.json'), result)
    print(json.dumps(dict(real_orders=False, promoted=False, provider_calls=voter.calls if voter else 0)), flush=True)


def code_hashes():
    return {name: h.hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
            for name in ('hybrid_research.py', 'run_hybrid_research.py', 'engine.py',
                         'ta_indicators.py', 'llm_gateway.py', 'challenge_rules.py', 'competition.py')}


if __name__ == '__main__':
    main()

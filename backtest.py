"""Reproducible command-line evaluation, using the same engine as the dashboard."""
import argparse
import csv
import json
import os
from pathlib import Path
from unittest.mock import patch
from engine import DEFAULTS, MODELS, STRATEGIES, SYMBOLS, validate, demo_candles, backtest
from server import checked_bars, fetch_bars, source_bars


def main():
    parser = argparse.ArgumentParser(description='Madar deterministic trading evaluation')
    parser.add_argument('--source', choices=['demo', 'wallex', 'wallex_mcp'], default='demo')
    parser.add_argument('--model', choices=MODELS, default='logistic')
    parser.add_argument('--strategy', choices=STRATEGIES, default='adaptive')
    parser.add_argument('--symbol', choices=SYMBOLS, default='BTCTMN')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--fee', type=float, default=DEFAULTS['fee'], help='Trading fee percent per side')
    parser.add_argument('--slippage', type=float, default=DEFAULTS['slippage'], help='Adverse slippage percent per side')
    parser.add_argument('--csv', type=Path)
    parser.add_argument('--replay', type=Path, help='Re-run a dashboard backtest report with its saved inputs')
    parser.add_argument('--output', type=Path, default=Path('reports/backtest.json'))
    args = parser.parse_args()
    previous = None
    if args.replay:
        os.environ['LLM_MODE'] = 'replay'  # A replay may never issue new LLM requests.
        previous = json.loads(args.replay.read_text(encoding='utf-8'))
        config, bars, source = validate(previous['config']), checked_bars(previous['bars']), previous['source']
    else:
        config = validate({**DEFAULTS, **{key: getattr(args, key) for key in ('source', 'model', 'strategy', 'symbol', 'seed', 'fee', 'slippage')}})
        if args.csv:
            with args.csv.open(encoding='utf-8-sig') as file:
                bars = checked_bars([{k: float(row[k]) for k in ('t', 'o', 'h', 'l', 'c', 'v')} for row in csv.DictReader(file)])
            source = 'csv'
        elif args.source != 'demo':
            bars, source = source_bars(config), args.source
        else:
            bars, source = demo_candles(480, args.seed, args.symbol), 'synthetic'
    if previous and config['model'] == 'hybrid_llm':
        from llm_gateway import ReplayVotes
        reviewer = ReplayVotes(previous['decisions'])
        with patch('llm_gateway.review', new=reviewer):
            report = backtest(bars, config)
        reviewer.assert_finished()
    else:
        report = backtest(bars, config)
    report.update(bars=bars, source=source)
    if previous:
        if report['metrics'] != previous['metrics'] or report['trades'] != previous['trades']:
            raise SystemExit('REPLAY MISMATCH: results differ from the saved report')
        print('REPLAY VERIFIED: metrics and trades match exactly')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report['metrics'], indent=2))
    print('Report:', args.output.resolve())


if __name__ == '__main__':
    main()

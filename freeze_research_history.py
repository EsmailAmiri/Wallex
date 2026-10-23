"""Extend PUBLIC historical research only. Never overwrite the original frozen files.

No account credentials or MCP tools are used. End date is anchored to the existing
frozen dataset, never the host clock. Newer future data is not fetched here.
"""
import argparse
import json
import statistics
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

import challenge_rules as rules

ROOT = Path(__file__).resolve().parent
BAR = 900


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def convert(data, start, end):
    if data.get('s') != 'ok' or len(data.get('t', [])) < 2:
        raise ValueError('Public history unavailable for requested older interval')
    raw = [dict(t=int(t), **{k: float(data[k][i]) for k in ('o','h','l','c','v')})
           for i,t in enumerate(data['t'])]
    differences = [b['t']-a['t'] for a,b in zip(raw,raw[1:])]
    if min(differences) <= 0:
        raise ValueError('Unordered public data')
    interval = int(statistics.median(differences))
    if interval <= 0 or BAR % interval:
        raise ValueError('Unsupported public interval')
    groups = {}
    for bar in raw:
        bucket = bar['t']//BAR*BAR
        groups.setdefault(bucket, []).append(bar)
    result = []
    for t, group in groups.items():
        if t < start or t+BAR > end or [b['t'] for b in group] != list(range(t,t+BAR,interval)):
            continue
        result.append(dict(t=t,o=group[0]['o'],c=group[-1]['c'],h=max(b['h'] for b in group),
                           l=min(b['l'] for b in group),v=sum(b['v'] for b in group)))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--days', type=int, default=60, choices=(30,60,90))
    parser.add_argument('--output', type=Path, default=ROOT/'data/hybrid-history-60d')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    opener = build_opener(NoRedirect())
    for path in sorted((ROOT/'research_data').glob('*-15m.json')):
        original = json.loads(path.read_text(encoding='utf-8'))
        symbol = rules.assert_market_allowed(original['symbol'])
        target = args.output/path.name
        if target.exists():
            print(json.dumps(dict(symbol=symbol,status='preserved_existing_snapshot')), flush=True)
            continue
        end = original['bars'][0]['t']
        first = end - args.days*86400
        collected = {b['t']:b for b in original['bars']}
        cursor = first
        while cursor < end:
            stop = min(cursor+7*86400,end)
            query = urlencode(dict(symbol=symbol,resolution='15',**{'from':cursor,'to':stop}))
            request = Request('https://api.wallex.ir/v1/udf/history?'+query,
                              headers={'User-Agent':'WallexOfflineResearch/1.0'})
            with opener.open(request,timeout=15) as response:
                data = json.loads(response.read(16_000_001))
            bars = convert(data,cursor,stop)
            if not bars:
                raise ValueError('No complete historical bars; stopping rather than fabricating')
            for bar in bars:
                if bar['t'] in collected and collected[bar['t']] != bar:
                    raise ValueError('Conflicting historical data')
                collected[bar['t']] = bar
            print(json.dumps(dict(symbol=symbol,interval_start=cursor,complete_bars=len(bars))),flush=True)
            cursor = stop
        payload = dict(symbol=symbol,timeframe_seconds=BAR,source='wallex-public-rest-historical-extension',
                       original_snapshot=path.name,requested_older_days=args.days,
                       bars=[collected[t] for t in sorted(collected)])
        # Validate but preserve gaps; the research pipeline resets warmup at each gap.
        from hybrid_research import validate_bars, digest
        validate_bars(symbol,payload['bars'],allow_gaps=True)
        target.write_text(json.dumps(payload,separators=(',',':')),encoding='utf-8')
        print(json.dumps(dict(symbol=symbol,bars=len(collected),sha256=digest(payload))),flush=True)


if __name__ == '__main__':
    main()

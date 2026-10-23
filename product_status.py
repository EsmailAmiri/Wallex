"""Curated product evidence and read-only Wallex preflight; no order transport."""
import json
import time
from pathlib import Path
from engine import validate,DEFAULTS,new_account
import challenge_rules as rules
import competition

ROOT=Path(__file__).resolve().parent


def paper_roundtrip():
    config=validate({**DEFAULTS,'symbol':'BTCTMN'})
    account=new_account(config,1)
    buy=account.execute('BUY',100,config,'diagnostic hypothetical BUY',1)
    sell=account.execute('SELL',100,config,'diagnostic hypothetical SELL',2)
    return dict(status='passed' if buy and sell and account.quantity==0 and account.cash>=0 else 'failed',
                mode='paper',exchange_orders_sent=0,final_asset='TMN',fees_applied=account.fees>0)


def release_info():
    path=ROOT/'release'/'evidence.json'
    evidence=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    return dict(version='2.0.0',execution='paper_shadow',real_orders_enabled=False,
        credit_account_identity_verified=False,profitability_proven=False,
        competition=competition.snapshot(),challenge=rules.snapshot(),evidence=evidence,
        order_transport='not_implemented',paper_roundtrip=paper_roundtrip())


def preflight():
    from mcp_link import bridge
    from wallex_settings import settings
    result=dict(checked_at=int(time.time()),endpoint='https://api.wallex.ir/mcp',
        connected=False,markets_read=False,balance_read=False,fee_read=False,
        account_identity_verified=False,real_buy_tested=False,real_sell_tested=False,
        real_orders_sent=0,order_transport='not_implemented',paper_roundtrip=paper_roundtrip(),
        error=None)
    try:
        config=settings()
        if config['WALLEX_MCP_URL']!='https://api.wallex.ir/mcp':
            raise ValueError('Exact official endpoint required')
        bridge.connect()
        result['connected']=bool(bridge.status()['connected'])
        markets=bridge.call('get_markets')
        if not isinstance(markets.get('symbols'),dict) or not markets['symbols']:
            raise ValueError('Valid market registry required')
        result['markets_read']=True
        if config['WALLEX_API_KEY']:
            for name,field in (('get_account_balances','balance_read'),('get_account_fees','fee_read')):
                try:
                    bridge.call(name)
                    result[field]=True
                except Exception:pass
    except Exception:
        result['error']='اتصال رسمی کامل نشد؛ کلید، شبکه و دسترسی خواندن را بررسی کنید.'
    return result


if __name__=='__main__':
    from mcp_link import bridge
    try:
        print(json.dumps(preflight(),ensure_ascii=False))
    finally:bridge.close()

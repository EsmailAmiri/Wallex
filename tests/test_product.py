import json
import tempfile
import threading
from urllib.request import Request,urlopen
from urllib.error import HTTPError
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import server
import competition
import challenge_rules as rules
import product_status
import llm_gateway
from engine import demo_candles,new_account,validate


class ProductTests(unittest.TestCase):
    def test_http_health_origin_and_missing_file_are_safe(self):
        http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(http.server_address[1])
        try:
            with urlopen(base+'/api/health',timeout=3) as response:
                self.assertFalse(json.load(response)['real_orders_enabled'])
            for path,headers,code in (('/api/health',{'Host':'evil.example'},403),
                                      ('/api/health',{'Origin':'https://evil.example'},403),
                                      ('/.env',{},404)):
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(base+path,headers=headers),timeout=3)
                self.assertEqual(error.exception.code,code)
        finally:http.shutdown();http.server_close();thread.join(3)

    def test_paper_roundtrip_is_not_real_order(self):
        result=product_status.paper_roundtrip()
        self.assertEqual(result['status'],'passed')
        self.assertEqual(result['exchange_orders_sent'],0)
        self.assertEqual(result['final_asset'],'TMN')

    def test_preflight_never_calls_financial_tools_or_exports_account(self):
        called=[]
        def call(name):
            called.append(name)
            return {'symbols':{'BTCTMN':{}}} if name=='get_markets' else {'balances':{'private':'secret-balance'}}
        with (patch('mcp_link.bridge.connect'),patch('mcp_link.bridge.status',return_value={'connected':True}),
              patch('mcp_link.bridge.call',side_effect=call),patch('wallex_settings.settings',return_value={
                  'WALLEX_MCP_URL':'https://api.wallex.ir/mcp','WALLEX_API_KEY':'private-key'})):
            result=product_status.preflight()
        self.assertEqual(called,['get_markets','get_account_balances','get_account_fees'])
        self.assertFalse(result['real_buy_tested']);self.assertFalse(result['account_identity_verified'])
        self.assertNotIn('private',json.dumps(result))

    def test_prompt_change_invalidates_vote_identity(self):
        cfg={'LLM_MODEL':'test','LLM_BASE_URL':'http://localhost/v1'}
        first=llm_gateway.request_sha256(cfg,{'rsi':55})
        with patch('llm_gateway.SYSTEM','different prompt'):
            self.assertNotEqual(first,llm_gateway.request_sha256(cfg,{'rsi':55}))

    def test_deadline_does_not_wait_for_model_and_late_entry_is_discarded(self):
        cfg=validate({'source':'wallex','model':'hybrid_llm'})
        bars=demo_candles(120)
        for i,bar in enumerate(bars):bar['t']=rules.START_AT+i*900
        next_bar={**bars[-1],'t':bars[-1]['t']+900}
        draft=dict(session_id='test',revision=0,config=cfg,bars=bars,account=asdict(new_account(cfg,rules.START_AT)),
            decisions=[],trades=[],curve=[],running=True,cycles=0,last_update=None,error=None)
        entered,release=threading.Event(),threading.Event()
        def blocked(*args):
            entered.set();release.wait(3)
            return dict(action='BUY',trade=None,equity=5_000_000)
        with tempfile.TemporaryDirectory() as folder:
            with (patch.object(server,'DATA',Path(folder)),patch.object(server,'STATE',draft),
                  patch.object(server,'source_bars',return_value=bars+[next_bar]),
                  patch.object(server,'step',side_effect=blocked),patch('server.time.time',return_value=rules.START_AT+120*900)):
                worker=threading.Thread(target=server.run_cycle)
                worker.start()
                try:
                    self.assertTrue(entered.wait(2))
                    self.assertTrue(server.deadline_stop(competition.END_AT))
                    self.assertFalse(server.STATE['running'])
                    self.assertTrue(server.STATE['account']['halted'])
                    self.assertFalse(server.deadline_stop(competition.END_AT+1))
                finally:release.set();worker.join(3)
                self.assertEqual(server.STATE['cycles'],0)
                self.assertEqual(server.STATE['trades'],[])
                with server.db() as con:
                    saved=json.loads(con.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
                    self.assertFalse(saved['running'])
                    self.assertEqual(con.execute('SELECT count(*) FROM events').fetchone()[0],1)

    def test_container_router_is_explicit_and_narrow(self):
        cfg={'LLM_BASE_URL':'http://host.docker.internal:20128/v1'}
        with patch.dict('os.environ',{'MADAR_ALLOW_DOCKER_ROUTER':'0'}):
            with self.assertRaises(ValueError):llm_gateway.endpoint(cfg)
        with patch.dict('os.environ',{'MADAR_ALLOW_DOCKER_ROUTER':'1'}):
            self.assertTrue(llm_gateway.endpoint(cfg).endswith('/chat/completions'))
            with self.assertRaises(ValueError):llm_gateway.endpoint({'LLM_BASE_URL':'http://example.com/v1'})


if __name__=='__main__':unittest.main()

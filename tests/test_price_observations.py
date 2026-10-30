"""Additive source evidence cannot change the existing rate/fallback decisions."""
import ast,copy,io,json,pathlib,sys,unittest
from datetime import datetime,timezone
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import price_observations as P
from legacy_price_loop import SOURCE as LEGACY
TOKENS={'MENTE':{'addr':'0x'+'1'*40,'fallback_rate':.02},'MOCA':{'addr':'0x'+'2'*40,'fallback_rate':.01}}
def pair(symbol='MENTE',price='0.02',liquidity='10',address='0x'+'3'*40):
    return {'chainId':'base','dexId':'aerodrome','pairAddress':address,'baseToken':{'address':TOKENS[symbol]['addr']},'quoteToken':{'address':'0x'+'4'*40},'priceUsd':price,'liquidity':{'usd':liquidity}}
class Clock:
    def __init__(self):self.n=0
    def __call__(self):self.n+=1;return '2026-10-30T08:00:'+str(self.n).zfill(2)+'.000Z'
def new_loop():
    raw=(ROOT/'refresh.py').read_text();tree=ast.parse(raw)
    return ast.get_source_segment(raw,next(n for n in tree.body if isinstance(n,ast.For) and ast.get_source_segment(raw,n.target)=='sym, t' and 'list(TOKENS.items())' in ast.get_source_segment(raw,n.iter)))
def execute(source,bs,dx,last=True):
    capture=P.Capture(TOKENS,now=Clock());calls=[]
    def get(url,**kwargs):
        calls.append(url);data=bs if 'blockscout' in url else dx
        if isinstance(data,Exception):raise data
        body=data if isinstance(data,bytes) else json.dumps(data).encode()
        if kwargs.get('price_observation'):kwargs['price_observation'].received(body)
        return json.loads(body)
    ns={'OFFLINE':False,'TOKENS':TOKENS,'RATE':{},'RATE_SRC':{},'DECIMALS':{},
      'STATE':{'last_accepted_rate':{'MENTE':.02,'MOCA':.01} if last else {},'market_rates':{'MENTE':{'2026-10-29':.02},'MOCA':{'2026-10-29':.01}}},
      'get':get,'utcnow':lambda:datetime(2026,10,30,tzinfo=timezone.utc),'MARKET_AGREE':.06,
      'datetime':datetime,'timezone':timezone,'_RATE_OBSERVED':{},'_PRICE_CAPTURE':capture,'print':lambda *a:None}
    exec(source,ns)
    return {k:ns[k] for k in ('RATE','RATE_SRC','DECIMALS','STATE')},calls,capture
class Evidence(unittest.TestCase):
    def test_actual_loop_preserves_provider_order_winners_anchors_and_fallbacks(self):
        cases=[({'exchange_rate':'0.02','decimals':'18'},{'pairs':[pair()]}),
          ({'exchange_rate':'1'},{'pairs':[pair()]}),
          ({'exchange_rate':'0'},{'pairs':[pair(price='0.02'),pair(price='0.021',liquidity='20')]}),
          (OSError('private provider diagnostic'),{'pairs':[pair(),pair(liquidity='10')]}),
          ({'exchange_rate':'garbage'},{'pairs':[]}),
          (OSError('offline'),OSError('offline')),
          ({'exchange_rate':'.02'}, {'pairs':[pair(symbol='MOCA',price='.01')]}),
          ({'exchange_rate':'0'},{'pairs':[pair(price='invalid')]}),
          ({'exchange_rate':'0'},{'pairs':None})]
        for bs,dx in cases:
            for last in (True,False):
                with self.subTest(bs=type(bs).__name__,last=last):
                    old,old_calls,_=execute(LEGACY,bs,dx,last)
                    new,new_calls,capture=execute(new_loop(),bs,dx,last)
                    self.assertEqual(new,old);self.assertEqual(new_calls,old_calls)
                    envelope=capture.envelope(new['RATE'],new['RATE_SRC'])
                    self.assertLess(len(json.dumps(envelope)),65536)
                    self.assertNotIn('private provider diagnostic',json.dumps(envelope))
    def test_numeric_lexeme_rejected_quote_and_actual_pool_identity_are_preserved(self):
        c=P.Capture(TOKENS,now=Clock());a=c.begin('MENTE','blockscout')
        a.received(b'{"exchange_rate":0.020000000000000000000001}');a.finish('rejected','market_reference_disagreement')
        d=c.begin('MENTE','dexscreener');d.received(json.dumps({'pairs':[pair(price='0.0201234567890123456789')]}).encode());d.select_pair(0);d.finish('acquired')
        c.not_queried('MOCA','dexscreener')
        e=c.envelope({'MENTE':.0201234567890123456789,'MOCA':.01},{'MENTE':'dexscreener','MOCA':'fallback'})
        self.assertEqual(e['observations'][0]['price_usd'],'0.020000000000000000000001')
        self.assertEqual(e['observations'][1]['price_usd'],'0.0201234567890123456789')
        self.assertEqual(e['observations'][1]['pair']['address'],pair()['pairAddress'])
        self.assertEqual(e['selections'][0]['original_observation_id'],e['observations'][1]['observation_id'])
        self.assertIsNone(e['observations'][2]['query_started_at']);self.assertIsNone(e['observations'][2]['fetched_at'])
    def test_retained_observation_reuses_original_identity_and_receipt_not_new_export_time(self):
        c=P.Capture(TOKENS,now=Clock());a=c.begin('MENTE','blockscout');a.received(b'{"exchange_rate":"0.02"}');a.finish('acquired')
        original=c.envelope({'MENTE':.02,'MOCA':.01},{'MENTE':'blockscout','MOCA':'fallback'})
        c2=P.Capture(TOKENS,original,now=lambda:'2026-10-30T09:00:00.000Z')
        a=c2.begin('MENTE','blockscout');a.finish('failed','provider_unavailable_or_invalid')
        retained=c2.envelope({'MENTE':.02,'MOCA':.01},{'MENTE':'last-accepted','MOCA':'fallback'})
        self.assertEqual(retained['selections'][0]['kind'],'retained')
        self.assertEqual(retained['selections'][0]['original_observed_at'],original['selections'][0]['original_observed_at'])
        self.assertEqual(retained['observations'][-1],original['observations'][0])
        self.assertNotEqual(retained['exported_at'],retained['selections'][0]['original_observed_at'])
        unknown=P.Capture(TOKENS,now=Clock()).envelope({'MENTE':.02,'MOCA':.01},{'MENTE':'last-accepted','MOCA':'fallback'})
        self.assertIsNone(unknown['selections'][0]['original_observed_at']);self.assertEqual(unknown['selections'][1]['kind'],'static')
    def test_malformed_oversized_provider_metadata_cannot_expand_public_evidence(self):
        c=P.Capture(TOKENS,now=Clock());a=c.begin('MENTE','dexscreener')
        bad=pair(price='0.'+'0'*400+'1');bad.update(dexId='x'*70000,pairAddress={'private':'unsupported'})
        a.received(json.dumps({'pairs':[bad]}).encode());a.select_pair(0);a.finish('acquired')
        e=c.envelope({'MENTE':.02,'MOCA':.01},{'MENTE':'dexscreener','MOCA':'fallback'})
        self.assertLess(len(json.dumps(e)),65536);self.assertIsNone(e['observations'][0]['price_usd'])
        self.assertIsNone(e['observations'][0]['pair']['dex_id']);self.assertIsNone(e['observations'][0]['pair']['address'])
    def test_offline_replay_keeps_exact_envelope_clocks_and_never_invents_legacy_age(self):
        prev={'scope':{'generated_iso':'2026-10-29T00:00:00Z'},'facts':{'rate':{'MENTE':.02,'MOCA':.01},'rate_src':{'MENTE':'dexscreener','MOCA':'fallback'}}}
        e=P.offline_envelope(prev,TOKENS);self.assertEqual(e['observations'],[])
        self.assertIsNone(e['selections'][0]['original_observed_at'])
        prev['scope']['price_observations']=e
        self.assertEqual(P.offline_envelope(prev,TOKENS),e)
        self.assertIsNot(P.offline_envelope(prev,TOKENS),e)
    def test_acquisition_selection_is_captured_before_financial_retention(self):
        source=(ROOT/'refresh.py').read_text()
        self.assertLess(source.index('_PRICE_OBSERVATIONS ='),source.index('RATE = dict(_LAST_GOOD["facts"]["rate"])'))
        self.assertIn("data['scope']['price_observations'] = _PRICE_OBSERVATIONS",source)
        self.assertIn("json.dump(_PRICE_OBSERVATIONS,_price_file",source)
    def test_get_observer_does_not_add_provider_calls_or_change_numeric_decoding(self):
        raw=(ROOT/'refresh.py').read_text();tree=ast.parse(raw);fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='get')
        import urllib.request
        from unittest.mock import patch
        class Response(io.BytesIO):
            def __enter__(self):return self
            def __exit__(self,*a):pass
        c=P.Capture(TOKENS,now=Clock());a=c.begin('MENTE','blockscout')
        ns={'urllib':__import__('urllib'),'json':json,'_network_timeout':lambda *a,**k:5,'_V2_FAILS':[0],'V2_TRIP':2,'_network_sleep':lambda *a,**k:None}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'get','exec'),ns)
        with patch.object(urllib.request,'urlopen',return_value=Response(b'{"exchange_rate":0.020000000000000000000001}')) as urlopen:
            value=ns['get']('https://base.blockscout.com/api/v2/tokens/'+TOKENS['MENTE']['addr'],price_observation=a)
        self.assertEqual(urlopen.call_count,1);self.assertIsInstance(value['exchange_rate'],float)
        self.assertEqual(a.row['price_usd'],'0.020000000000000000000001')
    def test_committed_small_file_matches_embedded_envelope_and_passes_privacy(self):
        import check_publish
        d=json.loads((ROOT/'data.json').read_text());small=json.loads((ROOT/'price_observations.json').read_text())
        self.assertEqual(d['scope']['price_observations'],small)
        self.assertLessEqual((ROOT/'price_observations.json').stat().st_size,65536)
        self.assertEqual(check_publish._oracle_keys(small),[])
        self.assertEqual(check_publish._label_leaks(small,'price_observations.json'),[])
        self.assertEqual(check_publish._status_adjacent('price_observations.json',json.dumps(small)),[])
if __name__=='__main__':unittest.main()

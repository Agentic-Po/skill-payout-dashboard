import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,os.path.dirname(os.path.dirname(__file__)))
import public_scan as S
W='0x'+'1'*40
T='0x'+'2'*40
TOPIC_W='0x'+'0'*24+'1'*40
class RangeError(RuntimeError):pass
class ScanTests(unittest.TestCase):
 def test_populated_source_coverage_passes_unchanged_public_tripwire(self):
  import check_publish as P
  wallet='0xBD956171F5B50936f0Ad1C4db80c022bd2442519'; coupon='0xb15afc65532f8ec4d39db521ad7eb5b9e9ef5acf'
  desc=S.descriptor(wallet,'from',['0x2b11834ed1feaed4b4b3a86a6f571315e25a884d']);proof={'descriptor':desc,'start':10,'through':11,'hash':'0x'+'4'*64,'timestamp':1787307825,'target':12,'complete':False,'chunk':2}
  coupon_proof=dict(proof,descriptor=S.descriptor(coupon,'to',desc['tokens']))
  coverage={'coverage_ok':False,'pending':[proof,coupon_proof]}
  document={'scope':{'wallet':wallet,'complete':False,'source_coverage':coverage}}
  for artifact in ('data.json','full.html','coupon_data.json','coupon.html'):
   text=json.dumps(document)
   if artifact.endswith('.html'):text='<script>const DATA='+text+';</script>'
   self.assertEqual(P._status_adjacent(artifact,text),[],artifact)
   self.assertTrue(P._status_adjacent(artifact,text.replace('coverage_ok','finance_current')),artifact)
  for token in ('ent','burst'):
   self.assertTrue(P._status_adjacent('data.json',json.dumps({'wallet':wallet,token:1})))
  with self.assertRaises(S.ScanIncomplete):S.require_coverage(document)

 def test_malformed_log_shapes_are_invalid_before_materialization_or_checkpoint(self):
  import copy
  with tempfile.TemporaryDirectory() as root:
   scanner,_,_=self.setup_scan(root);original=scanner.rpc;valid=original('eth_getLogs',[{'fromBlock':'0xa','toBlock':'0xb'}])[0]
   variants=['not a log',None,[valid,'not a log']]
   for field,value in (('topics',None),('topics',[None]*3),('topics','abc'),('address',None),('transactionHash',None),('blockNumber','not hex'),('logIndex',None),('data',{}),('removed','false')):
    log=copy.deepcopy(valid);log[field]=value;variants.append(log)
   for log in variants:
    scanner,_,_=self.setup_scan(root);network=scanner.rpc;materialized=[]
    scanner.rpc=lambda method,params: (log if isinstance(log,list) else [log]) if method=='eth_getLogs' else network(method,params)
    scanner.materialize=lambda logs:materialized.append(logs)
    with self.assertRaises(S.ScanInvalid):scanner.scan(W,'from',[T],10,42,chunk=2)
    self.assertFalse(materialized);self.assertFalse(os.path.exists(root) and os.listdir(root))
 def test_semantic_log_filter_mismatch_still_invalid(self):
  with tempfile.TemporaryDirectory() as root:
   scanner,_,_=self.setup_scan(root);network=scanner.rpc
   def wrong(method,params):
    result=network(method,params)
    if method=='eth_getLogs':result[0]['address']='0x'+'9'*40
    return result
   scanner.rpc=wrong
   with self.assertRaises(S.ScanInvalid):scanner.scan(W,'from',[T],10,42,chunk=2)
   self.assertFalse(os.listdir(root))
 def setup_scan(self,root,limit=None,fail_shape=False):
  clock=[0];calls=[]
  def rpc(method,params):
   if method=='eth_getBlockByNumber':return {'number':params[0],'hash':'0x'+format(int(params[0],16),'064x'),'timestamp':hex(100+int(params[0],16))}
   p=params[0];lo=int(p['fromBlock'],16);hi=int(p['toBlock'],16);calls.append((lo,hi))
   if limit and hi-lo+1>limit:raise RangeError()
   clock[0]+=1
   return [{'topics':[S.TRANSFER_TOPIC,TOPIC_W,'0x'+'0'*24+'3'*40],'address':T,'blockNumber':hex(lo),'transactionHash':'0x'+format(lo,'064x'),'logIndex':'0x0','data':'0x1'}]
  def shape(logs):
   if fail_shape:raise S.BudgetExpired()
   return [{'transaction_hash':x['transactionHash'],'log_index':0,'block_number':int(x['blockNumber'],16),'timestamp':'2026-10-09T00:00:00Z','from':{'hash':W},'to':{'hash':'0x'+'3'*40},'token':{'address_hash':T},'total':{'value':'1','decimals':None}} for x in logs]
  return S.PublicScanner(root,rpc,shape,RangeError,budget_seconds=2,clock=lambda:clock[0]),clock,calls
 def test_partial_resume_never_returns_financial_rows(self):
  with tempfile.TemporaryDirectory() as root:
   scan,clock,calls=self.setup_scan(root,limit=2)
   with self.assertRaises(S.ScanIncomplete):scan.scan(W,'from',[T],10,45,chunk=2)
   path=os.path.join(root,S.scan_id(S.descriptor(W,'from',[T]))+'.json');p=json.load(open(path))
   self.assertEqual(p['through'],11);self.assertFalse(p['complete'])
   scan,clock,calls=self.setup_scan(root,limit=2);scan.budget_seconds=10
   rows=scan.scan(W,'from',[T],10,45,chunk=2)
   self.assertEqual(calls[0][0],12);self.assertEqual(len({S.log_key(r) for r in rows}),len(rows))
   self.assertTrue(json.load(open(path))['complete'])
 def test_timestamp_failure_does_not_checkpoint(self):
  with tempfile.TemporaryDirectory() as root:
   scan,_,_=self.setup_scan(root,fail_shape=True)
   with self.assertRaises(S.ScanIncomplete):scan.scan(W,'from',[T],10,40,chunk=1)
   self.assertEqual(os.listdir(root),[])
 def test_hash_mismatch_fails_closed(self):
  with tempfile.TemporaryDirectory() as root:
   scan,_,_=self.setup_scan(root)
   with self.assertRaises(S.ScanIncomplete):scan.scan(W,'from',[T],10,45,chunk=2)
   path=os.path.join(root,S.scan_id(S.descriptor(W,'from',[T]))+'.json');p=json.load(open(path));p['hash']='0xbad';S.atomic_save(path,p)
   scan,_,_=self.setup_scan(root)
   with self.assertRaises(S.ScanInvalid):scan.scan(W,'from',[T],10,45,chunk=2)
 def test_empty_covered_range_can_complete(self):
  with tempfile.TemporaryDirectory() as root:
   scan,_,_=self.setup_scan(root);old=scan.rpc
   scan.rpc=lambda m,p: [] if m=='eth_getLogs' else old(m,p)
   self.assertEqual(scan.scan(W,'from',[T],10,40,chunk=1),[])
   p=json.load(open(os.path.join(root,S.scan_id(S.descriptor(W,'from',[T]))+'.json')));self.assertEqual(p['through'],10);self.assertTrue(p['complete'])
 def test_filter_mismatch_never_checkpoints(self):
  with tempfile.TemporaryDirectory() as root:
   scan,_,_=self.setup_scan(root);old=scan.rpc
   def bad(m,p):
    r=old(m,p)
    if m=='eth_getLogs':r[0]['address']='0x'+'4'*40
    return r
   scan.rpc=bad
   with self.assertRaises(S.ScanInvalid):scan.scan(W,'from',[T],10,40,chunk=1)
   self.assertEqual(os.listdir(root),[])
 def test_interrupted_manifest_write_replays_orphan_chunk(self):
  with tempfile.TemporaryDirectory() as root:
   scan,_,_=self.setup_scan(root)
   real=S.atomic_save
   def write(path,value):
    if os.path.dirname(path)==root:raise OSError('checkpoint interrupted')
    real(path,value)
   with patch.object(S,'atomic_save',side_effect=write):
    with self.assertRaises(OSError):scan.scan(W,'from',[T],10,40,chunk=1)
   manifest=os.path.join(root,S.scan_id(S.descriptor(W,'from',[T]))+'.json')
   self.assertFalse(os.path.exists(manifest))
   scan,_,calls=self.setup_scan(root)
   rows=scan.scan(W,'from',[T],10,40,chunk=1)
   self.assertEqual(calls,[(10,10)]);self.assertEqual(len(rows),1)
 def test_corrupt_pending_chunk_fails_closed(self):
  with tempfile.TemporaryDirectory() as root:
   scan,_,_=self.setup_scan(root)
   with self.assertRaises(S.ScanIncomplete):scan.scan(W,'from',[T],10,45,chunk=2)
   sid=S.scan_id(S.descriptor(W,'from',[T]));path=os.path.join(root,sid,'10-11.json')
   with open(path,'w') as out:out.write('[]')
   scan,_,_=self.setup_scan(root)
   with self.assertRaises(S.ScanInvalid):scan.scan(W,'from',[T],10,45,chunk=2)
 def test_requesting_older_floor_does_not_skip_uncovered_prefix(self):
  with tempfile.TemporaryDirectory() as root:
   scan,_,_=self.setup_scan(root)
   self.assertEqual(len(scan.scan(W,'from',[T],10,40,chunk=1)),1)
   scan,_,calls=self.setup_scan(root);scan.budget_seconds=10
   rows=scan.scan(W,'from',[T],8,40,chunk=1)
   self.assertEqual(calls[0],(8,8));self.assertEqual({r['block_number'] for r in rows},{8,9,10})
 def test_advanced_requested_floor_keeps_persisted_origin_valid(self):
  with tempfile.TemporaryDirectory() as root:
   scan,_,_=self.setup_scan(root);scan.budget_seconds=10
   scan.scan(W,'from',[T],10,42,chunk=1)
   scan,_,_=self.setup_scan(root);scan.budget_seconds=10
   rows=scan.scan(W,'from',[T],11,44,chunk=1)
   self.assertTrue(all(row['block_number']>=11 for row in rows))
   S.validate_store(root)
   scan,_,_=self.setup_scan(root);scan.budget_seconds=10
   rows=scan.scan(W,'from',[T],12,45,chunk=1)
   S.validate_store(root)
   self.assertTrue(all(row['block_number']>=12 for row in rows))
 def test_later_cap_excludes_rows_beyond_requested_endpoint(self):
  with tempfile.TemporaryDirectory() as root:
   scan,_,_=self.setup_scan(root);scan.budget_seconds=10
   scan.scan(W,'from',[T],10,42,chunk=1)
   scan,_,_=self.setup_scan(root)
   rows=scan.scan(W,'from',[T],10,40,chunk=1)
   self.assertEqual({r['block_number'] for r in rows},{10})
 def test_materializer_cannot_drop_or_replace_transfer(self):
  with tempfile.TemporaryDirectory() as root:
   scan,_,_=self.setup_scan(root)
   scan.materialize=lambda logs:[{'transaction_hash':'0xwrong','log_index':0,'block_number':10,'timestamp':'2026-10-09'}]
   with self.assertRaises(S.ScanInvalid):scan.scan(W,'from',[T],10,40,chunk=1)
   self.assertEqual(os.listdir(root),[])

 def test_public_store_accepts_only_chain_row_schema(self):
  with tempfile.TemporaryDirectory() as root:
   scan,_,_=self.setup_scan(root)
   scan.materialize=lambda logs:[{'timestamp':'2026-10-09T00:00:00.000000Z','transaction_hash':x['transactionHash'],'log_index':0,'block_number':int(x['blockNumber'],16),'from':{'hash':W},'to':{'hash':'0x'+'3'*40},'token':{'address_hash':T},'total':{'value':'1','decimals':None}} for x in logs]
   scan.scan(W,'from',[T],10,40,chunk=1)
   S.validate_store(root)
   sid=S.scan_id(S.descriptor(W,'from',[T]));chunk=os.path.join(root,sid,'10-10.json')
   rows=json.load(open(chunk));rows[0]['from']['label']='private person'
   S.atomic_save(chunk,rows)
   with self.assertRaises(S.ScanInvalid):S.validate_store(root)
 def test_cursor_cannot_claim_unpersisted_range(self):
  with tempfile.TemporaryDirectory() as root:
   scan,_,_=self.setup_scan(root)
   scan.materialize=lambda logs:[{'timestamp':'2026-10-09T00:00:00.000000Z','transaction_hash':x['transactionHash'],'log_index':0,'block_number':int(x['blockNumber'],16),'from':{'hash':W},'to':{'hash':'0x'+'3'*40},'token':{'address_hash':T},'total':{'value':'1','decimals':None}} for x in logs]
   scan.scan(W,'from',[T],10,40,chunk=1)
   path=os.path.join(root,S.scan_id(S.descriptor(W,'from',[T]))+'.json');state=json.load(open(path));state['through']=12;S.atomic_save(path,state)
   with self.assertRaises(S.ScanInvalid):S.validate_store(root)

class HealthTests(unittest.TestCase):
 def test_actual_workflow_guards_publication_and_healthy_ping(self):
  root=os.path.dirname(os.path.dirname(__file__))
  with open(os.path.join(root,'.github/workflows/refresh.yml')) as source:text=source.read()
  names=['Guard against detector-signal leaks','Sanity gate (independent recompute-and-diff)','Commit and push','Require complete chain coverage for healthy success','Dead-man ping']
  positions=[text.index('- name: '+name) for name in names]
  self.assertEqual(positions,sorted(positions))
  guard=text.split('- name: Require complete chain coverage for healthy success',1)[1].split('- name: Dead-man ping',1)[0]
  self.assertNotIn('continue-on-error:',guard)
  self.assertNotIn('if: always()',guard)
  self.assertIn('require_coverage(data, coupon)',guard)
  self.assertIn('steps.coverage.outcome',text)
  self.assertIn('partial chain scan published; current financial coverage unavailable',text)

 def test_complete_empty_verified_leg_is_healthy(self):
  scope={'complete':True,'source_coverage':{'coverage_ok':True,'pending':[{'complete':True,'through':12,'target':12,'hash':'0x'+'1'*64}]}}
  S.require_coverage({'scope':scope},{'scope':scope})
 def test_partial_or_missing_coupon_proof_is_unhealthy(self):
  good={'scope':{'complete':True,'source_coverage':{'coverage_ok':True,'pending':[]}}}
  for bad in ({'scope':{'complete':False}}, {'scope':{'complete':True}}, {'scope':{'complete':True,'source_coverage':{'coverage_ok':True,'pending':[{'complete':False}]}}}):
   with self.assertRaises(S.ScanIncomplete):S.require_coverage(good,bad)

class IntegrationTests(unittest.TestCase):
 def refresh_ns(self, fallback):
  import ast, types
  source=open(os.path.join(os.path.dirname(os.path.dirname(__file__)),'refresh.py')).read()
  tree=ast.parse(source); node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='refresh_cache')
  old=[{'transaction_hash':'old','log_index':0,'block_number':100,'timestamp':'2026-10-08T00:00:00Z'}]
  saved=[]; staged=[]
  shards=types.SimpleNamespace(migrate_legacy=lambda p:None,load=lambda p:old,slim=lambda r:r,month_of=lambda r:'2026-10',save=lambda *a,**k:saved.append(a))
  ns={'WALLET':W,'TOKENS':{'T':{'addr':T}},'shards':shards,'key':lambda r:(r['transaction_hash'],r['log_index']),'get':lambda u:{'items':[{'transaction_hash':'new','log_index':0,'block_number':110,'timestamp':'2026-10-09T00:00:00Z'},{'transaction_hash':'tip','log_index':0,'block_number':120,'timestamp':'2026-10-09T00:01:00Z'}]},'rpc':lambda *a:'0x96','rpc_transfer_fallback':fallback,'xcheck_from':lambda n,b:b,'xcheck_done':lambda *a:saved.append(a),'XCHECK_WINDOW':10,'os':os,'HERE':'/public','_TREASURY_STAGE':staged,'_COUPON_STAGE':[],'_RPC_LAST_THROUGH':110}
  exec(ast.get_source_segment(source,node),ns)
  return ns,old,saved,staged
 def test_stale_subsidiary_marks_actual_snapshot_incomplete_with_old_source_clock(self):
  import ast,types
  from datetime import datetime,timezone
  source=open(os.path.join(os.path.dirname(os.path.dirname(__file__)),'refresh.py')).read();tree=ast.parse(source)
  node=next(n for n in tree.body if isinstance(n,ast.If) and 'data["scope"]["source_coverage"] =' in ast.get_source_segment(source,n))
  data={'scope':{'complete':True,'generated_iso':'2026-10-09T00:00:00Z'}}
  ns={'OFFLINE':False,'_PUBLIC_SCANNER':types.SimpleNamespace(last={'required':{'complete':False}}),'data':data,'data_complete':True,'datetime':datetime,'timezone':timezone,'_LAST_GOOD_SECTION_CLOCKS':{'sink':'2026-10-08T00:00:00Z'}}
  exec(compile(ast.Module(body=[node],type_ignores=[]),'source_clock','exec'),ns)
  self.assertFalse(data['scope']['complete']);self.assertFalse(data['scope']['source_coverage']['coverage_ok'])
  self.assertEqual(data['scope']['generated_iso'],'2026-10-09T00:00:00Z')
  self.assertEqual(data['scope']['source_coverage']['last_good_sections']['sink'],'2026-10-08T00:00:00Z')
 def test_product_queries_recompute_remaining_timeout_each_request(self):
  import io
  import posthog_source as product
  clock=[0];calls=[]
  def remaining():
   value=3-clock[0]
   if value<=0:raise S.BudgetExpired('fixture deadline')
   return value
  def urlopen(req,timeout):
   calls.append(timeout);clock[0]+=min(2,timeout)
   return io.BytesIO(b'{"results":[]}')
  with tempfile.TemporaryDirectory() as root:
   with patch.object(product,'_env',return_value={'POSTHOG_API_KEY':'synthetic fixture'}),patch.object(product,'CACHE',os.path.join(root,'cache.json')),patch.object(product.urllib.request,'urlopen',side_effect=urlopen):
    result=product.fetch(timeout=remaining)
   self.assertEqual(calls,[3,1]);self.assertFalse(result['complete'])
 def test_online_acquisition_timeouts_share_one_budget(self):
  import ast,types
  source=open(os.path.join(os.path.dirname(os.path.dirname(__file__)),'refresh.py')).read();tree=ast.parse(source)
  clock=[0];calls=[]
  def urlopen(req,timeout):
   calls.append(timeout);clock[0]+=timeout;raise TimeoutError('fixture timeout')
  ns={'time':types.SimpleNamespace(monotonic=lambda:clock[0],sleep=lambda n:clock.__setitem__(0,clock[0]+n)),'urllib':types.SimpleNamespace(request=types.SimpleNamespace(Request=lambda *a,**k:None,urlopen=urlopen)),'_NETWORK_DEADLINE':None,'_MARKET_DEADLINE':None,'_V2_FAILS':[0],'V2_TRIP':2,'json':json}
  for name in ('_network_timeout','_network_sleep','get'):
   node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name);exec(ast.get_source_segment(source,node),ns)
  with self.assertRaises(TimeoutError):ns['get']('https://public.invalid')
  self.assertEqual(clock[0],240);self.assertTrue(all(n<=60 for n in calls))
  before=len(calls)
  with self.assertRaises(S.BudgetExpired):ns['get']('https://public.invalid')
  self.assertEqual(len(calls),before)
  # A fresh v2 outage cannot consume the entire fallback reserve.
  clock[0]=0;ns['_NETWORK_DEADLINE']=None;calls.clear()
  with self.assertRaises(TimeoutError):ns['get']('https://base.blockscout.com/api/v2/fixture')
  self.assertEqual(clock[0],32);self.assertEqual(calls,[5,5,5,5])
  with tempfile.TemporaryDirectory() as root:
   scan,_,_=ScanTests().setup_scan(root);scan.clock=lambda:clock[0]
   self.assertTrue(scan.scan(W,'from',[T],10,40,chunk=1))
   S.validate_store(root)
  # Gecko has its own bounded reservation after the chain budget is spent.
  clock[0]=240;calls.clear()
  with self.assertRaises(TimeoutError):ns['get']('https://api.geckoterminal.com/api/v2/fixture')
  self.assertTrue(calls);self.assertLessEqual(clock[0],300)
 def test_online_sink_failure_restores_both_directions_and_saved_section(self):
  import ast,io
  source=open(os.path.join(os.path.dirname(os.path.dirname(__file__)),'refresh.py')).read()
  tree=ast.parse(source)
  handler=next(n.handlers[0] for n in tree.body if isinstance(n,ast.Try) and n.handlers and 'sink fetch failed:' in ast.get_source_segment(source,n.handlers[0]))
  with tempfile.TemporaryDirectory() as root:
   with open(os.path.join(root,'data.json'),'w') as out:json.dump({'scope':{'generated_iso':'2026-10-08T00:00:00Z'},'sink':{'series':[{'s':1}]}},out)
   ns={'e':S.ScanIncomplete(),'OFFLINE':False,'PREV':None,'STATE':{'sink':{'rows':{'to':['new'],'from':['old']}},'xcheck':{'sink_to':999}},'_sink_saved_rows':{'to':['old'],'from':['old']},'_sink_saved_xcheck':{'sink_to':10},'_LAST_GOOD_SECTION_CLOCKS':{},'os':os,'HERE':root,'json':json}
   module=ast.Module(body=handler.body,type_ignores=[])
   exec(compile(module,'sink_failure','exec'),ns)
   self.assertEqual(ns['STATE']['sink']['rows'],{'to':['old'],'from':['old']})
   self.assertEqual(ns['STATE']['xcheck'],{'sink_to':10})
   self.assertEqual(ns['sink'],{'series':[{'s':1}]})
   self.assertEqual(ns['_LAST_GOOD_SECTION_CLOCKS'],{'sink':'2026-10-08T00:00:00Z'})
 def test_sink_partial_crosscheck_cannot_mutate_active_direction(self):
  import ast
  source=open(os.path.join(os.path.dirname(os.path.dirname(__file__)),'refresh.py')).read()
  tree=ast.parse(source)
  old={'to':[{'ts':'2026-10-08','val':1,'col':True,'blk':10,'k':'old'}]}
  def incomplete(*a,**k):raise S.ScanIncomplete()
  ns={'_sink_st':old,'COLLECTOR':W,'SINK':W,'TOKENS':{'MENTE':{'addr':T}},'DECIMALS':{'MENTE':18},'get':lambda u:{'items':[{'timestamp':'2026-10-09T00:00:00Z','total':{'value':'1','decimals':18},'token':{'address_hash':T},'from':{'hash':W},'to':{'hash':W},'block_number':20,'transaction_hash':'new','log_index':0}]},'key':lambda r:r['transaction_hash'],'xcheck_from':lambda *a:10,'SINK_GENESIS_BLOCK':1,'XCHECK_WINDOW':2,'rpc':lambda *a:hex(100),'SINK_XC_LAG':30,'SINK_XC_BUDGET':50,'rpc_transfer_fallback':incomplete,'xcheck_done':lambda *a:self.fail('partial cursor advanced'),'_public_leg':lambda *a:None}
  for name in ('_sink_row','_sweep'):
   node=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name==name)
   exec(ast.get_source_segment(source,node),ns)
  with self.assertRaises(S.ScanIncomplete):ns['_sweep']('to')
  self.assertEqual(old['to'][0]['k'],'old');self.assertEqual(len(old['to']),1)
 def test_bootstrap_head_request_is_inside_shared_budget(self):
  import ast
  source=open(os.path.join(os.path.dirname(os.path.dirname(__file__)),'refresh.py')).read()
  node=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='rpc_transfer_fallback')
  with tempfile.TemporaryDirectory() as root:
   scanner=S.PublicScanner(root,None,None,RangeError,budget_seconds=180,clock=lambda:25)
   def rpc(*a):
    self.assertEqual(scanner.deadline,205)
    raise S.BudgetExpired()
   ns={'_PUBLIC_SCANNER':scanner,'_RPC_LAST_THROUGH':None,'rpc':rpc,'HERE':root,'_shape_rpc_transfers':None,'RpcRangeError':RangeError}
   helper=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='_public_leg')
   ns['os']=os
   exec(ast.get_source_segment(source,helper),ns)
   exec(ast.get_source_segment(source,node),ns)
   with self.assertRaises(S.ScanIncomplete):ns['rpc_transfer_fallback'](W,'from',[T],10)
   self.assertFalse(next(iter(scanner.last.values()))['complete'])
   scope={'complete':True,'source_coverage':{'coverage_ok':True,'pending':list(scanner.last.values())}}
   with self.assertRaises(S.ScanIncomplete):S.require_coverage({'scope':scope})
 def test_partial_crosscheck_retains_cache_without_advancing_cursor(self):
  def incomplete(*a,**k):raise S.ScanIncomplete('partial')
  ns,old,saved,staged=self.refresh_ns(incomplete)
  self.assertEqual(ns['refresh_cache']('public?filter=from','/public/transfers'),(old,0,False))
  self.assertEqual(saved,[]);self.assertEqual(staged,[])
 def test_completed_crosscheck_excludes_unfinalized_v2_tip(self):
  ns,old,saved,staged=self.refresh_ns(lambda *a,**k:[])
  rows,added,complete=ns['refresh_cache']('public?filter=from','/public/transfers')
  self.assertTrue(complete);self.assertEqual(added,1)
  self.assertEqual({r['transaction_hash'] for r in rows},{'new','old'})
  self.assertEqual(len(staged),1)

if __name__=='__main__':unittest.main()

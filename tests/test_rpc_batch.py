#!/usr/bin/env python3
"""Validated batch recovery preserves exact timestamps and single fallback."""
import ast,io,json,os,sys,time,unittest
from collections import Counter
from contextlib import redirect_stdout
ROOT=os.path.dirname(os.path.dirname(__file__));sys.path.insert(0,ROOT)
src=open(os.path.join(ROOT,'refresh.py')).read();tree=ast.parse(src)
fns={n.name:ast.get_source_segment(src,n) for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('rpc_batch','_rpc_batch_skip','block_ts_prefetch','block_ts')}
class Resp(io.StringIO):
 def __enter__(self):return self
 def __exit__(self,*args):return False
def block(i):return {'number':hex(i+1),'timestamp':hex(100+i)}
calls=[('eth_getBlockByNumber',[hex(i+1),False]) for i in range(3)]
def harness(reply):
 seen=[];timeouts=[]
 class Req:
  def __init__(self,url,data=None,headers=None):self.full_url=url;self.data=data
 def fetch(req,timeout=None):
  payload=json.loads(req.data);seen.append((req.full_url,payload));timeouts.append(timeout)
  result=reply(req.full_url,payload)
  if isinstance(result,Exception):raise result
  return Resp(json.dumps(result))
 ns={'json':json,'time':time,'Counter':Counter,'RPC_ENDPOINTS':['https://a.invalid','https://b.invalid','https://c.invalid'],'urllib':type('U',(),{'request':type('R',(),{'Request':Req,'urlopen':staticmethod(fetch)})}),'_network_timeout':lambda cap:cap,'_RPC_BATCH_SKIPS':Counter(),'_RPC_BATCH_LOG_MAX':5}
 for name in ('_rpc_batch_skip','rpc_batch'):exec(fns[name],ns)
 return ns,seen,timeouts
class BatchTests(unittest.TestCase):
 def run_batch(self,ns,c=calls):
  with redirect_stdout(io.StringIO()):return ns['rpc_batch'](c)
 def test_partial_only_missing_preserves_exact_values(self):
  ns,seen,tm=harness(lambda u,p:[{'id':i['id'],'result':block(i['id'])} for i in p if u.endswith('b.invalid') or i['id']==0])
  self.assertEqual(self.run_batch(ns),{i:block(i) for i in range(3)})
  self.assertEqual([i['id'] for i in seen[1][1]],[1,2]);self.assertEqual(ns['_RPC_GOOD_ENDPOINTS'][('batch',('eth_getBlockByNumber',))],'https://b.invalid');self.assertTrue(all(t<=5 for t in tm))
 def test_duplicate_unknown_bool_ids_never_accepted(self):
  for bad in ([{'id':0,'result':block(0)}]*2,[{'id':7,'result':block(0)}],[{'id':True,'result':block(0)}],[None]):
   ns,_,_=harness(lambda u,p:bad);self.assertEqual(self.run_batch(ns),{})
 def test_null_error_missing_and_wrong_block_stay_missing(self):
  ns,_,_=harness(lambda u,p:[{'id':0,'result':None},{'id':1,'result':block(1),'error':{}},{'id':2,'result':block(0)}])
  self.assertEqual(self.run_batch(ns),{});self.assertNotIn(('batch',('eth_getBlockByNumber',)),ns.get('_RPC_GOOD_ENDPOINTS',{}))
 def test_partial_survives_invalid_alternative(self):
  ns,_,_=harness(lambda u,p:[{'id':0,'result':block(0)}] if u.endswith('a.invalid') else [{'id':0,'result':block(0)}])
  self.assertEqual(self.run_batch(ns),{0:block(0)})
 def test_dead_batch_endpoints_not_repeated_or_disable_singles(self):
  ns,seen,_=harness(lambda u,p:TimeoutError('secret-address') if u.endswith('a.invalid') else [{'id':i['id'],'result':block(i['id'])} for i in p])
  self.run_batch(ns);self.run_batch(ns);self.assertEqual(sum(u.endswith('a.invalid') for u,p in seen),1)
  self.assertEqual(ns['RPC_ENDPOINTS'][0],'https://a.invalid');self.assertEqual(ns['_RPC_BATCH_WORK']['calls'],3)
 def test_all_failed_empty_and_log_cap_preserved(self):
  ns,seen,_=harness(lambda u,p:TimeoutError('private-detail'));self.assertEqual(self.run_batch(ns),{});self.assertEqual(self.run_batch(ns),{});self.assertEqual(len(seen),3)
  with redirect_stdout(io.StringIO()) as out:
   for i in range(12):ns['_rpc_batch_skip']('error','https://a.invalid','1x block','TimeoutError')
  self.assertLessEqual(out.getvalue().count('rpc_batch skip'),5);self.assertEqual(ns['_RPC_BATCH_SKIPS']['error'],15)
 def test_expired_budget_retains_partial_without_next_socket(self):
  ns,seen,_=harness(lambda u,p:[{'id':0,'result':block(0)}]);count=[0]
  class Scanner:
   deadline=10
   def clock(self):return 8
   def expired(self):count[0]+=1;return count[0]>1
  ns['_PUBLIC_SCANNER']=Scanner();self.assertEqual(self.run_batch(ns),{0:block(0)});self.assertEqual(len(seen),1)
 def test_prefetch_missing_uses_direct_exact_block(self):
  ns,_,_=harness(lambda u,p:[{'id':0,'result':block(0)}] if u.endswith('a.invalid') else TimeoutError())
  ns.update(_block_ts_cache={},_fmt_ts=lambda value:int(value,16),_network_sleep=lambda value:None)
  direct=[]
  def rpc(method,p):direct.append(p[0]);return {'timestamp':'0xff'}
  ns['rpc']=rpc
  for name in ('block_ts_prefetch','block_ts'):exec(fns[name],ns)
  with redirect_stdout(io.StringIO()):ns['block_ts_prefetch']([1,2,3])
  self.assertEqual(ns['block_ts'](1),100);self.assertEqual(ns['block_ts'](2),255);self.assertEqual(direct,['0x2'])
 def test_original_skip_reason_host_and_call_logs_retained(self):
  def reply(u,p):
   if u.endswith('a.invalid'):return ConnectionError('secret-address')
   if u.endswith('b.invalid'):return {'error':{'message':'secret-provider-body'}}
   return [{'id':0,'result':block(0)},{'id':1,'error':{'message':'secret-item-body'}},{'id':2,'result':block(2)}]
  ns,_,_=harness(reply)
  with redirect_stdout(io.StringIO()) as captured:got=ns['rpc_batch'](calls)
  self.assertEqual(got,{0:block(0),2:block(2)})
  self.assertEqual(dict(ns['_RPC_BATCH_SKIPS']),{'error':1,'not-a-batch':1,'items-dropped':1})
  log=captured.getvalue()
  for expected in ('[error] a.invalid (3x eth_getBlockByNumber)','[not-a-batch] b.invalid (3x eth_getBlockByNumber)','[items-dropped] c.invalid (3x eth_getBlockByNumber): 1 of 3'):
   self.assertIn(expected,log)
  self.assertNotIn('secret-',log)
 def test_clean_batch_silent_and_complete(self):
  ns,_,_=harness(lambda u,p:[{'id':i['id'],'result':block(i['id'])} for i in p])
  with redirect_stdout(io.StringIO()) as captured:got=ns['rpc_batch'](calls)
  self.assertEqual(got,{i:block(i) for i in range(3)});self.assertEqual(captured.getvalue(),'');self.assertFalse(ns['_RPC_BATCH_SKIPS'])
 def test_exact_log_cap_and_unbounded_counts(self):
  ns,_,_=harness(lambda u,p:None)
  with redirect_stdout(io.StringIO()) as captured:
   for i in range(12):ns['_rpc_batch_skip']('error','https://a.invalid','3x eth_getBlockByNumber','TimeoutError')
  self.assertEqual(captured.getvalue().count('rpc_batch skip'),5);self.assertEqual(ns['_RPC_BATCH_SKIPS']['error'],12)
 def test_phase_summary_retains_skip_telemetry(self):
  self.assertIn('rpc_batch_skips=%d',src)
  self.assertIn('RPC ACQUISITION: single_roundtrips=%d batch_roundtrips=%d batch_seconds=%.1f batch_results=%d',src)
 def test_empty_calls_no_requests(self):
  ns,seen,_=harness(lambda u,p:None);self.assertEqual(self.run_batch(ns,[]),{});self.assertFalse(seen)
class MethodHealthTests(unittest.TestCase):
 def test_failure_isolated_both_method_orders(self):
  for failed,working in [('eth_getBlockByNumber','eth_getLogs'),('eth_getLogs','eth_getBlockByNumber')]:
   def reply(url,payload):
    if payload[0]['method']==failed:return ConnectionError('private-provider-detail')
    return [{'id':p['id'],'result':block(p['id']) if working=='eth_getBlockByNumber' else []} for p in payload]
   ns,seen,_=harness(reply)
   with redirect_stdout(io.StringIO()):
    initial=ns['rpc_batch']([(working,['0x1',False])])
    self.assertEqual(initial,{0:block(0) if working=='eth_getBlockByNumber' else []})
    self.assertEqual(ns['rpc_batch']([(failed,['0x1',False])]),{})
    got=ns['rpc_batch']([(working,['0x1',False])])
   self.assertEqual(got,{0:block(0) if working=='eth_getBlockByNumber' else []})
   self.assertEqual(seen[-1][0],'https://a.invalid')
   self.assertEqual(ns['_RPC_BATCH_DEAD'][(failed,)],set(ns['RPC_ENDPOINTS']))
   self.assertFalse(ns['_RPC_BATCH_DEAD'][(working,)])
 def test_preference_isolated_and_mixed_failure_does_not_poison(self):
  def reply(url,payload):
   methods={p['method'] for p in payload}
   if len(methods)>1:return ConnectionError('mixed failure')
   if methods=={'eth_getLogs'} and url.endswith('a.invalid'):return ConnectionError('log refusal')
   return [{'id':p['id'],'result':block(p['id']) if p['method']=='eth_getBlockByNumber' else []} for p in payload]
  ns,seen,_=harness(reply)
  with redirect_stdout(io.StringIO()):
   self.assertEqual(ns['rpc_batch']([('eth_getLogs',[]) ]),{0:[]})
   self.assertEqual(ns['rpc_batch']([('eth_getLogs',[]),('eth_getBlockByNumber',['0x2',False])]),{})
   self.assertEqual(ns['rpc_batch']([('eth_getBlockByNumber',['0x1',False])]),{0:block(0)})
   self.assertEqual(ns['rpc_batch']([('eth_getLogs',[])]),{0:[]})
  self.assertEqual(seen[-2][0],'https://a.invalid');self.assertEqual(seen[-1][0],'https://b.invalid')
  self.assertEqual(ns['_RPC_GOOD_ENDPOINTS'][('batch',('eth_getLogs',))],'https://b.invalid')
 def test_two_header_groups_missing_only_cache_and_single_fallback(self):
  requests=[]
  def reply(url,payload):
   requests.append(payload)
   return [{'id':p['id'],'result':{'number':p['params'][0],'timestamp':hex(100+int(p['params'][0],16))}} for p in payload if p['params'][0]!='0x2']
  ns,seen,_=harness(reply);ns.update(_block_ts_cache={5:'saved'},_fmt_ts=lambda value:int(value,16),_network_sleep=lambda delay:None)
  for name in ('block_ts_prefetch','block_ts'):exec(fns[name],ns)
  singles=[];ns['rpc']=lambda method,params:singles.append((method,params)) or {'timestamp':'0x66'}
  with redirect_stdout(io.StringIO()):ns['block_ts_prefetch']([1,2,3,4,5,1])
  self.assertTrue(all(len(p)<=2 for p in requests));self.assertEqual([p['params'][0] for p in requests[1]],['0x2'])
  self.assertEqual(ns['_block_ts_cache'],{1:101,3:103,4:104,5:'saved'})
  self.assertEqual(ns['block_ts'](2),102);self.assertEqual(singles,[('eth_getBlockByNumber',['0x2',False])])
  self.assertEqual(ns['block_ts'](2),102);self.assertEqual(len(singles),1)
class ScopedLogTests(unittest.TestCase):
 def ns(self):
  ns={}
  for name in ('_valid_log_subrange','_bounded_scan_rpc'):
   node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name);exec(ast.get_source_segment(src,node),ns)
  return ns
 def filt(self,width=2000):
  return {'fromBlock':'0xa','toBlock':hex(10+width-1),'address':['0x'+'2'*40],'topics':['0x'+'a'*64,'0x'+'0'*24+'1'*40]}
 def log(self,query,identity=None):
  block=int(query['fromBlock'],16)
  return {'topics':query['topics']+['0x'+'b'*64],'address':query['address'][0],'transactionHash':'0x'+format(identity if identity is not None else block,'064x'),'blockNumber':hex(block),'logIndex':'0x0','data':'0x1','removed':False}
 def test_twenty_contiguous_subranges_in_ten_two_call_groups_exact_filter(self):
  ns=self.ns();groups=[];f=self.filt()
  def batch(group,validate_result):
   groups.append(group);return {i:[self.log(call[1][0])]for i,call in enumerate(group)}
  ns.update(rpc_batch=batch,rpc=lambda *a,**k:self.fail('single fallback'))
  rows=ns['_bounded_scan_rpc']('eth_getLogs',[f]);self.assertEqual(len(rows),20);self.assertEqual([len(g)for g in groups],[2]*10);queries=[c[1][0]for g in groups for c in g]
  self.assertEqual(queries[0]['fromBlock'],f['fromBlock']);self.assertEqual(queries[-1]['toBlock'],f['toBlock'])
  for a,b in zip(queries,queries[1:]):self.assertEqual(int(a['toBlock'],16)+1,int(b['fromBlock'],16))
  self.assertTrue(all(int(q['toBlock'],16)-int(q['fromBlock'],16)+1<=100 and q['address']==f['address'] and q['topics']==f['topics']for q in queries));self.assertEqual(f,self.filt())
 def test_partial_batch_only_missing_subrange_falls_back_with_exact_filter(self):
  ns=self.ns();singles=[];f=self.filt(250)
  def batch(group,validate_result):return {i:[]for i in range(len(group))if i!=1}
  def rpc(method,params,validate_result):singles.append((method,params));return [self.log(params[0])]
  ns.update(rpc_batch=batch,rpc=rpc);rows=ns['_bounded_scan_rpc']('eth_getLogs',[f]);self.assertEqual(len(rows),1);self.assertEqual(len(singles),1);q=singles[0][1][0];self.assertEqual((q['fromBlock'],q['toBlock']),('0x6e','0xd1'));self.assertEqual(q['topics'],f['topics']);self.assertEqual(q['address'],f['address'])
 def test_strict_subrange_rejects_shapes_filters_and_duplicate_identity(self):
  import copy
  ns=self.ns();q=self.filt(100);call=('eth_getLogs',[q]);valid=self.log(q);self.assertTrue(ns['_valid_log_subrange'](call,[valid]));self.assertTrue(ns['_valid_log_subrange'](call,[]))
  variants=[None,{},['bad'],[valid,valid]]
  for key,value in [('blockNumber','0x6e'),('address','0x'+'3'*40),('topics',['0x'+'c'*64]*3),('transactionHash',None),('logIndex',True),('data',None),('removed',True),('removed','false')]:
   log=copy.deepcopy(valid);log[key]=value;variants.append([log])
  for value in variants:self.assertFalse(ns['_valid_log_subrange'](call,value))
 def test_scoped_items_validate_before_alternative_provider_selection(self):
  helper=self.ns();queries=[('eth_getLogs',[self.filt(100)]),('eth_getLogs',[dict(self.filt(100),fromBlock='0x6e',toBlock='0xd1')])]
  def reply(url,payload):
   return [{'jsonrpc':'2.0','id':item['id'],'result':[self.log(queries[item['id']][1][0])] if not(url.endswith('a.invalid')and item['id']==0) else [self.log(queries[1][1][0])]}for item in payload]
  ns,seen,_=harness(reply)
  with redirect_stdout(io.StringIO()):got=ns['rpc_batch'](queries,validate_result=helper['_valid_log_subrange'])
  self.assertEqual(set(got),{0,1});self.assertEqual([x['id']for x in seen[1][1]],[0]);self.assertEqual(got[0][0]['blockNumber'],'0xa')
 def test_bad_version_both_result_error_and_invalid_ids_never_empty_proof(self):
  helper=self.ns();query=[('eth_getLogs',[self.filt(100)])]
  for bad in ({'id':0,'result':[]},{'jsonrpc':'1.0','id':0,'result':[]},{'jsonrpc':'2.0','id':0,'result':[],'error':None},{'jsonrpc':'2.0','id':True,'result':[]},{'jsonrpc':'2.0','id':1,'result':[]}):
   ns,_,_=harness(lambda u,p:[bad])
   with redirect_stdout(io.StringIO()):self.assertEqual(ns['rpc_batch'](query,validate_result=helper['_valid_log_subrange']),{})
 def test_duplicate_identity_across_valid_subranges_and_invalid_missing_fail_closed(self):
  ns=self.ns();f=self.filt(200)
  ns.update(rpc_batch=lambda group,**kw:{i:[self.log(call[1][0],identity=9)]for i,call in enumerate(group)},rpc=lambda *a,**k:[])
  from public_scan import ScanInvalid
  with self.assertRaises(ScanInvalid):ns['_bounded_scan_rpc']('eth_getLogs',[f])
  ns['rpc_batch']=lambda *a,**k:{};ns['rpc']=lambda *a,**k:None
  with self.assertRaises(ScanInvalid):ns['_bounded_scan_rpc']('eth_getLogs',[f])
 def test_bounds_and_offline_kill_never_network(self):
  ns=self.ns();seen=[]
  ns.update(rpc=lambda *a,**k:seen.append(a),rpc_batch=lambda *a,**k:seen.append(a))
  from public_scan import ScanInvalid
  for lo,hi in ((-1,10),(10,9),(10,2010)):
   f=self.filt();f.update(fromBlock=hex(lo),toBlock=hex(hi))
   with self.assertRaises(ScanInvalid):ns['_bounded_scan_rpc']('eth_getLogs',[f])
  self.assertFalse(seen)
  kill=next(n for n in tree.body if isinstance(n,ast.If) and any(isinstance(x,ast.FunctionDef)and x.name=='_no_net'for x in n.body));ns['OFFLINE']=True;exec(ast.get_source_segment(src,kill),ns)
  for width in (31,2000):
   with self.assertRaisesRegex(RuntimeError,'network access is disabled'):ns['_bounded_scan_rpc']('eth_getLogs',[self.filt(width)])
  self.assertFalse(seen)
 def test_later_batch_failure_never_persists_original_range(self):
  import tempfile,public_scan as S
  ns=self.ns();groups=[]
  def batch(group,**kwargs):
   groups.append(group)
   if len(groups)==2:raise S.BudgetExpired('synthetic deadline')
   return {i:[]for i in range(len(group))}
  def rpc(method,params,**kwargs):return {'number':params[0],'hash':'0x'+'a'*64,'timestamp':'0x1'}
  ns.update(rpc_batch=batch,rpc=rpc)
  with tempfile.TemporaryDirectory()as root:
   class RangeError(Exception):pass
   scanner=S.PublicScanner(root,ns['_bounded_scan_rpc'],lambda logs:[],RangeError,budget_seconds=360)
   with self.assertRaises(S.ScanIncomplete):scanner.scan('0x'+'1'*40,'from',['0x'+'2'*40],10,2039,chunk=2000)
   self.assertFalse(os.listdir(root));self.assertFalse(next(iter(scanner.last.values()))['complete']);S.validate_store(root)
 def test_expired_active_budget_batch_makes_zero_requests(self):
  ns,seen,_=harness(lambda *a:self.fail('network after budget'))
  ns['_PUBLIC_SCANNER']=type('Scanner',(),{'clock':type('Clock',(),{'depth':1})(),'expired':lambda self:True})()
  helper=self.ns()
  with redirect_stdout(io.StringIO()):self.assertEqual(ns['rpc_batch']([('eth_getLogs',[self.filt(100)])],validate_result=helper['_valid_log_subrange']),{})
  self.assertFalse(seen)

if __name__=='__main__':unittest.main()

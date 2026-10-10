#!/usr/bin/env python3
"""Validated batch recovery preserves exact timestamps and single fallback."""
import ast,io,json,os,sys,time,unittest
from collections import Counter
from contextlib import redirect_stdout
ROOT=os.path.dirname(os.path.dirname(__file__))
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
  self.assertEqual([i['id'] for i in seen[1][1]],[1,2]);self.assertEqual(ns['_RPC_GOOD_ENDPOINTS']['batch'],'https://b.invalid');self.assertTrue(all(t<=5 for t in tm))
 def test_duplicate_unknown_bool_ids_never_accepted(self):
  for bad in ([{'id':0,'result':block(0)}]*2,[{'id':7,'result':block(0)}],[{'id':True,'result':block(0)}],[None]):
   ns,_,_=harness(lambda u,p:bad);self.assertEqual(self.run_batch(ns),{})
 def test_null_error_missing_and_wrong_block_stay_missing(self):
  ns,_,_=harness(lambda u,p:[{'id':0,'result':None},{'id':1,'result':block(1),'error':{}},{'id':2,'result':block(0)}])
  self.assertEqual(self.run_batch(ns),{});self.assertNotIn('batch',ns.get('_RPC_GOOD_ENDPOINTS',{}))
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
if __name__=='__main__':unittest.main()

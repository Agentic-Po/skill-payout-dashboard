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
  proof['prefix']={'start':5,'through':7,'hash':'0x'+'5'*64,'timestamp':'2026-10-09T00:00:00Z','target':9,'complete':False,'chunk':2}
  for artifact in ('data.json','full.html','coupon_data.json','coupon.html'):
   text=json.dumps(document)
   if artifact.endswith('.html'):text='<script>const DATA='+text+';</script>'
   self.assertEqual(P._status_adjacent(artifact,text),[],artifact)
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
 def prefix_fixture(self,root):
  scan,_,_=self.setup_scan(root);scan.budget_seconds=100
  scan.scan(W,'from',[T],20,60,chunk=2)
  path=os.path.join(root,S.scan_id(S.descriptor(W,'from',[T]))+'.json')
  return path,json.load(open(path))
 def test_earlier_floor_preserves_suffix_and_durable_prefix_before_join(self):
  with tempfile.TemporaryDirectory() as root:
   path,old=self.prefix_fixture(root)
   before={r['file']:open(os.path.join(root,S.scan_id(old['descriptor']),r['file']),'rb').read() for r in old['chunks']}
   scan,_,calls=self.setup_scan(root)
   with self.assertRaises(S.ScanIncomplete):scan.scan(W,'from',[T],10,60,chunk=2)
   pending=json.load(open(path));S.validate_store(root)
   for key in ('start','through','hash','timestamp','chunks'):self.assertEqual(pending[key],old[key])
   self.assertEqual(pending['prefix']['through'],11);self.assertFalse(scan.last[S.scan_id(old['descriptor'])]['complete'])
   self.assertEqual(scan.work[S.scan_id(old['descriptor'])]['remaining_blocks'],8)
   scan,_,calls=self.setup_scan(root);scan.budget_seconds=100
   rows=scan.scan(W,'from',[T],10,60,chunk=2)
   self.assertEqual(calls[0],(12,13));self.assertEqual(calls[-1],(18,19))
   joined=json.load(open(path));self.assertNotIn('prefix',joined);self.assertEqual(joined['start'],10);S.validate_store(root)
   self.assertEqual(joined['chunks'][-len(old['chunks']):],old['chunks'])
   for name,raw in before.items():self.assertEqual(open(os.path.join(root,S.scan_id(old['descriptor']),name),'rb').read(),raw)
   self.assertEqual(len(rows),len({S.log_key(row) for row in rows}))
 def test_prefix_provider_error_retains_original_suffix(self):
  with tempfile.TemporaryDirectory() as root:
   path,old=self.prefix_fixture(root);raw=open(path,'rb').read()
   scan,_,_=self.setup_scan(root);network=scan.rpc
   def fail(m,p):
    if m=='eth_getLogs':raise RuntimeError('provider refused')
    return network(m,p)
   scan.rpc=fail
   with self.assertRaises(RuntimeError):scan.scan(W,'from',[T],10,60,chunk=2)
   self.assertEqual(open(path,'rb').read(),raw);self.assertFalse(scan.last[S.scan_id(old['descriptor'])]['complete'])
 def test_prefix_manifest_failure_does_not_claim_uncommitted_progress(self):
  with tempfile.TemporaryDirectory() as root:
   path,old=self.prefix_fixture(root);raw=open(path,'rb').read();real=S.atomic_save
   def fail(destination,value):
    if destination==path:raise OSError('manifest crash')
    real(destination,value)
   scan,_,_=self.setup_scan(root)
   with patch.object(S,'atomic_save',side_effect=fail):
    with self.assertRaises(OSError):scan.scan(W,'from',[T],10,60,chunk=2)
   self.assertEqual(open(path,'rb').read(),raw);self.assertEqual(scan.work[S.scan_id(old['descriptor'])]['verified_blocks'],0)
   S.validate_store(root)
 def test_crash_before_join_replays_verified_prefix_without_querying_it(self):
  with tempfile.TemporaryDirectory() as root:
   path,old=self.prefix_fixture(root);real=S.atomic_save
   def fail(destination,value):
    if destination==path and value['start']==10 and 'prefix' not in value:raise OSError('join crash')
    real(destination,value)
   scan,_,_=self.setup_scan(root);scan.budget_seconds=100
   with patch.object(S,'atomic_save',side_effect=fail):
    with self.assertRaises(OSError):scan.scan(W,'from',[T],10,60,chunk=2)
   staged=json.load(open(path));self.assertTrue(staged['prefix']['complete']);S.validate_store(root)
   scan,_,calls=self.setup_scan(root);scan.budget_seconds=100
   scan.scan(W,'from',[T],10,60,chunk=2)
   self.assertEqual(calls,[]);self.assertNotIn('prefix',json.load(open(path)));S.validate_store(root)
 def test_final_completion_write_failure_cannot_claim_healthy_join(self):
  with tempfile.TemporaryDirectory() as root:
   path,old=self.prefix_fixture(root);real=S.atomic_save
   def fail(destination,value):
    if destination==path and value['start']==10 and 'prefix' not in value and value['complete']:raise OSError('completion crash')
    real(destination,value)
   scan,_,_=self.setup_scan(root);scan.budget_seconds=100
   with patch.object(S,'atomic_save',side_effect=fail):
    with self.assertRaises(OSError):scan.scan(W,'from',[T],10,60,chunk=2)
   pending=json.load(open(path));self.assertFalse(pending['complete']);self.assertEqual(pending['chunks'][-len(old['chunks']):],old['chunks']);S.validate_store(root)
   proof=scan.last[S.scan_id(old['descriptor'])];self.assertFalse(proof['complete'])
   with self.assertRaises(S.ScanIncomplete):S.require_coverage({'scope':{'complete':True,'source_coverage':{'coverage_ok':True,'pending':[proof]}}})
 def test_changed_prefix_or_suffix_anchor_never_joins(self):
  for changed in (11,30):
   with tempfile.TemporaryDirectory() as root:
    path,old=self.prefix_fixture(root);scan,_,_=self.setup_scan(root)
    with self.assertRaises(S.ScanIncomplete):scan.scan(W,'from',[T],10,60,chunk=2)
    raw=open(path,'rb').read();scan,_,_=self.setup_scan(root);scan.budget_seconds=100;network=scan.rpc
    def reorg(m,p):
     value=network(m,p)
     if m=='eth_getBlockByNumber' and int(p[0],16)==changed:value['hash']='0x'+'f'*64
     return value
    scan.rpc=reorg
    with self.assertRaises(S.ScanInvalid):scan.scan(W,'from',[T],10,60,chunk=2)
    self.assertEqual(open(path,'rb').read(),raw)
 def test_later_floor_never_resets_or_skips_gap(self):
  with tempfile.TemporaryDirectory() as root:
   path,old=self.prefix_fixture(root);scan,_,calls=self.setup_scan(root);scan.budget_seconds=100
   rows=scan.scan(W,'from',[T],35,70,chunk=2)
   self.assertEqual(calls[0],(31,32));self.assertEqual(json.load(open(path))['start'],20)
   self.assertTrue(all(row['block_number']>=35 for row in rows));S.validate_store(root)
   scan,_,calls=self.setup_scan(root);scan.budget_seconds=100;scan.scan(W,'from',[T],36,70,chunk=2)
   self.assertEqual(calls,[]);S.validate_store(root)
 def test_wider_floor_during_pending_prefix_finishes_then_extends(self):
  with tempfile.TemporaryDirectory() as root:
   path,old=self.prefix_fixture(root);scan,_,_=self.setup_scan(root)
   with self.assertRaises(S.ScanIncomplete):scan.scan(W,'from',[T],10,60,chunk=2)
   scan,_,calls=self.setup_scan(root);scan.budget_seconds=100;scan.scan(W,'from',[T],6,60,chunk=2)
   self.assertEqual(calls,[(12,13),(14,15),(16,17),(18,19),(6,7),(8,9)])
   self.assertEqual(json.load(open(path))['start'],6);S.validate_store(root)
 def test_prefix_schema_checksum_and_overlap_remain_strict(self):
  import copy
  with tempfile.TemporaryDirectory() as root:
   path,old=self.prefix_fixture(root);scan,_,_=self.setup_scan(root)
   with self.assertRaises(S.ScanIncomplete):scan.scan(W,'from',[T],10,60,chunk=2)
   valid=json.load(open(path))
   for mutate in (lambda p:p.update(complete=True),lambda p:p['prefix'].update(target=20),lambda p:p['prefix'].update(start=20),lambda p:p['prefix'].update(through=20),lambda p:p['prefix'].update(owner='private'),lambda p:p['prefix']['chunks'][0].update(checksum='0x'+'f'*64)):
    bad=copy.deepcopy(valid);mutate(bad);S.atomic_save(path,bad)
    with self.assertRaises(S.ScanInvalid):S.validate_store(root)
   S.atomic_save(path,valid);S.validate_store(root)
 def test_prefix_join_rechecks_suffix_after_prefix_scan(self):
  with tempfile.TemporaryDirectory() as root:
   path,old=self.prefix_fixture(root);scan,_,_=self.setup_scan(root);scan.budget_seconds=100;network=scan.rpc;count=[0]
   def changing(m,p):
    value=network(m,p)
    if m=='eth_getBlockByNumber' and int(p[0],16)==30:
     count[0]+=1
     if count[0]>1:value['hash']='0x'+'f'*64
    return value
   scan.rpc=changing
   with self.assertRaises(S.ScanInvalid):scan.scan(W,'from',[T],10,60,chunk=2)
   pending=json.load(open(path));self.assertEqual(pending['chunks'],old['chunks']);self.assertIn('prefix',pending)
   self.assertFalse(scan.last[S.scan_id(old['descriptor'])]['complete']);S.validate_store(root)
 def test_unjoined_prefix_rejects_forged_health(self):
  leg={'complete':True,'through':30,'target':30,'hash':'0x'+'a'*64,'prefix':{'through':11,'target':19}}
  with self.assertRaises(S.ScanIncomplete):S.require_coverage({'scope':{'complete':True,'source_coverage':{'coverage_ok':True,'pending':[leg]}}})
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
 def test_sink_both_directions_declared_before_first_failure(self):
  import ast
  source=open(os.path.join(os.path.dirname(os.path.dirname(__file__)),'refresh.py')).read();tree=ast.parse(source)
  with tempfile.TemporaryDirectory() as root:
   def fail(direction):raise S.ScanIncomplete('fixture first direction')
   ns={'_PUBLIC_SCANNER':None,'HERE':root,'rpc':None,'_shape_rpc_transfers':None,'RpcRangeError':RangeError,'os':os,'SINK':W,'TOKENS':{'MENTE':{'addr':T}},'_sweep':fail}
   helper=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_public_leg');exec(ast.get_source_segment(source,helper),ns)
   declarations=[n for n in ast.walk(tree) if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call) and isinstance(n.value.func,ast.Name) and n.value.func.id=='_public_leg' and isinstance(n.value.args[0],ast.Name) and n.value.args[0].id=='SINK' and isinstance(n.value.args[1],ast.Constant)]
   invocation=next(n for n in ast.walk(tree) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Tuple) and [getattr(e,'id',None) for e in t.elts]==['_in','_out'] for t in n.targets))
   nodes=sorted(declarations+[invocation],key=lambda n:n.lineno)
   with self.assertRaises(S.ScanIncomplete):exec(compile(ast.Module(body=nodes,type_ignores=[]),'sink_pair','exec'),ns)
   self.assertEqual(len(ns['_PUBLIC_SCANNER'].last),2)
   self.assertEqual({v['alias'] for v in ns['_PUBLIC_SCANNER'].work.values()},{'sink_in','sink_out'})
   self.assertTrue(all(not v['complete'] for v in ns['_PUBLIC_SCANNER'].last.values()))
 def test_neutral_work_counts_only_durable_empty_ranges_and_replay(self):
  with tempfile.TemporaryDirectory() as root:
   scanner,_,_=ScanTests().setup_scan(root);network=scanner.rpc
   scanner.rpc=lambda method,params:[] if method=='eth_getLogs' else network(method,params)
   scanner.scan(W,'from',[T],10,41,chunk=1)
   sid=S.scan_id(S.descriptor(W,'from',[T]));self.assertEqual(scanner.work[sid]['verified_blocks'],2)
   scanner.scan(W,'from',[T],10,41,chunk=1)
   self.assertEqual(scanner.work[sid]['verified_blocks'],2)
   self.assertEqual(scanner.work[sid]['through'],11);S.validate_store(root)
 def test_neutral_work_checkpoint_failure_has_no_verified_delta(self):
  from unittest.mock import patch
  with tempfile.TemporaryDirectory() as root:
   scanner,_,_=ScanTests().setup_scan(root);save=S.atomic_save
   def broken(path,value):
    if path.endswith(S.scan_id(S.descriptor(W,'from',[T]))+'.json'):raise OSError('fixture manifest failure')
    return save(path,value)
   with patch.object(S,'atomic_save',broken):
    with self.assertRaises(OSError):scanner.scan(W,'from',[T],10,40,chunk=1)
   work=scanner.work[S.scan_id(S.descriptor(W,'from',[T]))]
   self.assertEqual(work['verified_blocks'],0);self.assertEqual(work['through'],9)
 def test_neutral_active_charges_failed_bootstrap_once(self):
  clock=[0];active=S.ActiveScanClock(lambda:clock[0]);scanner=S.PublicScanner('unused',None,None,RangeError,clock=active);scanner.declare('fixture','treasury_out')
  with self.assertRaises(ValueError):
   with scanner.acquisition('fixture'):
    clock[0]+=2
    with scanner.acquisition('fixture'):
     clock[0]+=3;raise ValueError('fixture head')
  self.assertEqual(scanner.work['fixture']['active_s'],5);self.assertEqual(active(),5)
  self.assertEqual(scanner.work['fixture']['verified_blocks'],0);self.assertIsNone(scanner.work['fixture']['target'])
  self.assertEqual(scanner._acquiring['fixture'],0)
 def test_neutral_active_rejects_cross_source_nesting_and_sums_exactly(self):
  clock=[0];active=S.ActiveScanClock(lambda:clock[0]);scanner=S.PublicScanner('unused',None,None,RangeError,clock=active)
  with scanner.acquisition('first'):
   clock[0]+=2
   with self.assertRaises(RuntimeError):
    with scanner.acquisition('second'):self.fail('cross-source nesting allowed')
   with scanner.acquisition('first'):clock[0]+=3
  with scanner.acquisition('second'):clock[0]+=4
  self.assertEqual(sum(work['active_s'] for work in scanner.work.values()),active())
  self.assertEqual(active(),9);self.assertEqual(active.depth,0)
 def test_neutral_log_summary_omits_descriptor_and_unknown_coverage(self):
  import ast,types
  source=open(os.path.join(os.path.dirname(os.path.dirname(__file__)),'refresh.py')).read();tree=ast.parse(source)
  block=tree.body[-1];output=[]
  scanner=types.SimpleNamespace(clock=lambda:5,work={'first':{'alias':'sink_in','active_s':5,'verified_blocks':31,'through':100,'target':200,'descriptor':{'wallet':W,'private':'fixture secret'}},'second':{'alias':'sink_out','active_s':0,'verified_blocks':0,'through':None,'target':None}})
  ns={'OFFLINE':False,'_PUBLIC_SCANNER':scanner,'time':types.SimpleNamespace(monotonic=lambda:10),'_BUILD_STARTED':0,'print':lambda value:output.append(value)}
  exec(compile(ast.Module(body=[block],type_ignores=[]),'telemetry','exec'),ns)
  text='\n'.join(output)
  self.assertIn('declared_legs=2',text);self.assertIn('verified_blocks=31 through=100 target=200 remaining_blocks=100',text)
  self.assertIn('leg=sink_out active_s=0.0 verified_blocks=0 through=None target=None remaining_blocks=None',text)
  self.assertNotIn(W,text);self.assertNotIn('fixture secret',text);self.assertNotIn('descriptor',text)
 def test_active_clock_cumulative_idle_nested_and_exception(self):
  clock=[0];active=S.ActiveScanClock(lambda:clock[0])
  with active.active():
   clock[0]+=40
   with active.active():clock[0]+=10
  self.assertEqual(active(),50)
  clock[0]+=131
  self.assertEqual(active(),50)
  with self.assertRaises(ValueError):
   with active.active():
    clock[0]+=30
    with active.active():
     clock[0]+=20
     raise ValueError('fixture')
  self.assertEqual(active(),100);self.assertEqual(active.depth,0);self.assertIsNone(active.started)
  with active.active():clock[0]+=80
  self.assertEqual(active(),180)
  self.assertEqual(active.depth,0)
 def test_fallback_scopes_bootstrap_materialization_hash_and_checkpoint(self):
  import ast
  source=open(os.path.join(os.path.dirname(os.path.dirname(__file__)),'refresh.py')).read();tree=ast.parse(source)
  with tempfile.TemporaryDirectory() as root:
   scanner,_,_=ScanTests().setup_scan(root);clock=[0];active=S.ActiveScanClock(lambda:clock[0]);scanner.clock=active;scanner.budget_seconds=180
   network=scanner.rpc;shape=scanner.materialize;phases=[]
   def rpc(method,params):
    self.assertGreater(active.depth,0);phases.append(method);clock[0]+=1
    return hex(41) if method=='eth_blockNumber' else network(method,params)
   scanner.rpc=rpc
   def materialize(logs):
    self.assertGreater(active.depth,0);phases.append('materialize');clock[0]+=1;return shape(logs)
   scanner.materialize=materialize
   original=S.atomic_save
   def save(*args):
    self.assertGreater(active.depth,0);phases.append('checkpoint');clock[0]+=1;return original(*args)
   ns={'_PUBLIC_SCANNER':scanner,'_RPC_LAST_THROUGH':None,'rpc':rpc,'HERE':root,'_shape_rpc_transfers':shape,'RpcRangeError':RangeError,'os':os,'LOGS_CHUNK':[1]}
   for name in ('_public_leg','_scan_rpc','rpc_transfer_fallback'):
    node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name);exec(ast.get_source_segment(source,node),ns)
   from unittest.mock import patch
   with patch.object(S,'atomic_save',save):
    self.assertTrue(ns['rpc_transfer_fallback'](W,'from',[T],10,to_block=10))
    first=active();clock[0]+=131
    self.assertTrue(ns['rpc_transfer_fallback'](W,'from',[T],11,to_block=11))
   self.assertGreater(active(),first);self.assertLess(active(),180)
   self.assertEqual(scanner.deadline,180);self.assertEqual(active.depth,0)
   self.assertIn('materialize',phases);self.assertIn('checkpoint',phases);self.assertIn('eth_getBlockByNumber',phases)
   S.validate_store(root)
 def test_outer_wall_guard_and_idle_active_timeout(self):
  import ast,types
  source=open(os.path.join(os.path.dirname(os.path.dirname(__file__)),'refresh.py')).read()
  clock=[0];active=S.ActiveScanClock(lambda:clock[0])
  scanner=types.SimpleNamespace(clock=active,deadline=360)
  ns={'time':types.SimpleNamespace(monotonic=lambda:clock[0]),'_BUILD_STARTED':0,'_NETWORK_DEADLINE':None,'_MARKET_DEADLINE':None,'_PUBLIC_SCANNER':scanner}
  node=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='_network_timeout');exec(ast.get_source_segment(source,node),ns)
  with active.active():
   clock[0]+=40
   self.assertEqual(ns['_network_timeout'](400),320)
  clock[0]+=131
  self.assertEqual(ns['_network_timeout'](200),200);self.assertEqual(active(),40)
  with active.active():
   self.assertEqual(ns['_network_timeout'](400),320)
   self.assertEqual(ns['_network_timeout'](100,market=True),60)
   clock[0]+=320
   with self.assertRaises(S.BudgetExpired):ns['_network_timeout'](1)
  self.assertEqual(active(),360)
  self.assertEqual(ns['_network_timeout'](10),10)
  clock[0]=540
  with self.assertRaises(S.BudgetExpired):ns['_network_timeout'](1)
  with self.assertRaises(S.BudgetExpired):ns['_network_timeout'](100,market=True)
  ns['_MARKET_DEADLINE']=None
  self.assertEqual(ns['_network_timeout'](100,market=True),60)
 def test_live_limits_and_socket_pacing_clamp_to_active_and_outer(self):
  import ast,types
  source=open(os.path.join(os.path.dirname(os.path.dirname(__file__)),'refresh.py')).read();tree=ast.parse(source)
  with tempfile.TemporaryDirectory() as root:
   ns={'_PUBLIC_SCANNER':None,'HERE':root,'rpc':None,'_shape_rpc_transfers':None,'RpcRangeError':RangeError,'os':os}
   node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_public_leg');exec(ast.get_source_segment(source,node),ns);ns['_public_leg'](W,'from',[T])
   self.assertEqual(ns['_PUBLIC_SCANNER'].budget_seconds,360);self.assertEqual(ns['_PUBLIC_SCANNER'].deadline,360);self.assertAlmostEqual(ns['_PUBLIC_SCANNER'].leg_budget_seconds,360/7)
   ns['_PUBLIC_SCANNER'].clock.used=180;ns['_public_leg'](W,'to',[T])
   self.assertEqual(ns['_PUBLIC_SCANNER'].deadline-ns['_PUBLIC_SCANNER'].clock(),180)
  clock=[531];active=S.ActiveScanClock(lambda:clock[0]);active.used=359
  scanner=types.SimpleNamespace(clock=active,deadline=360)
  ns={'time':types.SimpleNamespace(monotonic=lambda:clock[0],sleep=lambda n:clock.__setitem__(0,clock[0]+n)),'_BUILD_STARTED':0,'_NETWORK_DEADLINE':None,'_MARKET_DEADLINE':None,'_PUBLIC_SCANNER':scanner}
  for name in ('_network_timeout','_network_sleep'):
   node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name);exec(ast.get_source_segment(source,node),ns)
  with active.active():
   self.assertEqual(ns['_network_timeout'](20),1)
   with active.active():ns['_network_sleep'](2)
   with self.assertRaises(S.BudgetExpired):ns['_network_timeout'](20)
  self.assertEqual(active(),360)
  clock[0]=539;active.used=10
  with active.active():self.assertEqual(ns['_network_timeout'](20),1)
  clock[0]=540
  with self.assertRaises(S.BudgetExpired):ns['_network_timeout'](20)
  self.assertEqual(ns['_network_timeout'](100,market=True),60)
 def test_active_clock_rejects_other_thread_scope(self):
  import threading
  active=S.ActiveScanClock();errors=[]
  def attempt():
   try:
    with active.active():pass
   except RuntimeError as error:errors.append(error)
  with active.active():
   thread=threading.Thread(target=attempt);thread.start();thread.join()
   self.assertEqual(active.depth,1)
  self.assertEqual(len(errors),1);self.assertEqual(active.depth,0)
 def test_online_acquisition_timeouts_share_one_budget(self):
  import ast,types
  source=open(os.path.join(os.path.dirname(os.path.dirname(__file__)),'refresh.py')).read();tree=ast.parse(source)
  clock=[0];calls=[]
  def urlopen(req,timeout):
   calls.append(timeout);clock[0]+=timeout;raise TimeoutError('fixture timeout')
  ns={'time':types.SimpleNamespace(monotonic=lambda:clock[0],sleep=lambda n:clock.__setitem__(0,clock[0]+n)),'urllib':types.SimpleNamespace(request=types.SimpleNamespace(Request=lambda *a,**k:None,urlopen=urlopen)),'_BUILD_STARTED':0,'_NETWORK_DEADLINE':None,'_MARKET_DEADLINE':None,'_V2_FAILS':[0],'V2_TRIP':2,'json':json}
  for name in ('_network_timeout','_network_sleep','get'):
   node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name);exec(ast.get_source_segment(source,node),ns)
  with self.assertRaises(TimeoutError):ns['get']('https://public.invalid')
  with self.assertRaises(TimeoutError):ns['get']('https://public.invalid')
  with self.assertRaises(S.BudgetExpired):ns['get']('https://public.invalid')
  self.assertEqual(clock[0],540);self.assertTrue(all(n<=60 for n in calls))
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
  clock[0]=540;calls.clear()
  with self.assertRaises(TimeoutError):ns['get']('https://api.geckoterminal.com/api/v2/fixture')
  self.assertTrue(calls);self.assertLessEqual(clock[0],600)
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
  ns={'_sink_st':old,'COLLECTOR':W,'SINK':W,'TOKENS':{'MENTE':{'addr':T}},'DECIMALS':{'MENTE':18},'get':lambda u:{'items':[{'timestamp':'2026-10-09T00:00:00Z','total':{'value':'1','decimals':18},'token':{'address_hash':T},'from':{'hash':W},'to':{'hash':W},'block_number':20,'transaction_hash':'new','log_index':0}]},'key':lambda r:r['transaction_hash'],'xcheck_from':lambda *a:10,'SINK_GENESIS_BLOCK':1,'XCHECK_WINDOW':2,'_scan_rpc':lambda *a:hex(100),'rpc':lambda *a:hex(100),'SINK_XC_LAG':30,'SINK_XC_BUDGET':50,'rpc_transfer_fallback':incomplete,'xcheck_done':lambda *a:self.fail('partial cursor advanced'),'_public_leg':lambda *a:None}
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
   ns['_scan_rpc']=rpc
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

class FairAdaptiveTests(unittest.TestCase):
 def seed(self,root):
  scan,_,_=ScanTests().setup_scan(root);scan.budget_seconds=100;scan.scan(W,'from',[T],10,101,chunk=31)
  return json.load(open(os.path.join(root,S.scan_id(S.descriptor(W,'from',[T]))+'.json')))
 def test_persisted_small_range_recovers_with_successes_and_never_exceeds_cap(self):
  with tempfile.TemporaryDirectory() as root:
   old=self.seed(root);scan,_,calls=ScanTests().setup_scan(root);scan.budget_seconds=10000
   scan.scan(W,'from',[T],10,24030,chunk=9000);sizes=[hi-lo+1 for lo,hi in calls]
   self.assertEqual(sizes[0],31);self.assertIn(62,sizes);self.assertIn(2000,sizes);self.assertLessEqual(max(sizes),2000)
   state=json.load(open(os.path.join(root,S.scan_id(S.descriptor(W,'from',[T]))+'.json')));self.assertEqual(state['chunks'][:len(old['chunks'])],old['chunks']);S.validate_store(root)
 def test_refused_growth_cools_down_for_entire_process_leg(self):
  with tempfile.TemporaryDirectory() as root:
   self.seed(root);scan,_,calls=ScanTests().setup_scan(root,limit=31);scan.budget_seconds=10000
   scan.scan(W,'from',[T],10,1530,chunk=2000);scan.scan(W,'from',[T],10,2530,chunk=2000)
   self.assertEqual(sum(hi-lo+1>31 for lo,hi in calls),1);S.validate_store(root)
 def test_growth_refusal_cooldown_survives_prefix_join_and_tail(self):
  with tempfile.TemporaryDirectory() as root:
   seed,_,_=ScanTests().setup_scan(root);seed.budget_seconds=10000;seed.scan(W,'from',[T],1000,1130,chunk=31)
   scan,_,calls=ScanTests().setup_scan(root,limit=31);scan.budget_seconds=10000;scan.scan(W,'from',[T],1,2030,chunk=2000)
   self.assertEqual(sum(hi-lo+1>31 for lo,hi in calls),1);self.assertTrue(any(lo>1100 for lo,hi in calls));S.validate_store(root)
   state=json.load(open(os.path.join(root,S.scan_id(S.descriptor(W,'from',[T]))+'.json')));self.assertEqual(state['start'],1);self.assertNotIn('prefix',state)
 def test_uncommitted_ranges_do_not_trigger_growth_or_cursor_advance(self):
  with tempfile.TemporaryDirectory() as root:
   old=self.seed(root);scan,_,calls=ScanTests().setup_scan(root);scan.budget_seconds=10000;save=S.atomic_save;writes=[0]
   def fail(path,value):
    if path.endswith('.json') and isinstance(value,dict):
     writes[0]+=1
     if writes[0]==4:raise OSError('synthetic manifest failure')
    return save(path,value)
   with patch.object(S,'atomic_save',fail),self.assertRaises(OSError):scan.scan(W,'from',[T],10,1030,chunk=2000)
   self.assertTrue(all(hi-lo+1==31 for lo,hi in calls));S.validate_store(root)
   state=json.load(open(os.path.join(root,S.scan_id(S.descriptor(W,'from',[T]))+'.json')));self.assertEqual(state['through'],old['through']+93);self.assertFalse(state['complete'])
 def test_actual_fallback_reserves_each_leg_and_repeated_leg_cannot_reset(self):
  import ast
  source=open(os.path.join(os.path.dirname(os.path.dirname(__file__)),'refresh.py')).read();tree=ast.parse(source)
  with tempfile.TemporaryDirectory() as root:
   wall=[0];active=S.ActiveScanClock(lambda:wall[0]);calls=[]
   def rpc(method,params):
    if scanner.expired():raise S.BudgetExpired()
    wall[0]+=1;calls.append(method)
    if method=='eth_blockNumber':return hex(100)
    if method=='eth_getLogs':return []
    return {'number':params[0],'hash':'0x'+format(int(params[0],16),'064x'),'timestamp':hex(100+int(params[0],16))}
   scanner=S.PublicScanner(root,rpc,lambda logs:[],RangeError,budget_seconds=28,clock=active,leg_budget_seconds=4)
   ns={'_PUBLIC_SCANNER':scanner,'_RPC_LAST_THROUGH':None,'rpc':rpc,'HERE':root,'_shape_rpc_transfers':lambda logs:[],'RpcRangeError':RangeError,'os':os,'LOGS_CHUNK':[1]}
   for name in ('_public_leg','_scan_rpc','rpc_transfer_fallback'):
    node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name);exec(ast.get_source_segment(source,node),ns)
   for index in range(7):
    wallet='0x'+format(index+1,'040x')
    with self.assertRaises(S.ScanIncomplete):ns['rpc_transfer_fallback'](wallet,'from',[T],10)
   self.assertEqual(len(scanner.last),7);self.assertTrue(all(p.get('through')==10 and p['complete'] is False for p in scanner.last.values()));self.assertEqual(active(),28)
   before=len(calls)
   with self.assertRaises(S.ScanIncomplete):ns['rpc_transfer_fallback'](W,'from',[T],10)
   self.assertEqual(len(calls),before);S.validate_store(root)
 def test_per_leg_timeout_and_nested_exception_accounting(self):
  import ast,types
  clock=[0];active=S.ActiveScanClock(lambda:clock[0]);scanner=S.PublicScanner('unused',None,None,RangeError,budget_seconds=360,clock=active,leg_budget_seconds=360/7);scanner.deadline=360
  source=open(os.path.join(os.path.dirname(os.path.dirname(__file__)),'refresh.py')).read();node=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='_network_timeout');ns={'time':types.SimpleNamespace(monotonic=lambda:clock[0]),'_NETWORK_DEADLINE':None,'_MARKET_DEADLINE':None,'_BUILD_STARTED':0,'_PUBLIC_SCANNER':scanner};exec(ast.get_source_segment(source,node),ns)
  with self.assertRaises(ValueError):
   with scanner.acquisition('first'):
    clock[0]+=50
    with scanner.acquisition('first'):
     self.assertAlmostEqual(ns['_network_timeout'](20),360/7-50);raise ValueError('fixture')
  self.assertEqual(scanner.work['first']['active_s'],50)
  with scanner.acquisition('first'):self.assertAlmostEqual(scanner.remaining(),360/7-50)
  with scanner.acquisition('second'):self.assertAlmostEqual(ns['_network_timeout'](100),360/7)

if __name__=='__main__':unittest.main()

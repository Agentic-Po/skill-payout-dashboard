"""Different-clock finance must retain provenance without weakening live checks."""
import ast,copy,importlib.util,json,pathlib,subprocess,sys,tempfile,unittest
from datetime import datetime,timedelta
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));import sanity as S
class Evidence(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=pathlib.Path(self.tmp.name)
  self.prior={'scope':{'wallet':'0x'+'1'*40,'tokens':{'MOCA':'0x'+'2'*40},'generated_iso':'2026-10-10T00:00:00Z'},'facts':{'balance':{'MOCA':100},'balance_usd':{'MOCA':100},'rate':{'MOCA':1},'rate_src':{'MOCA':'dexscreener'},'float':{'days_7d_pace':7}}}
  self.cp={'scope':{'wallet':'0x'+'3'*40,'token':{'MOCA':'0x'+'2'*40},'generated_iso':'2026-10-10T00:00:00Z','complete':False,'build_generated_iso':'2026-10-10T01:01:00Z','rate':1,'rate_src':'dexscreener'},'totals':{'balance_moca':50,'balance_usd':50,'weeks_left':2}}
  for f,d in [('data.json',self.prior),('coupon_data.json',self.cp)]: (self.root/f).write_text(json.dumps(d))
  subprocess.run(['git','init','-q',str(self.root)],check=True);subprocess.run(['git','-C',str(self.root),'add','.'],check=True);subprocess.run(['git','-C',str(self.root),'-c','user.name=Synthetic QA','-c','user.email=qa@example.invalid','commit','-qm','fixture'],check=True)
  self.sha=subprocess.check_output(['git','-C',str(self.root),'rev-parse','HEAD'],text=True).strip();self.D=copy.deepcopy(self.prior)
  self.obs={'started_iso':'2026-10-10T01:00:00Z','rate':{'MOCA':1},'rate_src':{'MOCA':'dexscreener'},'balance':{'MOCA':200},'balance_usd':{'MOCA':200},'rate_observed_iso':{'MOCA':'2026-10-10T01:00:01Z'},'balance_observed_iso':{'MOCA':'2026-10-10T01:00:02Z'},'retained_from':{'commit':self.sha,'generated_iso':self.prior['scope']['generated_iso']}}
  self.D['scope'].update(complete=False,source_coverage={'treasury_complete':False},wallet='0x'+'1'*40,tokens={'MOCA':'0x'+'2'*40},build_generated_iso='2026-10-10T01:01:00Z',balance_observations=self.obs)
 def complete_proofs(self):
  import hashlib
  from public_scan import descriptor,scan_id,PublicScanner
  directory=self.root/'pending_scans';directory.mkdir(exist_ok=True);legs=[]
  for direction in ('from','to'):
   desc=descriptor(self.D['scope']['wallet'],direction,list(self.D['scope']['tokens'].values()));sid=scan_id(desc);(directory/sid).mkdir(exist_ok=True)
   raw=b'[]';(directory/sid/'1-2.json').write_bytes(raw)
   state={'descriptor':desc,'start':1,'through':2,'target':2,'hash':'0x'+'a'*64,'timestamp':'2026-10-10T01:00:00Z','complete':True,'chunk':1,'chunks':[{'file':'1-2.json','checksum':'0x'+hashlib.sha256(raw).hexdigest()}]}
   (directory/(sid+'.json')).write_text(json.dumps(state));legs.append(PublicScanner.proof(state))
  self.D['scope']['source_coverage']['pending']=legs
 def test_removed_marker_and_flipped_flag_require_durable_proofs(self):
  self.D['scope'].pop('balance_observations');self.D['scope']['source_coverage']['treasury_complete']=True
  self.assertTrue(self.check()[0].blocked());self.complete_proofs();self.assertFalse(self.check()[0].blocked())
  leg=self.D['scope']['source_coverage']['pending'][0];leg['through']=3;self.assertTrue(self.check()[0].blocked())
 def test_complete_checkpoint_corruption_missing_direction_and_prefix_fail_closed(self):
  self.D['scope'].pop('balance_observations');self.D['scope']['source_coverage']['treasury_complete']=True
  self.complete_proofs();legs=self.D['scope']['source_coverage']['pending'];self.D['scope']['source_coverage']['pending']=legs[:1];self.assertTrue(self.check()[0].blocked())
  self.D['scope']['source_coverage']['pending']=legs;leg=legs[0];leg['prefix']={};self.assertTrue(self.check()[0].blocked());leg.pop('prefix')
  import public_scan
  (self.root/'pending_scans'/public_scan.scan_id(leg['descriptor'])/'1-2.json').write_text('[{}]');self.assertTrue(self.check()[0].blocked())
 def check(self):
  r=S.Report();d,retained=S.check_observations(r,self.D,['MOCA'],False,str(self.root));return r,d,retained
 def test_real_prior_blob_and_current_balance_have_separate_clocks(self):
  r,d,retained=self.check();self.assertTrue(retained);self.assertFalse(r.blocked());self.assertEqual(d['facts']['balance_usd']['MOCA'],200);self.assertEqual(self.D['facts']['balance_usd']['MOCA'],100)
  with patch.object(S,'_eth_call_balance',return_value=200),patch.object(S,'_dexscreener_price',return_value=1):live=S.check_balance(r,d,['MOCA'],False)
  gen=datetime(2026,10,10);rows=[{'ts':(gen-timedelta(days=8)).isoformat(),'usd':0},{'ts':(gen-timedelta(days=1)).isoformat(),'usd':100}]
  S.check_runway(r,self.D,rows,['MOCA'],gen,{} if retained else live);self.assertFalse(r.blocked())
 def test_retained_corruption_blocks_against_actual_committed_evidence(self):
  for field in ('balance','balance_usd','rate','rate_src'):
   with self.subTest(field=field):
    old=self.D['facts'][field]['MOCA'];self.D['facts'][field]['MOCA']='bad' if field=='rate_src' else 999;r,_,_=self.check();self.assertTrue(r.blocked());self.D['facts'][field]['MOCA']=old
 def test_retained_wallet_and_token_identity_mutation_blocks(self):
  for field,value in [('wallet','0x'+'4'*40),('tokens',{'MOCA':'0x'+'5'*40})]:
   original=self.D['scope'][field];self.D['scope'][field]=value;self.assertTrue(self.check()[0].blocked());self.D['scope'][field]=original
 def test_retained_flag_flip_cannot_bypass_immutable_binding(self):
  self.D['scope']['source_coverage']['treasury_complete']=True;self.D['facts']['balance_usd']['MOCA']=200;self.assertTrue(self.check()[0].blocked())
 def test_fresh_balance_and_quote_corruption_keep_original_bounds(self):
  for bal,rate in ((400,1),(200,2)):
   r,d,_=self.check()
   with patch.object(S,'_eth_call_balance',return_value=bal),patch.object(S,'_dexscreener_price',return_value=rate):S.check_balance(r,d,['MOCA'],False)
   self.assertTrue(r.blocked())
 def test_missing_reference_marker_and_invalid_sha_block(self):
  for ref in (None,{'commit':'--anything','generated_iso':'2026-10-10T00:00:00Z'},{'commit':'a'*40,'generated_iso':'2026-10-10T00:00:00Z'}):
   with self.subTest(ref=ref):
    if ref is None:self.obs.pop('retained_from',None)
    else:self.obs['retained_from']=ref
    with patch.object(S,'_prior_document',side_effect=OSError('unavailable')):self.assertTrue(self.check()[0].blocked())
 def test_each_token_clock_missing_bad_future_or_fallback_blocks(self):
  for field,value in [('balance_observed_iso',{}),('rate_observed_iso',{'MOCA':'2026-10-10T02:00:00Z'}),('rate_observed_iso',{'MOCA':'2026-99-10T01:00:00Z'}),('balance',{'MOCA':None}),('rate',{'MOCA':float('nan')}),('rate_src',{'MOCA':'last-accepted'})]:
   with self.subTest(field=field):
    old=self.obs[field];self.obs[field]=value;self.assertTrue(self.check()[0].blocked());self.obs[field]=old
 def test_financial_build_future_and_retained_after_acquisition_block(self):
  for field,value in [('generated_iso','2026-10-10T01:02:00Z'),('build_generated_iso','2099-01-01T00:00:00Z')]:
   original=self.D['scope'][field];self.D['scope'][field]=value;self.assertTrue(self.check()[0].blocked());self.D['scope'][field]=original
  self.obs['retained_from']['generated_iso']='2026-10-10T01:00:30Z';self.assertTrue(self.check()[0].blocked())
 def test_missing_second_token_timestamp_blocks(self):
  for field in ('rate','balance','balance_usd','rate_src','rate_observed_iso','balance_observed_iso'):self.obs[field]['MENTE']=self.obs[field]['MOCA']
  self.obs['balance_observed_iso'].pop('MENTE');r=S.Report();S.check_observations(r,self.D,['MOCA','MENTE'],False,str(self.root));self.assertTrue(r.blocked())
 def test_complete_path_checks_published_facts_not_substituted_observation(self):
  self.complete_proofs();self.obs.pop('retained_from');self.D['scope']['source_coverage']['treasury_complete']=True;self.D['scope'].update(complete=True,generated_iso='2026-10-10T01:01:00Z');r,d,retained=self.check();self.assertFalse(retained);self.assertIs(d,self.D)
  with patch.object(S,'_eth_call_balance',return_value=200),patch.object(S,'_dexscreener_price',return_value=1):S.check_balance(r,d,['MOCA'],False)
  self.assertTrue(r.blocked())
 def test_fresh_treasury_with_incomplete_subsidiaries_uses_original_check(self):
  self.complete_proofs();self.obs.pop('retained_from');self.D['scope']['source_coverage']['treasury_complete']=True;self.D['scope']['generated_iso']='2026-10-10T01:01:00Z';self.D['facts'].update(balance={'MOCA':200},balance_usd={'MOCA':200});r,d,retained=self.check();self.assertFalse(retained)
  with patch.object(S,'_eth_call_balance',return_value=200),patch.object(S,'_dexscreener_price',return_value=1):S.check_balance(r,d,['MOCA'],False)
  self.assertFalse(r.blocked())
 def test_original_complete_provider_warn_behavior_and_retained_missing_evidence(self):
  self.complete_proofs();self.D['scope'].update(complete=True);self.D['scope']['source_coverage']['treasury_complete']=True;self.D['scope'].pop('balance_observations');r,d,retained=self.check()
  with patch.object(S,'_eth_call_balance',side_effect=OSError('provider')),patch.object(S,'_dexscreener_price',side_effect=OSError('provider')):S.check_balance(r,d,['MOCA'],False)
  self.assertFalse(r.blocked());self.assertTrue(r.warns)
  self.D['scope'].update(complete=False);self.D['scope']['source_coverage']['treasury_complete']=False;self.assertTrue(self.check()[0].blocked())
 def test_coupon_exact_previous_blob_and_missing_marker_block(self):
  current=copy.deepcopy(self.cp);current['scope']['valuation_from']={'commit':self.sha,'generated_iso':self.cp['scope']['generated_iso']};p=self.root/'coupon_data.json';p.write_text(json.dumps(current));r=S.Report();S.check_coupon_valuation(r,str(self.root),False);self.assertFalse(r.blocked())
  current['totals']['balance_usd']=999;p.write_text(json.dumps(current));r=S.Report();S.check_coupon_valuation(r,str(self.root),False);self.assertTrue(r.blocked())
  current['scope'].pop('valuation_from');p.write_text(json.dumps(current));r=S.Report();S.check_coupon_valuation(r,str(self.root),False);self.assertTrue(r.blocked())
 def test_git_reference_preferred_and_invalid_path_never_networks(self):
  with patch.object(S.urllib.request,'build_opener',side_effect=AssertionError('network')):self.assertEqual(S._prior_document(str(self.root),self.sha),self.prior)
  for sha,path in [('bad','data.json'),(self.sha,'../secret'),(self.sha,'data.json:other')]:
   with self.assertRaises(ValueError):S._prior_document(str(self.root),sha,path)
 def test_shallow_reference_fixed_bounded_fallback_and_size(self):
  class Response:
   def __enter__(self):return self
   def __exit__(self,*a):pass
   def geturl(self):return 'https://raw.githubusercontent.com/Agentic-Po/skill-payout-dashboard/'+'a'*40+'/data.json'
   def read(self,n):self.limit=n;return json.dumps({'scope':{}}).encode()
  response=Response()
  with patch.object(S.subprocess,'check_output',side_effect=subprocess.CalledProcessError(1,['git'])),patch.object(S.urllib.request,'build_opener') as make:
   make.return_value.open.return_value=response;self.assertEqual(S._prior_document(str(self.root),'a'*40),{'scope':{}});args=make.return_value.open.call_args;self.assertEqual(args.kwargs['timeout'],10);self.assertEqual(response.limit,S._PRIOR_LIMIT+1)
   response.read=lambda n:b'x'*(S._PRIOR_LIMIT+1)
   with self.assertRaises(ValueError):S._prior_document(str(self.root),'a'*40)
 def test_remote_redirect_and_malformed_document_are_rejected(self):
  class Response:
   def __enter__(self):return self
   def __exit__(self,*a):pass
   def geturl(self):return self.url
   def read(self,n):return self.raw
  response=Response();response.url='https://raw.githubusercontent.com/Agentic-Po/skill-payout-dashboard/'+'a'*40+'/data.json';response.raw=b'not json'
  with patch.object(S.subprocess,'check_output',side_effect=subprocess.CalledProcessError(1,['git'])),patch.object(S.urllib.request,'build_opener') as make:
   make.return_value.open.return_value=response
   with self.assertRaises(ValueError):S._prior_document(str(self.root),'a'*40)
   handler=make.call_args.args[0]
   with self.assertRaises(ValueError):handler.redirect_request(None,None,302,None,None,'https://example.invalid/')
   response.raw=b'{}';response.url='https://example.invalid/'
   with self.assertRaises(ValueError):S._prior_document(str(self.root),'a'*40)
 def test_remote_redirect_malformed_unavailable_fail_closed(self):
  for error in (ValueError('redirect'),OSError('unavailable')):
   with patch.object(S._prior_document.__globals__['subprocess'],'check_output',side_effect=error):self.assertTrue(self.check()[0].blocked())
 def test_public_observation_schema_passes_original_status_tripwire(self):
  import check_publish
  d=json.loads((ROOT/'data.json').read_text());d['scope']['source_coverage']['treasury_complete']=False;d['scope']['balance_observations']=copy.deepcopy(self.obs);d['scope']['balance_observations']['retained_from']['commit']=self.sha
  cp=json.loads((ROOT/'coupon_data.json').read_text());cp['scope']['valuation_from']={'commit':self.sha,'generated_iso':cp['scope']['generated_iso']}
  for filename,value in [('data.json',d),('full.html',d),('coupon_data.json',cp),('coupon.html',cp)]:self.assertEqual(check_publish._status_adjacent(filename,json.dumps(value)),[])
 def test_actual_coupon_mixed_clocks_quotes_and_no_treasury_alias_mutation(self):
  from datetime import timezone
  import types
  source=(ROOT/'refresh.py').read_text();tree=ast.parse(source)
  node=next(n for n in ast.walk(tree) if isinstance(n,ast.If) and ast.get_source_segment(source,n.test)=='cp_complete' and any(isinstance(x,ast.For) and ast.get_source_segment(source,x.iter)=='_COUPON_STAGE' for x in n.body))
  copy_node=next(n for n in ast.walk(tree) if isinstance(n,ast.Assign) and ast.get_source_segment(source,n).startswith('RATE, RATE_SRC = dict(RATE)'))
  class Clock:
   @staticmethod
   def now(tz):return datetime(2026,10,10,2,tzinfo=timezone.utc)
   strptime=datetime.strptime
  for complete in (True,False):
   treasury_rate={'MOCA':1};treasury_src={'MOCA':'saved'};oldnow=datetime(2026,10,10,0,tzinfo=timezone.utc)
   ns={'cp_complete':complete,'RATE':treasury_rate,'RATE_SRC':treasury_src,'now':oldnow,'datetime':Clock,'timezone':timezone,'timedelta':timedelta,'_BALANCE_OBSERVATIONS':{'rate':{'MOCA':2},'rate_src':{'MOCA':'dexscreener'}},'_COUPON_STAGE':[],'_coupon_old_out':[],'_coupon_old_in':[],'STATE':{'xcheck':{}},'_coupon_old_xcheck':{},'cp_cursor':{},'_coupon_old_cursor':{},'cp_verified':{},'_coupon_old_verified':{},'_LAST_COUPON':{'scope':{'generated_iso':'2026-10-10T01:00:00Z','rate':0.5,'rate_src':'prior coupon'}}}
   exec(compile(ast.Module(body=[copy_node,node],type_ignores=[]),'coupon','exec'),ns)
   self.assertEqual(treasury_rate,{'MOCA':1});self.assertEqual(treasury_src,{'MOCA':'saved'});self.assertEqual(ns['RATE']['MOCA'],2 if complete else 0.5);self.assertEqual(ns['now'].hour,2 if complete else 1)
 def test_actual_refresh_capture_precedes_retention_and_keeps_fresh_maps(self):
  source=(ROOT/'refresh.py').read_text();tree=ast.parse(source);nodes=[n for n in tree.body if isinstance(n,ast.If) and any(isinstance(x,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='_BALANCE_OBSERVATIONS' for t in x.targets) for x in n.body)];self.assertEqual(len(nodes),1)
  class Clock:
   @staticmethod
   def now(tz):return datetime.fromisoformat('2026-10-10T01:00:00+00:00')
  ns={'OFFLINE':False,'RATE':{'MOCA':2},'RATE_SRC':{'MOCA':'dexscreener'},'BALANCE':{'MOCA':200},'TOKENS':{'MOCA':{}},'_OBSERVATIONS_STARTED':'2026-10-10T01:00:00Z','_RATE_OBSERVED':{'MOCA':'2026-10-10T01:00:00Z'},'_BALANCE_OBSERVED':{'MOCA':'2026-10-10T01:00:00Z'},'subprocess':subprocess,'HERE':str(self.root)}
  exec(compile(ast.Module(body=nodes,type_ignores=[]),'capture','exec'),ns);ns['RATE']['MOCA']=1;ns['BALANCE']['MOCA']=100;self.assertEqual(ns['_BALANCE_OBSERVATIONS']['balance_usd']['MOCA'],400);self.assertEqual(ns['_BALANCE_BASE_COMMIT'],self.sha)
if __name__=='__main__':unittest.main()

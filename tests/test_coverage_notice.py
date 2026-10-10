import copy
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('coverage_notice',ROOT/'tools/coverage_notice.py');N=importlib.util.module_from_spec(spec);spec.loader.exec_module(N)
class Notice(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=pathlib.Path(self.tmp.name);self.addCleanup(self.tmp.cleanup)
  self.legs=[dict(descriptor=N.descriptor(w,d,t),start=10,through=20,target=30,hash='0x'+'a'*64,timestamp='2026-10-09T23:00:00Z',complete=False) for _,w,d,t in N.SOURCES]
  self.docs=[{'scope':{'generated_iso':'2026-10-10T00:50:55Z','complete':False,'source_coverage':{'coverage_ok':False,'pending':self.legs}}} for _ in range(2)]
 def render(self):
  for name,d in zip(('data.json','coupon_data.json'),self.docs):(self.root/name).write_text(json.dumps(d))
  return N.summary(self.root,'2026-10-10T01:00:00Z')
 def test_sgt_rollover_lag_and_separate_finance_clock(self):
  text=self.render();self.assertIn('10 Oct 2026 07:00 SGT · 2h 00m behind',text);self.assertIn('saved figures built: 10 Oct 2026 08:50 SGT (incomplete; build time, not chain coverage)',text);self.assertEqual(text.count('saved checkpoint'),7);self.assertNotIn('All required sources',text)
 def test_all_seven_exact_config_aliases_no_addresses(self):
  text=self.render()
  for alias,w,_,tokens in N.SOURCES:
   self.assertEqual(text.count(alias+':'),1);self.assertNotIn(w,text)
   for token in tokens:self.assertNotIn(token,text)
  self.assertIn('every 30 minutes; freshness is not guaranteed',text)
  source=(ROOT/'refresh.py').read_text().lower()
  for _,w,_,tokens in N.SOURCES:
   self.assertIn(w,source)
   for token in tokens:self.assertIn(token,source)
  coupon=[x for x in N.SOURCES if x[0].startswith('Coupon')];self.assertTrue(all(x[3]==[N.MOCA] for x in coupon))
 def test_missing_future_invalid_proofs_have_no_overall_cutoff(self):
  for change in ({'hash':'bad'},{'timestamp':'2026-10-10T02:00:00Z'},{'timestamp':'2026-99-10T00:00:00Z'},{'through':True},{'through':9}):
   with self.subTest(change=change):
    original=copy.deepcopy(self.legs[0]);self.legs[0].update(change);text=self.render();self.assertIn('Treasury OUT: unavailable',text);self.assertNotIn('All required sources',text);self.legs[0].clear();self.legs[0].update(original)
 def test_prefix_even_forged_complete_never_claims_window_coverage(self):
  for leg in self.legs:leg.update(complete=True,target=20)
  self.legs[0]['prefix']={'start':1,'through':4,'target':9}
  for d in self.docs:d['scope']['complete']=True;d['scope']['source_coverage']['coverage_ok']=True
  text=self.render();self.assertIn('suffix proof; earlier gap',text);self.assertNotIn('All required sources',text)
 def test_no_event_rows_needed_for_verified_empty_range(self):
  self.assertIn('Treasury OUT: 10 Oct',self.render())
 def test_retained_manifest_fallback_validated_readonly(self):
  d=copy.deepcopy(self.legs[0]);sid=N.scan_id(d['descriptor']);store=self.root/'pending_scans';store.mkdir();(store/(sid+'.json')).write_text(json.dumps(d));self.legs[0].clear();self.legs[0]['descriptor']=d['descriptor']
  with patch.object(N,'validate_store') as check:
   text=self.render();check.assert_called_once_with(str(store));self.assertIn('Treasury OUT: 10 Oct',text)
  with patch.object(N,'validate_store',side_effect=ValueError('bad store')):self.assertIn('Treasury OUT: unavailable',self.render())
 def test_real_manifest_checksum_validation_and_corruption(self):
  import hashlib
  d=copy.deepcopy(self.legs[0]);sid=N.scan_id(d['descriptor']);store=self.root/'pending_scans';(store/sid).mkdir(parents=True);raw=b'[]';chunk=store/sid/'10-20.json';chunk.write_bytes(raw)
  d.update(chunk=11,chunks=[{'file':'10-20.json','checksum':'0x'+hashlib.sha256(raw).hexdigest()}]);(store/(sid+'.json')).write_text(json.dumps(d));self.legs[0].clear();self.legs[0]['descriptor']=d['descriptor']
  self.assertIn('Treasury OUT: 10 Oct',self.render());chunk.write_bytes(b'[{}]');self.assertIn('Treasury OUT: unavailable',self.render())
 def test_complete_overall_requires_all_seven_and_both_aggregate_flags(self):
  for leg in self.legs:leg.update(complete=True,target=20)
  for d in self.docs:d['scope'].update(complete=True);d['scope']['source_coverage']['coverage_ok']=True
  self.assertIn('All required sources verified through',self.render())
  self.docs[1]['scope']['source_coverage']['coverage_ok']=False;self.assertNotIn('All required sources verified through',self.render())
 def test_unknown_and_conflicting_descriptor_no_raw_output(self):
  self.docs[1]['scope']['source_coverage']['pending']=copy.deepcopy(self.legs);self.docs[1]['scope']['source_coverage']['pending'][0]['timestamp']='2026-10-09T22:00:00Z';self.docs[0]['scope']['source_coverage']['pending'].append({'descriptor':{'owner':'private sentinel'}})
  text=self.render();self.assertIn('Treasury OUT: 10 Oct',text);self.assertNotIn('private sentinel',text)
 def test_actual_workflow_shell_selects_only_postcommit_coverage_copy(self):
  import subprocess,re
  source=(ROOT/'.github/workflows/refresh.yml').read_text();body=source[source.index('          TIER="BLOCK"'):source.index('      # Explicit save')];body='\n'.join(line[10:] for line in body.splitlines())
  for gate,commit,expected in [('coverage','success','Dashboard coverage behind'),('refresh','skipped','refresh.py (crawl/build) failed'),('coverage','failure','commit and push failed')]:
   with self.subTest(gate=gate,commit=commit):
    def replace(match):
     key=match[1].strip()
     if key=='steps.commit.outcome':return commit
     if key.startswith('steps.') and key.endswith('.outcome'):return 'failure' if key=='steps.'+gate+'.outcome' else 'success'
     return 'placeholder'
    shell=re.sub(r'\$\{\{(.*?)\}\}',replace,body)
    prefix='python3() { printf "SAFEDETAIL"; }; curl() { printf "%s" "$NOTICE"; };\n'
    result=subprocess.run(['bash','-e','-c',prefix+shell],capture_output=True,text=True,check=True).stdout
    self.assertIn(expected,result)
    if expected!='Dashboard coverage behind':self.assertNotIn('SAFEDETAIL',result)
 def test_bad_store_never_bypasses_with_valid_published_scalar(self):
  with patch.object(N,'validate_store',side_effect=ValueError('bad store')):
   text=self.render();self.assertEqual(text.count('checkpoint validation failed'),7);self.assertNotIn('All required sources',text)
 def test_workflow_only_enriches_published_coverage_original_send_retained(self):
  s=(ROOT/'.github/workflows/refresh.yml').read_text();self.assertIn('[ "${{ steps.commit.outcome }}" = "success" ]',s);self.assertEqual(s.count('tools/coverage_notice.py'),1);self.assertIn('TIER="PAGE"; DETAIL="Last saved finance',s);self.assertIn('if: failure()',s);self.assertIn('curl -s -o /dev/null "https://api.telegram.org',s)
if __name__=='__main__':unittest.main()

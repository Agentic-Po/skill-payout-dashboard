"""Execution regressions: missing chain coverage is unknown, never a zero day."""
import copy,json,unittest
from datetime import datetime,timezone
from pathlib import Path
import pagehost as P
ROOT=Path(__file__).resolve().parents[1]
PROBE="""(()=>{const e=globalThis.__domshim.elements;return {tiles:e.get('ftiles').innerHTML,note:e.get('dailyCoverage').textContent,hidden:e.get('dailyCoverage').hidden,daily:e.get('dailyT').innerHTML};})()"""
class Freshness(unittest.TestCase):
 def render(self,own,overall=False):
  d=json.loads((ROOT/'data.json').read_text());original=copy.deepcopy(d)
  d['scope']['complete']=overall;d['scope']['source_coverage']['treasury_complete']=own
  d['scope']['generated_iso']='2027-02-03T01:02:03Z'
  d['facts']['range']['to']='2027-02-01T12:34:56'
  d['facts']['daily']=[dict(d['facts']['daily'][-1],d='2027-02-01')]
  w=d['facts']['windows'][0];w['out_usd']=0;w['in_usd']=0
  for g in w['groups'].values():g['usd']=0
  before=copy.deepcopy(d)
  page=P.build_from_template((ROOT/'template.html').read_text())
  r=P.run(page,json.dumps(d),[{'now':int(datetime(2027,2,3,12,tzinfo=timezone.utc).timestamp()*1000),'probe':PROBE}])[0]
  self.assertFalse(r['uncaught']);self.assertFalse(r['renderErrors']);self.assertEqual(d,before)
  return r['probe'],original
 def test_retained_clock_and_unknown_zero_windows(self):
  p,_=self.render(False)
  self.assertIn('saved as of 2027-02-03 01:02:03 UTC',p['tiles'])
  self.assertEqual(p['tiles'].count('<div class="v">Unavailable</div>'),2)
  self.assertNotIn('right now',p['tiles']);self.assertNotIn('vs prev 24h',p['tiles'])
  self.assertFalse(p['hidden']);self.assertIn('2027-02-02 through 2027-02-03 UTC: coverage not verified',p['note'])
  self.assertIn('2027-02-01 12:34:56 UTC',p['note']);self.assertIn('unknown, not zero',p['note'])
  self.assertNotIn('<td class="mono">2027-02-02',p['daily'])
 def test_own_complete_with_stale_subsidiary_keeps_true_zero(self):
  p,_=self.render(True,False)
  self.assertNotIn('<div class="v">Unavailable</div>',p['tiles'])
  self.assertIn('<div class="v">$0</div>',p['tiles']);self.assertTrue(p['hidden'])
 def test_all_complete_keeps_figures(self):
  p,_=self.render(True,True)
  self.assertIn('<div class="v">$0</div>',p['tiles']);self.assertTrue(p['hidden'])
if __name__=='__main__':unittest.main()

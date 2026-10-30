#!/usr/bin/env python3
"""Regression: consumption market value must never use payout/live fallback."""
import ast
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from market_valuation import market_price, value_consumption

NOW=datetime(2026,10,30,1,tzinfo=timezone.utc)
class MarketValuationTests(unittest.TestCase):
    def test_historical_gap_does_not_carry_or_borrow_today(self):
        prices={'2026-10-01':.02,'2026-10-29':.01}
        self.assertEqual(market_price('2026-10-28',prices,NOW),(None,None,False))
        self.assertEqual(market_price('2026-10-30',prices,NOW),(Decimal('.01'),'2026-10-29',True))
        self.assertEqual(market_price('2026-10-31',prices,NOW),(None,None,False))
    def test_exact_old_observation_retains_original_value(self):
        self.assertEqual(market_price('2026-04-24',{'2026-04-24':.04,'2026-10-29':.01},NOW),(Decimal('.04'),'2026-04-24',False))
    def test_today_cannot_borrow_two_days_ago(self):
        self.assertEqual(market_price('2026-10-30',{'2026-10-28':.01},NOW),(None,None,False))
    def test_missing_price_preserves_native_amount_and_partial_subtotal(self):
        value=value_consumption([{'ts':'2026-10-28T01:00','val':50},{'ts':'2026-10-29T01:00','val':10},{'ts':'2026-10-30T01:00','val':20}],{'2026-10-29':.02},NOW)
        self.assertIsNone(value['usd']);self.assertEqual(value['priced_usd_subtotal'],.60)
        self.assertEqual(value['unpriced_rows'],1);self.assertEqual(value['unpriced_mente'],'50')
        self.assertEqual(value['provisional_rows'],1)
    def test_partial_today_and_future_are_never_closed_daily_prices(self):
        prices={'2026-10-29':.01,'2026-10-30':99,'2026-10-31':999}
        self.assertEqual(market_price('2026-10-30',prices,NOW),(Decimal('.01'),'2026-10-29',True))
        self.assertEqual(market_price('2026-10-31',prices,NOW),(None,None,False))
    def test_empty_and_zero_consumption_are_zero(self):
        self.assertEqual(value_consumption([],{},NOW)['usd'],0)
        self.assertEqual(value_consumption([{'ts':'2026-10-28','val':0}],{},NOW)['usd'],0)
    def test_invalid_positive_rate_cannot_create_value(self):
        for bad in (0,-1,'NaN','Infinity'):
            with self.assertRaises(ValueError):market_price('2026-10-29',{'2026-10-29':bad},NOW)
    def test_delayed_financial_cutoff_does_not_create_historical_estimate(self):
        tree=ast.parse((ROOT/'refresh.py').read_text())
        statement=next(n for n in ast.walk(tree) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='_cog_valuation_now' for t in n.targets))
        class Clock:
            @staticmethod
            def now(zone):return NOW
        pinned=datetime(2026,10,28,1,tzinfo=timezone.utc)
        for offline,expected in ((False,NOW),(True,pinned)):
            scope={'OFFLINE':offline,'_PINNED_NOW':pinned,'_now':pinned,'datetime':Clock,'timezone':timezone}
            exec(compile(ast.Module(body=[statement],type_ignores=[]),'valuation-clock','exec'),scope)
            self.assertEqual(scope['_cog_valuation_now'],expected)
        self.assertEqual(market_price('2026-10-28',{'2026-10-27':.02},NOW),(None,None,False))
    def test_unpriced_consumption_keeps_native_values_visible(self):
        import json
        import pagehost
        data=json.loads((ROOT/'data.json').read_text())
        data['facts']['cognition']={'total_n':2,'total_mente':60,'total_usd':None,'usd_7d':None,
                                  'minds_7d':1,'n_24h':2,'crawl_complete':True,'range_from':'2026-04-24',
                                  'minds_all':1,'daily':[],
                                  'valuation':{'all':{'unpriced_rows':1,'provisional_rows':0}}}
        page=pagehost.build_from_template((ROOT/'template.html').read_text())
        result=pagehost.run(page,json.dumps(data),[{'probe':"({tiles:document.getElementById('cogTiles').innerHTML,flow:document.getElementById('nCons').textContent})"}])[0]
        self.assertEqual(result['uncaught'],[])
        self.assertNotIn('Cognition consumption',str(result['renderErrors']))
        self.assertIn('USD unavailable',result['probe']['tiles'])
        self.assertIn('60 MENTE',result['probe']['tiles'])
        self.assertEqual(result['probe']['flow'],' · USD unavailable')
    def test_main_consumption_calls_market_evidence_only(self):
        source=(ROOT/'refresh.py').read_text();tree=ast.parse(source)
        calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='value_consumption']
        self.assertEqual(len(calls),2)
        self.assertTrue(all(isinstance(n.args[1],ast.Name) and n.args[1].id=='_cog_prices' for n in calls))
        self.assertIn('STATE.get("market_rates", {}).get("MENTE", {})',source)
        self.assertNotIn('def _cog_usd(',source)
if __name__=='__main__':unittest.main()

"""Price-only evidence from existing collector calls; never chooses a rate.

Query windows describe logical get() calls, including its existing retries.
Provider JSON number lexemes are decoded separately from the legacy collector.
No bodies, exception strings, headers, labels or balance data are exported.
"""
import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

COLLECTOR = 'treasury-price-acquisition-v1'
PRICE = re.compile(r'^[+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$')
ADDRESS = re.compile(r'^0x[0-9a-fA-F]{40}$')

def bounded_text(value,limit=80):
    return value if isinstance(value,str) and len(value)<=limit else None

def address(value):
    return value if isinstance(value,str) and ADDRESS.fullmatch(value) else None

def clock():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')

def price_text(value):
    text = value if isinstance(value,str) else str(value) if isinstance(value,(int,float)) and not isinstance(value,bool) else ''
    try:
        if len(text)<=256 and PRICE.fullmatch(text) and Decimal(text).is_finite() and Decimal(text)>0:return text
    except InvalidOperation:pass
    return None

def same_price(a,b):
    x,y=price_text(a),price_text(b)
    return x is not None and y is not None and Decimal(x)==Decimal(y)

def identity(row):
    raw=json.dumps(row,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    return 'treasury-price-'+hashlib.sha256(raw).hexdigest()

class Attempt:
    def __init__(self,owner,symbol,source,queried=True):
        self.owner=owner;self.raw=None
        self.row={'source':source,'symbol':symbol,'token_address':owner.tokens[symbol]['addr'],
          'price_usd':None,'query_started_at':owner.now() if queried else None,
          'query_completed_at':None,'fetched_at':None,'provider_updated_at':None,
          'last_trade_at':None,'outcome':'failed' if queried else 'not_queried',
          'reason_codes':['provider_unavailable_or_invalid'] if queried else ['higher_priority_source_accepted'],
          'accepted_by_collector':False,'pair':None}
    def received(self,body):
        # Called at receipt, before the collector's normal JSON decode. A bad
        # observation must not alter its pre-existing result or exception path.
        self.row['fetched_at']=self.owner.now()
        try:
            self.raw=json.loads(body,parse_float=str,parse_int=str)
            if self.row['source']=='blockscout':self.row['price_usd']=price_text(self.raw.get('exchange_rate'))
        except (ValueError,TypeError,AttributeError):self.raw=None
    def select_pair(self,index):
        try:
            pair=self.raw['pairs'][index]
            self.row['price_usd']=price_text(pair.get('priceUsd'))
            self.row['pair']={'chain_id':bounded_text(pair.get('chainId')),'address':address(pair.get('pairAddress')),
              'dex_id':bounded_text(pair.get('dexId')),'base_token_address':address(pair.get('baseToken',{}).get('address')),
              'quote_token_address':address(pair.get('quoteToken',{}).get('address')),
              'liquidity_usd':price_text(pair.get('liquidity',{}).get('usd'))}
        except (KeyError,IndexError,TypeError,AttributeError):pass
    def finish(self,outcome,reason=None):
        self.row['query_completed_at']=self.owner.now()
        self.row['outcome']=outcome
        self.row['accepted_by_collector']=outcome=='acquired'
        self.row['reason_codes']=[reason] if reason else []
    def record(self):
        row=copy.deepcopy(self.row)
        return {'observation_id':identity(row),**row}

class Capture:
    def __init__(self,tokens,previous=None,now=clock):
        self.tokens=tokens;self.previous=copy.deepcopy(previous or {});self.now=now;self.attempts=[]
    def begin(self,symbol,source):
        value=Attempt(self,symbol,source);self.attempts.append(value);return value
    def not_queried(self,symbol,source):
        self.attempts.append(Attempt(self,symbol,source,False))
    def envelope(self,rates,sources):
        observations=[a.record() for a in self.attempts];selections=[]
        for symbol in self.tokens:
            source=sources[symbol];rate=price_text(rates[symbol])
            acquired=next((r for r in observations if r['symbol']==symbol and r['source']==source and r['accepted_by_collector']),None)
            prior=next((s for s in self.previous.get('selections',[]) if s.get('symbol')==symbol and same_price(s.get('price_usd'),rate)),None)
            kind='acquired' if acquired else 'static' if source=='fallback' else 'retained'
            oid=acquired['observation_id'] if acquired else prior.get('original_observation_id') if prior and kind=='retained' else None
            observed=acquired['fetched_at'] if acquired else prior.get('original_observed_at') if prior and kind=='retained' else None
            if oid and not acquired:
                old=next((r for r in self.previous.get('observations',[]) if r.get('observation_id')==oid),None)
                if old and not any(r['observation_id']==oid for r in observations):observations.append(copy.deepcopy(old))
                if old is None:oid=observed=None
            selections.append({'symbol':symbol,'token_address':self.tokens[symbol]['addr'],'price_usd':rate,
              'source':source,'kind':kind,'original_observation_id':oid,'original_observed_at':observed})
        result={'schema_version':1,'collector':COLLECTOR,'origin':'provider_acquisition','chain_id':8453,
          'exported_at':self.now(),'observations':observations,'selections':selections}
        if len(json.dumps(result,separators=(',',':')).encode())>65536:raise ValueError('price_observations_exceeds_64k')
        return result

def offline_envelope(previous,tokens):
    """No acquisition or refreshed clocks on an offline build/legacy bootstrap."""
    prior=previous.get('scope',{}).get('price_observations')
    if prior is not None:return copy.deepcopy(prior)
    c=Capture(tokens,now=lambda:previous['scope'].get('build_generated_iso') or previous['scope']['generated_iso'])
    return c.envelope(previous['facts']['rate'],previous['facts']['rate_src'])

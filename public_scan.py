"""Bounded public ERC20 scan staging. Pending rows never enter financial caches.

A checkpoint contains public chain facts only. Fsynced chunks precede the
atomic cursor manifest; interrupted promotion replays rows for caller dedup.
"""
import hashlib
import json
import os
import re
import time
from datetime import datetime,timezone

FINALITY = 30
TRANSFER_TOPIC = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'

class ScanIncomplete(RuntimeError):
    pass

class ScanInvalid(RuntimeError):
    pass

class BudgetExpired(RuntimeError):
    pass


def atomic_save(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = path + '.tmp'
    try:
        with open(temp, 'w') as out:
            json.dump(value, out, separators=(',', ':'))
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.remove(temp)


def descriptor(wallet, direction, tokens):
    if not re.fullmatch(r'0x[0-9a-fA-F]{40}',wallet) or not tokens or any(not re.fullmatch(r'0x[0-9a-fA-F]{40}',t) for t in tokens):
        raise ScanInvalid('invalid public contract or wallet')
    if direction not in ('from', 'to'):
        raise ScanInvalid('invalid scan direction')
    return {'wallet':wallet.lower(), 'direction':direction,
            'tokens':sorted(set(t.lower() for t in tokens)), 'finality':FINALITY,
            'version':1}


def scan_id(desc):
    return hashlib.sha256(json.dumps(desc, sort_keys=True).encode()).hexdigest()[:16]


def log_key(row):
    return row['transaction_hash'].lower(), row['log_index']


class PublicScanner:
    def __init__(self, directory, rpc, materialize, range_error, budget_seconds=180, clock=time.monotonic):
        self.directory = directory
        self.rpc = rpc
        self.materialize = materialize
        self.range_error = range_error
        self.clock = clock
        self.deadline = None
        self.budget_seconds = budget_seconds
        self.last = {}

    def expired(self):
        return self.deadline is not None and self.clock() >= self.deadline

    def block(self, number):
        if self.expired():
            raise BudgetExpired('public scan budget exhausted')
        value = self.rpc('eth_getBlockByNumber', [hex(number), False])
        if (not isinstance(value, dict) or not value.get('hash') or not value.get('timestamp')
                or int(value.get('number','-1'),16) != number):
            raise ScanInvalid('missing verified block anchor')
        return value

    def scan(self, wallet, direction, tokens, start, head, chunk=2000):
        desc = descriptor(wallet, direction, tokens)
        sid = scan_id(desc)
        path = os.path.join(self.directory, sid+'.json')
        target = head - FINALITY
        if self.deadline is None:
            self.deadline = self.clock() + self.budget_seconds
        state = {'descriptor':desc, 'start':start, 'through':start-1,
                 'hash':None, 'timestamp':None, 'target':target, 'complete':False, 'chunks':[], 'rows':[]}
        if os.path.exists(path):
            validate_store(self.directory)
            with open(path) as prior:
                previous = json.load(prior)
            if previous.get('descriptor') != desc:
                raise ScanInvalid('scan descriptor mismatch')
            previous['rows'] = []
            for part in previous.get('chunks', []):
                if not re.fullmatch(r'\d+-\d+\.json', part['file']):
                    raise ScanInvalid('invalid pending chunk path')
                chunk_path = os.path.join(self.directory,sid,part['file'])
                with open(chunk_path,'rb') as data:
                    raw = data.read()
                if '0x'+hashlib.sha256(raw).hexdigest() != part['checksum']:
                    raise ScanInvalid('pending chunk checksum mismatch')
                previous['rows'].extend(json.loads(raw))
            through = previous['through']
            if through >= start and start >= previous['start']:
                if self.block(through)['hash'].lower() != previous['hash'].lower():
                    raise ScanInvalid('checkpoint block hash mismatch')
                # Rows remain until their complete leg has actually been promoted.
                state = previous
                chunk = min(chunk,previous.get('chunk',chunk))
                # Keep the immutable persisted origin; requested start only
                # filters the return value, never rewrites chunk coverage.
        state['target'] = target
        state['complete'] = False
        topic_wallet = '0x'+'0'*24+wallet[2:].lower()
        topics = [TRANSFER_TOPIC, topic_wallet] if direction == 'from' else [TRANSFER_TOPIC, None, topic_wallet]
        nxt = max(start, state['through']+1)
        if target < start:
            raise ScanIncomplete('requested start exceeds finalized head')
        seen = {log_key(r) for r in state['rows']}
        while nxt <= target:
            if self.expired():
                self.last[sid] = {k:v for k,v in state.items() if k not in ('rows','chunks')}
                raise ScanIncomplete('bounded scan incomplete; last good cache retained')
            end = min(nxt+chunk-1,target)
            try:
                logs = self.rpc('eth_getLogs',[{'fromBlock':hex(nxt),'toBlock':hex(end),'address':desc['tokens'],'topics':topics}])
            except self.range_error:
                if chunk == 1:
                    raise
                chunk = max(1,chunk//2)
                continue
            if not isinstance(logs,list):
                raise ScanInvalid('invalid log result')
            for log in logs:
                if not isinstance(log,dict):
                    raise ScanInvalid('invalid log object')
                ls=log.get('topics')
                if (not isinstance(ls,list) or len(ls)!=3
                        or any(not isinstance(topic,str) or not re.fullmatch(r'0x[0-9a-fA-F]{64}',topic) for topic in ls)
                        or not isinstance(log.get('address'),str)
                        or not re.fullmatch(r'0x[0-9a-fA-F]{40}',log['address'])
                        or not isinstance(log.get('transactionHash'),str)
                        or not re.fullmatch(r'0x[0-9a-fA-F]{64}',log['transactionHash'])
                        or any(not isinstance(log.get(field),str) or not re.fullmatch(r'0x[0-9a-fA-F]+',log[field]) for field in ('blockNumber','logIndex','data'))
                        or ('removed' in log and not isinstance(log['removed'],bool))):
                    raise ScanInvalid('invalid Transfer log shape')
                index=1 if direction=='from' else 2
                if (len(ls)!=3 or ls[0].lower()!=TRANSFER_TOPIC or ls[index].lower()!=topic_wallet
                        or log.get('removed') or log.get('address','').lower() not in desc['tokens']
                        or not nxt <= int(log['blockNumber'],16) <= end):
                    raise ScanInvalid('log does not match verified range and filter')
            # Materialize before checkpoint: a missing timestamp cannot become
            # a skipped transfer. The complete anchor is fetched after shaping.
            try:
                shaped = self.materialize(logs)
                if len(shaped) != len(logs):
                    raise ScanInvalid('materialization lost a transfer')
                anchor = self.block(end)
            except BudgetExpired:
                self.last[sid] = {k:v for k,v in state.items() if k not in ('rows','chunks')}
                raise ScanIncomplete('bounded scan incomplete; last good cache retained') from None
            expected = {(log['transactionHash'].lower(),int(log['logIndex'],16),int(log['blockNumber'],16)) for log in logs}
            actual = {(row['transaction_hash'].lower(),row['log_index'],row['block_number']) for row in shaped}
            if actual != expected:
                raise ScanInvalid('materialization changed transfer identity')
            for row in shaped:
                key = log_key(row)
                if key not in seen:
                    state['rows'].append(row)
                    seen.add(key)
            state.update(through=end,hash=anchor['hash'],timestamp=datetime.fromtimestamp(int(anchor['timestamp'],16),timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),complete=end==target,chunk=chunk)
            # Each range is small; no ever-growing monolithic pending file.
            # Persist the chunk before its manifest so an interrupted write
            # leaves at worst an unreferenced chunk, never a cursor ahead.
            filename = f'{nxt}-{end}.json'
            chunk_path = os.path.join(self.directory,sid,filename)
            atomic_save(chunk_path,shaped)
            with open(chunk_path,'rb') as raw:
                checksum = '0x'+hashlib.sha256(raw.read()).hexdigest()
            state['chunks'].append({'file':filename,'checksum':checksum})
            atomic_save(path,{k:v for k,v in state.items() if k!='rows'})
            nxt = end+1
        state['complete'] = True
        self.last[sid] = {k:v for k,v in state.items() if k not in ('rows','chunks')}
        return [r for r in state['rows'] if start <= r['block_number'] <= target]


def validate_store(directory):
    """Reject private/malformed pending documents before public staging."""
    if not os.path.isdir(directory):
        return
    manifests={}
    for filename in os.listdir(directory):
        path=os.path.join(directory,filename)
        if os.path.isdir(path):
            if not re.fullmatch(r'[0-9a-f]{16}',filename):
                raise ScanInvalid('invalid pending scan directory')
            continue
        if filename.endswith('.tmp'):
            continue
        if not re.fullmatch(r'[0-9a-f]{16}\.json',filename):
            raise ScanInvalid('invalid pending manifest path')
        with open(path) as source:
            state=json.load(source)
        required={'descriptor','start','through','hash','timestamp','target','complete','chunks','chunk'}
        if set(state)!=required or set(state['descriptor'])!={'wallet','direction','tokens','finality','version'}:
            raise ScanInvalid('invalid pending manifest schema')
        desc=state['descriptor']
        if descriptor(desc['wallet'],desc['direction'],desc['tokens'])!=desc or scan_id(desc)!=filename[:-5]:
            raise ScanInvalid('invalid pending scan identity')
        if (not all(isinstance(state[k],int) and not isinstance(state[k],bool) for k in ('start','through','target','chunk'))
                or state['through']<state['start'] or not isinstance(state['complete'],bool)
                or (state['complete'] and state['through']<state['target'])
                or not re.fullmatch(r'0x[0-9a-fA-F]{64}',state['hash'])
                or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ',state['timestamp'])):
            raise ScanInvalid('invalid pending coverage proof')
        prior=None
        for part in state['chunks']:
            if set(part)!={'file','checksum'} or not re.fullmatch(r'\d+-\d+\.json',part['file']):
                raise ScanInvalid('invalid pending chunk reference')
            lo,hi=map(int,part['file'][:-5].split('-'))
            if hi<lo or (prior is None and lo!=state['start']) or (prior is not None and lo!=prior+1):
                raise ScanInvalid('pending coverage gap')
            prior=hi
            chunk_path=os.path.join(directory,filename[:-5],part['file'])
            with open(chunk_path,'rb') as data:
                raw=data.read()
            if '0x'+hashlib.sha256(raw).hexdigest()!=part['checksum']:
                raise ScanInvalid('pending chunk checksum mismatch')
        if prior!=state['through']:
            raise ScanInvalid('pending cursor exceeds persisted chunks')
        manifests[filename[:-5]]=desc
    for sid in (name for name in os.listdir(directory) if os.path.isdir(os.path.join(directory,name))):
        for filename in os.listdir(os.path.join(directory,sid)):
            if filename.endswith('.tmp'):
                continue
            if not re.fullmatch(r'\d+-\d+\.json',filename):
                raise ScanInvalid('invalid pending chunk path')
            lo,hi=map(int,filename[:-5].split('-'))
            with open(os.path.join(directory,sid,filename)) as data:
                rows=json.load(data)
            if not isinstance(rows,list):
                raise ScanInvalid('invalid pending rows')
            for row in rows:
                fields={'timestamp','transaction_hash','log_index','block_number','from','to','token','total'}
                if (set(row)!=fields or set(row['from'])!={'hash'} or set(row['to'])!={'hash'}
                        or set(row['token'])!={'address_hash'} or set(row['total'])!={'value','decimals'}
                        or not re.fullmatch(r'0x[0-9a-fA-F]{64}',row['transaction_hash'])
                        or not re.fullmatch(r'\d+',row['total']['value'])
                        or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{6})?Z',row['timestamp'])
                        or row['total']['decimals'] not in (None,18)
                        or not isinstance(row['log_index'],int) or row['log_index']<0
                        or not isinstance(row['block_number'],int) or not lo<=row['block_number']<=hi
                        or any(not re.fullmatch(r'0x[0-9a-fA-F]{40}',a) for a in
                               (row['from']['hash'],row['to']['hash'],row['token']['address_hash']))):
                    raise ScanInvalid('invalid public transfer row schema')
                if sid in manifests:
                    desc=manifests[sid]
                    if row[desc['direction']]['hash'].lower()!=desc['wallet'] or row['token']['address_hash'].lower() not in desc['tokens']:
                        raise ScanInvalid('pending row filter mismatch')


def require_coverage(*documents):
    """Post-publication health gate: cached finance cannot claim current health."""
    for document in documents:
        scope = document.get('scope', {})
        coverage = scope.get('source_coverage')
        if (scope.get('complete') is not True or not isinstance(coverage, dict)
                or coverage.get('coverage_ok') is not True
                or not isinstance(coverage.get('pending'), list)):
            raise ScanIncomplete('chain coverage incomplete; last saved finance retained')
        for leg in coverage['pending']:
            if (not isinstance(leg, dict) or leg.get('complete') is not True
                    or not isinstance(leg.get('through'), int)
                    or not isinstance(leg.get('target'), int)
                    or leg['through'] < leg['target']
                    or not isinstance(leg.get('hash'),str)
                    or not re.fullmatch(r'0x[0-9a-fA-F]{64}',leg['hash'])):
                raise ScanIncomplete('chain coverage incomplete; verified pending scan retained')

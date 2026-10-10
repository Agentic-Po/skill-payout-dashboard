"""Public RPC range refusal remains actionable through provider outages."""
import ast
import io
import json
import os
import types
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0,ROOT)
SOURCE = open(os.path.join(ROOT, 'refresh.py')).read()
TREE = ast.parse(SOURCE)
NAMES = {'RpcRangeError', '_rpc_range_error', 'rpc', 'rpc_transfer_fallback','_public_leg'}
CODE = '\n'.join(ast.get_source_segment(SOURCE,n) for n in TREE.body if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name in NAMES)

class RpcFallback(unittest.TestCase):
    def ns(self, responses):
        def call(req, **kw):
            result=responses[req.full_url]
            if callable(result):result=result()
            if isinstance(result,Exception):raise result
            return io.BytesIO(json.dumps(result).encode())
        ns={'json':json,'urllib':types.SimpleNamespace(request=types.SimpleNamespace(Request=urllib.request.Request,urlopen=call),error=urllib.error),
            'time':types.SimpleNamespace(sleep=lambda *_:None,time=lambda:0),'RPC_ENDPOINTS':list(responses),'_RPC_CALLS':[0],
            'TRANSFER_TOPIC':'topic','LOGS_CHUNK':[2000],'XCHECK_WARN_S':60,'block_ts_prefetch':lambda _:None,'block_ts':lambda _:0}
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        ns.update({'_PUBLIC_SCANNER':None,'_RPC_LAST_THROUGH':None,'_shape_rpc_transfers':lambda logs:[],'HERE':temp.name,'os':os,'_network_timeout':lambda cap:cap,'_network_sleep':lambda _:None})
        exec(CODE,ns)
        return ns

    def test_http400_range_survives_later403(self):
        def range_error():return urllib.error.HTTPError('https://range.invalid',400,'range',{},io.BytesIO(b'{"error":{"message":"ranges over 10000 blocks unsupported"}}'))
        ns=self.ns({'https://range.invalid':range_error,'https://down.invalid':lambda:urllib.error.HTTPError('https://down.invalid',403,'blocked',{},io.BytesIO(b'blocked'))})
        with self.assertRaises(ns['RpcRangeError']):ns['rpc']('eth_getLogs',[],tries=1)

    def test_working_provider_wins_over_range_refusal(self):
        ns=self.ns({'https://range.invalid':{'error':{'message':'limited to 50 blocks range'}},'https://ok.invalid':{'result':[]}})
        self.assertEqual(ns['rpc']('eth_getLogs',[]),[])

    def test_rate_limit_is_not_range_refusal(self):
        ns=self.ns({'https://down.invalid':{'error':{'message':'rate limit too many requests'}}})
        with self.assertRaises(RuntimeError) as c:ns['rpc']('eth_getLogs',[],tries=1)
        self.assertNotIsInstance(c.exception,ns['RpcRangeError'])

    def test_null_is_not_empty_success(self):
        ns=self.ns({'https://down.invalid':{'result':None}})
        with self.assertRaises(RuntimeError):ns['rpc']('eth_getLogs',[],tries=1)

    def test_fallback_reaches_50_block_cap_and_covers_entire_range(self):
        ns=self.ns({});calls=[]
        def rpc(method,params):
            if method=='eth_blockNumber':return hex(160)
            if method=='eth_getBlockByNumber':return {'number':params[0],'hash':'0x'+'1'*64,'timestamp':'0x1'}
            query=params[0];start=int(query['fromBlock'],16);end=int(query['toBlock'],16);calls.append((start,end))
            if end-start+1>50:raise ns['RpcRangeError']('range')
            return []
        ns['rpc']=rpc
        self.assertEqual(ns['rpc_transfer_fallback']('0x'+'1'*40,'from',['0x'+'2'*40],10,130),[])
        success=[x for x in calls if x[1]-x[0]+1<=50]
        self.assertEqual(success[0][0],10);self.assertEqual(success[-1][1],130)
        for prev,nxt in zip(success,success[1:]):self.assertEqual(prev[1]+1,nxt[0])

    def test_transport_failure_does_not_shrink_or_advance(self):
        ns=self.ns({});calls=[]
        def rpc(*args):calls.append(args);raise RuntimeError('outage')
        ns['rpc']=rpc
        with self.assertRaises(RuntimeError):ns['rpc_transfer_fallback']('0x'+'1'*40,'from',['0x'+'2'*40],10,130)
        self.assertEqual(len(calls),1);self.assertEqual(ns['LOGS_CHUNK'],[2000])

    def test_minimum_range_terminates(self):
        ns=self.ns({});ns['LOGS_CHUNK']=[1]
        def rpc(method,params):
            if method=='eth_blockNumber':return hex(160)
            raise ns['RpcRangeError']('range')
        ns['rpc']=rpc
        with self.assertRaises(ns['RpcRangeError']):ns['rpc_transfer_fallback']('0x'+'1'*40,'from',['0x'+'2'*40],10,10)

if __name__=='__main__':unittest.main()

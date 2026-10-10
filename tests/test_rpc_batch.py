#!/usr/bin/env python3
"""rpc_batch skips are counted and logged, never silent — and never change
control flow (loop 3).

refresh.py's rpc_batch and _rpc_batch_skip are extracted by AST and run
against a fake urlopen that walks the three endpoints:
  1. raises (connection error)          -> skip [error], next endpoint
  2. answers a dict, not a batch list   -> skip [not-a-batch], next endpoint
  3. answers a list with one item that
     errored                            -> skip [items-dropped], returns the rest
The return value must be exactly what the pre-loop-3 code returned (ids that
errored omitted), the log lines must name reason + host + call, and the
counter must hold every skip. A run where every endpoint fails still returns
{}. The PHASE TIMING line must carry the total.
"""
import ast, io, json, os, re, sys
from collections import Counter
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(ROOT, "refresh.py")).read()
tree = ast.parse(src)
fns = {n.name: ast.get_source_segment(src, n) for n in tree.body
       if isinstance(n, ast.FunctionDef) and n.name in ("rpc_batch", "_rpc_batch_skip")}
assert set(fns) == {"rpc_batch", "_rpc_batch_skip"}, f"refresh.py lost {fns.keys()}"
fails = []


class FakeResp(io.StringIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_ns(behaviour):
    urls = ["https://mainnet.base.org", "https://base.drpc.org", "https://base.blockscout.com/api/eth-rpc"]

    class Req:
        def __init__(self, url, data=None, headers=None):
            self.full_url = url

    def urlopen(req, timeout=None):
        b = behaviour[urls.index(req.full_url)]
        if isinstance(b, Exception):
            raise b
        return FakeResp(json.dumps(b))

    fake = type("U", (), {"Request": Req, "urlopen": staticmethod(urlopen)})
    ns = {"json": json, "Counter": Counter, "RPC_ENDPOINTS": urls,
          "urllib": type("urllib", (), {"request": fake}),
          "_network_timeout":lambda cap:cap, "_RPC_BATCH_SKIPS": Counter(), "_RPC_BATCH_LOG_MAX": 5}
    exec(fns["_rpc_batch_skip"], ns)
    exec(fns["rpc_batch"], ns)
    return ns


calls = [("eth_getBlockByNumber", [hex(b), False]) for b in (1, 2, 3)]
# case 1: error -> not-a-batch -> partial list
ns = make_ns([ConnectionError("reset by peer"),
              {"jsonrpc": "2.0", "error": {"message": "maximum 10 calls in 1 batch"}},
              [{"id": 0, "result": {"timestamp": "0x1"}},
               {"id": 1, "error": {"message": "header not found"}},
               {"id": 2, "result": {"timestamp": "0x3"}}]])
buf = io.StringIO()
with redirect_stdout(buf):
    got = ns["rpc_batch"](calls)
log = buf.getvalue()
if got != {0: {"timestamp": "0x1"}, 2: {"timestamp": "0x3"}}:
    fails.append(f"return value changed: {got}")
if dict(ns["_RPC_BATCH_SKIPS"]) != {"error": 1, "not-a-batch": 1, "items-dropped": 1}:
    fails.append(f"skip counts wrong: {dict(ns['_RPC_BATCH_SKIPS'])}")
for pat in (r"rpc_batch skip \[error\] mainnet\.base\.org \(3x eth_getBlockByNumber\): ConnectionError: reset by peer",
            r"rpc_batch skip \[not-a-batch\] base\.drpc\.org \(3x eth_getBlockByNumber\): .*maximum 10 calls",
            r"rpc_batch skip \[items-dropped\] base\.blockscout\.com \(3x eth_getBlockByNumber\): 1 of 3"):
    if not re.search(pat, log):
        fails.append(f"missing log line /{pat}/ in:\n{log}")

# case 2: every endpoint fails -> {} and three counted skips
ns = make_ns([TimeoutError("timed out")] * 3)
with redirect_stdout(io.StringIO()):
    got = ns["rpc_batch"](calls)
if got != {} or ns["_RPC_BATCH_SKIPS"]["error"] != 3:
    fails.append(f"all-fail case: got {got}, counts {dict(ns['_RPC_BATCH_SKIPS'])}")

# case 3: a clean batch logs nothing and counts nothing
ns = make_ns([[{"id": i, "result": {"timestamp": "0x1"}} for i in range(3)], None, None])
buf = io.StringIO()
with redirect_stdout(buf):
    got = ns["rpc_batch"](calls)
if len(got) != 3 or ns["_RPC_BATCH_SKIPS"] or buf.getvalue():
    fails.append("clean batch must return all ids, log nothing, count nothing")

# log volume is capped, the count is not
ns = make_ns([ConnectionError("x")] * 3)
buf = io.StringIO()
with redirect_stdout(buf):
    for _ in range(4):
        ns["rpc_batch"](calls)
if buf.getvalue().count("rpc_batch skip") != 5 or sum(ns["_RPC_BATCH_SKIPS"].values()) != 12:
    fails.append(f"log cap: {buf.getvalue().count('rpc_batch skip')} lines, "
                 f"{sum(ns['_RPC_BATCH_SKIPS'].values())} counted (want 5 / 12)")

if not re.search(r'print\("PHASE TIMING: .*rpc_batch_skips=%d', src):
    fails.append("PHASE TIMING line no longer carries rpc_batch_skips")
if re.search(r"except Exception:\s*\n\s*continue", fns["rpc_batch"]):
    fails.append("rpc_batch has a silent `except Exception: continue` again")

for f in fails:
    print("FAIL", f)
if fails:
    print(f"test_rpc_batch: FAIL ({len(fails)})")
    sys.exit(1)
print("ok skips counted by reason and logged with host + call; return values unchanged; log capped at 5")
print("test_rpc_batch: PASS")

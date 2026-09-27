#!/usr/bin/env python3
"""util.py stays pure, and the helpers it absorbed stay single-copy.

  (a) util.py imports only `datetime`; references no I/O, network, process or
      state names (open, os, sys, json, urllib, socket, subprocess, requests,
      shards, state, STATE, alert_state, ...); its module body is only the
      docstring, imports, constant assignments and defs.
  (b) no module keeps its own def of hkt / _iso / _dt / _cooled that util now
      owns (fences.py, cap_detect.py). notify.hkt is the documented exception:
      it also accepts an ISO string, so it is a different function.
  (c) behaviour spot-checks of every helper.
"""
import ast, glob, os, sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import util  # noqa: E402

fails = []
src = open(os.path.join(ROOT, "util.py")).read()
tree = ast.parse(src)
DOCS = {id(n.body[0].value) for n in ast.walk(tree)
        if isinstance(n, (ast.Module, ast.FunctionDef)) and n.body and isinstance(n.body[0], ast.Expr)
        and isinstance(n.body[0].value, ast.Constant)}
FORBIDDEN = {"open", "os", "sys", "json", "urllib", "socket", "subprocess", "requests", "http",
             "shards", "state", "_state", "STATE", "alert_state", "RATES_PATH", "print", "input",
             "exec", "eval", "__import__", "time"}
for n in ast.walk(tree):
    if isinstance(n, ast.Import):
        mods = [a.name for a in n.names]
    elif isinstance(n, ast.ImportFrom):
        mods = [n.module]
    else:
        mods = None
    if mods is not None and any(m.split(".")[0] != "datetime" for m in mods):
        fails.append(f"util.py:{n.lineno} imports {mods} — only datetime is allowed")
    if isinstance(n, ast.Name) and n.id in FORBIDDEN:
        fails.append(f"util.py:{n.lineno} references {n.id!r}")
    if isinstance(n, ast.Attribute) and n.attr in {"open", "urlopen", "load", "dump", "write", "update"}:
        fails.append(f"util.py:{n.lineno} calls .{n.attr}")
    if isinstance(n, ast.Constant) and id(n) not in DOCS and isinstance(n.value, str) and ("alert_state" in n.value
                                                                     or "day_rates" in n.value):
        fails.append(f"util.py:{n.lineno} names a state file")
for i, n in enumerate(tree.body):
    ok = (isinstance(n, (ast.Import, ast.ImportFrom, ast.FunctionDef))
          or (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and i == 0)
          or isinstance(n, ast.Assign))
    if not ok:
        fails.append(f"util.py:{n.lineno} module-level {type(n).__name__} (side effect?)")

OWNED = {"hkt", "_iso", "_dt", "_cooled", "iso_min", "parse_dt", "cooled"}
EXEMPT = {("notify.py", "hkt")}     # accepts str too — a different function (see util.py docstring)
for path in sorted(glob.glob(os.path.join(ROOT, "*.py"))):
    name = os.path.basename(path)
    if name == "util.py":
        continue
    for n in ast.parse(open(path).read()).body:
        if isinstance(n, ast.FunctionDef) and n.name in OWNED and (name, n.name) not in EXEMPT:
            fails.append(f"{name}:{n.lineno} still defines {n.name} — use util")

import fences, cap_detect  # noqa: E402
for mod in (fences, cap_detect):
    for local, shared in (("hkt", util.hkt), ("_iso", util.iso_min), ("_dt", util.parse_dt),
                          ("_cooled", util.cooled)):
        if getattr(mod, local, None) is not shared:
            fails.append(f"{mod.__name__}.{local} is not util's")
if cap_detect.HKT != util.HKT:
    fails.append("cap_detect.HKT differs from util.HKT")

t = datetime(2026, 9, 27, 17, 5, 30)
checks = [
    (util.hkt(t), "28 Sep 01:05 HKT"),
    (util.hkt(t, "%H:%M"), "01:05 HKT"),
    (util.iso_min(t), "2026-09-27T17:05"),
    (util.parse_dt("2026-09-27T17:05"), datetime(2026, 9, 27, 17, 5)),
    (util.parse_dt(""), None),
    (util.parse_dt(None), None),
    (util.cooled(None, t, 6), True),
    (util.cooled("2026-09-27T11:05", t, 6), True),     # 6h00m30s >= 6h
    (util.cooled("2026-09-27T11:06", t, 6), False),    # 5h59m30s
]
for got, want in checks:
    if got != want:
        fails.append(f"behaviour: got {got!r}, want {want!r}")
try:
    util.hkt("2026-09-27T17:05")
    fails.append("util.hkt accepted a string — that is notify.hkt's behaviour, not the shared one")
except TypeError:
    pass

for f in fails:
    print("FAIL", f)
if fails:
    print(f"test_util: FAIL ({len(fails)})")
    sys.exit(1)
print("ok util.py pure (datetime only, no I/O/state names); ok no duplicate helper defs; ok behaviour")
print("test_util: PASS")

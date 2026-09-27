#!/usr/bin/env python3
"""STATE (day_rates.json) write order is load-bearing — pin it.

refresh.py persists STATE twice: once right after data.json (so a failure in
any later section cannot cost the run its newly computed closed-day rates) and
again at the end of the coupon verification crawl (so the coupon cursor and
verification stamp survive). Moving either write changes what a crash leaves
on disk. Both go through refresh.write_state(label).

  (a) static: the write_state() call sites, in source order, carry exactly the
      labels in tests/state_write_order.json["source_order"]; the first sits
      after the data.json dump; the coupon write sits inside `if not OFFLINE`;
      nothing else in refresh.py writes RATES_PATH / json.dump(STATE, ...).
  (b) dynamic: `refresh.py --offline` in a throwaway copy of the tree with
      REFRESH_TRACE_STATE set records every write (label, phases completed
      before it, STATE's top-level keys); the trace must equal
      tests/state_write_order.json["offline"]. The coupon write is online-only,
      so (a) is what pins it.

An INTENDED change to the write order regenerates the expectation with
    python3 tests/test_state_order.py --update
and says why in the PR.
"""
import ast, json, os, shutil, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPECT = os.path.join(ROOT, "tests", "state_write_order.json")


def static_order(src):
    tree = ast.parse(src)
    parents = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            parents[c] = n
    calls, problems = [], []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
            if name == "write_state":
                label = n.args[0].value if n.args and isinstance(n.args[0], ast.Constant) else None
                # enclosing `if not OFFLINE` guard?
                p, guarded = n, False
                while p in parents:
                    p = parents[p]
                    if isinstance(p, ast.If) and "OFFLINE" in ast.unparse(p.test) \
                            and ast.unparse(p.test).startswith("not "):
                        guarded = True
                calls.append((n.lineno, label, guarded))
            elif name == "dump" and n.args and isinstance(n.args[0], ast.Name) \
                    and n.args[0].id == "STATE":
                fn = next((p for p in _up(n, parents) if isinstance(p, ast.FunctionDef)), None)
                if not (fn and fn.name == "write_state"):
                    problems.append(f"line {n.lineno}: json.dump(STATE, ...) outside write_state()")
    calls.sort()
    return calls, problems


def _up(n, parents):
    while n in parents:
        n = parents[n]
        yield n


def trace_offline():
    tmp = tempfile.mkdtemp(prefix="state-order-")
    work = os.path.join(tmp, "w")
    shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    tr = os.path.join(tmp, "trace.jsonl")
    env = dict(os.environ, REFRESH_TRACE_STATE=tr)
    r = subprocess.run([sys.executable, "refresh.py", "--offline"], cwd=work, env=env,
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-1500:], r.stderr[-1500:])
        sys.exit("FAIL: refresh.py --offline failed in the throwaway copy")
    out = [json.loads(l) for l in open(tr)] if os.path.exists(tr) else []
    shutil.rmtree(tmp, ignore_errors=True)
    return out


def main():
    src = open(os.path.join(ROOT, "refresh.py")).read()
    calls, problems = static_order(src)
    labels = [c[1] for c in calls]
    trace = trace_offline()
    if "--update" in sys.argv:
        json.dump({"source_order": labels, "offline": trace}, open(EXPECT, "w"), indent=1)
        print(f"wrote {EXPECT}")
        return
    exp = json.load(open(EXPECT))
    fails = list(problems)
    if labels != exp["source_order"]:
        fails.append(f"write_state call sites in source order {labels} != expected {exp['source_order']}")
    i_data = src.find('json.dump(data, open(os.path.join(HERE, "data.json")')
    first = calls[0][0] if calls else 0
    if i_data < 0 or src[:i_data].count("\n") + 1 > first:
        fails.append("the first STATE write must come after the data.json dump")
    for ln, lab, guarded in calls:
        if lab == "coupon_verify" and not guarded:
            fails.append(f"line {ln}: coupon_verify write is no longer inside `if not OFFLINE`")
    if trace != exp["offline"]:
        fails.append("offline STATE write trace differs from tests/state_write_order.json:\n"
                     f"   got      {json.dumps(trace)[:600]}\n   expected {json.dumps(exp['offline'])[:600]}")
    for f in fails:
        print("FAIL", f)
    if fails:
        print(f"test_state_order: FAIL ({len(fails)})")
        sys.exit(1)
    print(f"ok static order {labels}; ok offline trace ({len(trace)} write(s))")
    print("test_state_order: PASS")


if __name__ == "__main__":
    main()

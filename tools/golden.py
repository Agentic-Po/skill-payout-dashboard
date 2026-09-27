#!/usr/bin/env python3
"""Golden-output guard: prove a code change does (or does not) move a figure.

    python3 tools/golden.py [BASE_REF]      # default BASE_REF = origin/main

Builds two throwaway copies of the CURRENT working tree (same shards, same
state files, same data.json), swaps the code in copy A back to BASE_REF, runs
`refresh.py --offline` in both, and diffs every published artifact after
normalising the only two wall-clock fields an offline rebuild still carries
(`data_age_hours`, catalog `generated_iso`).

`refresh.py --offline` is byte-deterministic run-to-run (measured 2026-09-27),
so any remaining difference is caused by the code change. JSON artifacts get a
key-path diff so an INTENDED change (a new field, a relabel) can be reviewed
line by line; everything else gets a unified-diff line count.

Exit 0 = byte-identical after normalisation; exit 1 = differences listed.
A refactor that claims to be behaviour-preserving must exit 0.
"""
import json, os, re, shutil, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARTIFACTS = ["exports/transfers-2026-09.csv", "exports/transfers-2026-08.csv", "data.json", "index.html", "legacy.html", "coupon.html",
             "coupon_data.json", "transfers_export.csv", "day_rates.json",
             "day_digests.json", "stats_history.json", "catalog.json", "DATASETS.md"]
# code = anything whose change is the thing under test; data stays identical
CODE = re.compile(r"(\.py|^template[^/]*\.html)$")
AGE = re.compile(r'("data_age_hours":\s*)-?[0-9.]+')
GEN = re.compile(r'("generated_iso":\s*)"[^"]*"')


def sh(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def copy_tree(dst):
    # the working tree as it is (incl. uncommitted edits), minus .git
    shutil.copytree(ROOT, dst, ignore=shutil.ignore_patterns(".git", "__pycache__"))


def norm(name, text):
    text = AGE.sub(r"\1 0", text)
    if name == "catalog.json":
        text = GEN.sub(r'\1"-"', text)
    if name == "DATASETS.md":
        text = re.sub(r"(generated|Generated)[^\n]*\d{4}-\d\d-\d\dT[^\n]*", r"\1 -", text)
    return text


def jdiff(a, b, path="", out=None, cap=60):
    out = [] if out is None else out
    if len(out) >= cap:
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"+ {path}.{k}")
            elif k not in b:
                out.append(f"- {path}.{k}")
            else:
                jdiff(a[k], b[k], f"{path}.{k}", out, cap)
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b)):
            jdiff(x, y, f"{path}[{i}]", out, cap)
    elif a != b:
        sa, sb = json.dumps(a)[:90], json.dumps(b)[:90]
        out.append(f"~ {path}: {sa} -> {sb}")
    return out


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    tmp = tempfile.mkdtemp(prefix="golden-")
    a, b = os.path.join(tmp, "base"), os.path.join(tmp, "head")
    copy_tree(a)
    copy_tree(b)
    # swap copy A's code back to BASE_REF (files that exist there)
    files = sh(["git", "ls-tree", "-r", "--name-only", base], ROOT).stdout.split()
    head_code = [f for f in sh(["git", "ls-files"], ROOT).stdout.split() if CODE.search(f)]
    for f in set(head_code) | {f for f in files if CODE.search(f)}:
        p = os.path.join(a, f)
        if f in files:
            blob = subprocess.run(["git", "show", f"{base}:{f}"], cwd=ROOT, capture_output=True).stdout
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "wb").write(blob)
        elif os.path.exists(p):
            os.remove(p)  # new on HEAD, absent on base
    for side in (a, b):
        r = sh([sys.executable, "refresh.py", "--offline"], side)
        if r.returncode != 0:
            print(f"FATAL: refresh.py --offline failed in {os.path.basename(side)}\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}")
            sys.exit(2)
    diffs = 0
    for name in ARTIFACTS:
        pa, pb = os.path.join(a, name), os.path.join(b, name)
        if not (os.path.exists(pa) and os.path.exists(pb)):
            print(f"?? {name}: present base={os.path.exists(pa)} head={os.path.exists(pb)}")
            diffs += 1
            continue
        ta, tb = norm(name, open(pa).read()), norm(name, open(pb).read())
        if ta == tb:
            print(f"ok {name}")
            continue
        diffs += 1
        if name.endswith(".json"):
            lines = jdiff(json.loads(ta), json.loads(tb))
            print(f"DIFF {name}: {len(lines)} key path(s)")
            for l in lines:
                print("   ", l)
        else:
            import difflib
            d = [l for l in difflib.unified_diff(ta.splitlines(), tb.splitlines(), lineterm="", n=0)
                 if l[:1] in "+-" and l[:3] not in ("+++", "---")]
            print(f"DIFF {name}: {len(d)} changed line(s); {len(ta):,} -> {len(tb):,} bytes")
            for l in d[:int(os.environ.get("GOLDEN_SHOW", "0"))]:
                print("   ", l[:220])
    print(f"golden: {'IDENTICAL' if not diffs else f'{diffs} artifact(s) differ'} vs {base} (scratch {tmp})")
    if not diffs:
        shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if diffs else 0)


if __name__ == "__main__":
    main()

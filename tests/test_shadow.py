#!/usr/bin/env python3
"""No module-level assignment may shadow an imported name.

Added 2026-08-31: `band = _market_band(...)` in refresh.py's oracle section
shadowed classify.band at module scope; it only fired on the first run after
UTC midnight when a newly closed day took a market rate — the hourly refresh
crashed with "'tuple' object is not callable" and no local test caught it.
"""
import ast
import glob
import sys

failures = 0
for path in sorted(glob.glob("*.py")):
    tree = ast.parse(open(path).read(), path)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imported.update(a.asname or a.name.split(".")[0] for a in node.names)
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign, ast.For)):
            targets = [node.target]
        for t in targets:
            for name in ast.walk(t):
                if isinstance(name, ast.Name) and name.id in imported:
                    print(f"FAIL {path}:{name.lineno}: assignment shadows "
                          f"imported name '{name.id}'")
                    failures += 1

if failures:
    print(f"test_shadow: FAIL ({failures} shadowed import(s))")
    sys.exit(1)
print("test_shadow: PASS")

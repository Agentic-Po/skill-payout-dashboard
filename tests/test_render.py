#!/usr/bin/env python3
"""Render smoke gate: every <script> in the built page must PARSE.

Added 2026-08-30 after a dangling brace from a template edit shipped a
parse-time SyntaxError that killed ALL client-side rendering for ~36h —
the page served, every JS-built table and chart (including the daily
size-band mix bar that caught the equip exploit) was blank, and no data
check noticed because the data was fine. Requires node on PATH (present
on GitHub runners); skips with a loud message if absent.
"""
import re
import shutil
import subprocess
import sys
import tempfile

if not shutil.which("node"):
    # In CI a missing node must FAIL — a skipped gate is a lying-green gate.
    import os
    if os.environ.get("CI"):
        print("FAIL: node not available in CI — render gate cannot run")
        sys.exit(1)
    print("SKIP: node not available — render gate not enforced this run")
    sys.exit(0)

failures = 0
for page in ("index.html", "legacy.html", "template.html", "template_legacy.html"):
    try:
        html = open(page).read()
    except FileNotFoundError:
        continue
    for i, script in enumerate(re.findall(r"<script>(.*?)</script>", html, re.S)):
        src = script.replace("/*__DATA__*/", "{}")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(src)
            path = fh.name
    # (checked outside the write context so the file is flushed)
        r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        if r.returncode != 0:
            failures += 1
            print(f"FAIL {page} script[{i}]: {r.stderr.splitlines()[0] if r.stderr else 'parse error'}")
        else:
            print(f"ok {page} script[{i}]")
if failures:
    print(f"test_render: FAIL ({failures} unparseable script block(s))")
    sys.exit(1)
print("test_render: PASS")

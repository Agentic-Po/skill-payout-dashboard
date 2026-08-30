#!/usr/bin/env python3
"""Execution-level render gate: the page scripts must RUN on real data.

Cycle-3 Loop 2, item 1. test_render.py proves every <script> PARSES; parse
proves nothing about paint. A runtime TypeError after a data-shape change
(the Aug-29 class, one layer up) blanks the page while every parse/data
check stays green. This gate executes index.html's scripts under
tests/domshim.js — a committed, hand-written, dependency-free DOM shim —
with the REAL data.json in the /*__DATA__*/ slot (index.html ships with the
data already injected, in which case it runs verbatim), fails on ANY
uncaught error, and then asserts the run produced actual signal in the
recorded DOM structure:

  * the daily table container (#dailyT) got > 0 <tr> rows,
  * the hero/plain-English strip (#plainStrip) text contains a "$" figure,
  * the daily size-band mix produced > 0 band divs.

Requires node on PATH (present on GitHub runners); in CI a missing node
FAILS — a skipped gate is a lying-green gate.

  python3 tests/test_render_exec.py [page.html]     (no network, <30s)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

if not shutil.which("node"):
    if os.environ.get("CI"):
        print("FAIL: node not available in CI — render exec gate cannot run")
        sys.exit(1)
    print("SKIP: node not available — render exec gate not enforced this run")
    sys.exit(0)

# Reads the structure the shim recorded; printed as one greppable line.
PROBE = r"""
;(function () {
  const els = globalThis.__domshim.elements;
  const html = id => String((els.get(id) || {}).innerHTML || "");
  const out = {
    daily_rows: (html("dailyT").match(/<tr\b/g) || []).length,
    band_divs: (html("dailyT").match(/height:10px;background:/g) || []).length,
    strip_text: html("plainStrip")
  };
  console.log("__RENDER_PROBE__" + JSON.stringify(out));
})();
"""


def main():
    page = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "index.html")
    shim = open(os.path.join(HERE, "domshim.js")).read()
    data = open(os.path.join(ROOT, "data.json")).read()
    html = open(page).read()
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert scripts, f"{page} has no inline <script> blocks"

    failures = 0
    probe = {"daily_rows": 0, "band_divs": 0, "strip_text": ""}
    for i, script in enumerate(scripts):
        # index.html ships with the data already injected (no marker left);
        # template.html still carries the slot — either way this runs the
        # REAL data.json, never the parse gate's {} stand-in.
        src = shim + "\n" + script.replace("/*__DATA__*/", data) + "\n" + PROBE
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(src)
            path = fh.name
        try:
            r = subprocess.run(["node", path], capture_output=True, text=True, timeout=25)
        finally:
            os.unlink(path)
        if r.returncode != 0:
            failures += 1
            err = (r.stderr or "uncaught error").strip().splitlines()
            print(f"FAIL {os.path.basename(page)} script[{i}] raised at runtime:")
            for line in err[:8]:
                print(f"    {line}")
            continue
        m = re.search(r"__RENDER_PROBE__(\{.*\})", r.stdout)
        assert m, f"script[{i}] ran but the probe line is missing:\n{r.stdout[-500:]}"
        p = json.loads(m.group(1))
        for k in ("daily_rows", "band_divs"):
            probe[k] = max(probe[k], p[k])
        probe["strip_text"] = probe["strip_text"] or p["strip_text"]
        print(f"ok {os.path.basename(page)} script[{i}] executed clean "
              f"(daily_rows={p['daily_rows']}, band_divs={p['band_divs']})")

    if failures:
        print(f"test_render_exec: FAIL ({failures} script(s) raised at runtime)")
        sys.exit(1)

    # produced signal, not just absence of error
    assert probe["daily_rows"] > 0, "daily table (#dailyT) rendered zero rows"
    assert probe["band_divs"] > 0, "size-band mix rendered zero band divs"
    assert "$" in probe["strip_text"], \
        f"hero strip has no $ figure: {probe['strip_text'][:200]!r}"
    print(f"ok signal: {probe['daily_rows']} daily rows · {probe['band_divs']} band divs "
          f"· hero strip carries a $ figure")
    print("test_render_exec: PASS")


if __name__ == "__main__":
    main()

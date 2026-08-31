#!/usr/bin/env python3
"""Execution-level render gate: the page scripts must RUN on real data.

Cycle-3 Loop 2, item 1. test_render.py proves every <script> PARSES; parse
proves nothing about paint. A runtime TypeError after a data-shape change
(the Aug-29 class, one layer up) blanks the page while every parse/data
check stays green. This gate executes each built page's scripts under
tests/domshim.js — a committed, hand-written, dependency-free DOM shim —
with that page's REAL data file in the /*__DATA__*/ slot (a built page ships
with the data already injected, in which case it runs verbatim), fails on ANY
uncaught error, and then asserts the run produced actual signal in the
recorded DOM structure.

index.html (data.json):
  * the daily table container (#dailyT) got > 0 <tr> rows,
  * the hero/plain-English strip (#plainStrip) text contains a "$" figure,
  * the daily size-band mix produced > 0 band divs,
  * the executive summary block (#execSummary) rendered non-empty with a
    "$" figure (Cycle-3 Loop 3, item 1).

coupon.html (coupon_data.json):
  * the daily claims table (#dailyT) got > 0 <tr> rows,
  * the coupon-size mix produced > 0 band divs,
  * the summary strip (#couponStrip) rendered non-empty with a "$" figure,
  * the claimant concentration table (#topT) got > 0 rows.

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

# Reads the structure the shim recorded; printed as one greppable line. The
# id list is the union across pages — a page that has no such element records
# an empty string, which its own assertions simply do not look at.
PROBE = r"""
;(function () {
  const els = globalThis.__domshim.elements;
  const html = id => String((els.get(id) || {}).innerHTML || "");
  const text = id => String((els.get(id) || {}).textContent || "");
  const out = {
    daily_rows: (html("dailyT").match(/<tr\b/g) || []).length,
    band_divs: (html("dailyT").match(/height:10px;background:/g) || []).length,
    strip_text: html("plainStrip"),
    exec_text: text("execSummary"),
    coupon_strip: text("couponStrip"),
    top_rows: (html("topT").match(/<tr\b/g) || []).length
  };
  console.log("__RENDER_PROBE__" + JSON.stringify(out));
})();
"""


def _checks_index(p):
    assert p["daily_rows"] > 0, "daily table (#dailyT) rendered zero rows"
    assert p["band_divs"] > 0, "size-band mix rendered zero band divs"
    assert "$" in p["strip_text"], f"hero strip has no $ figure: {p['strip_text'][:200]!r}"
    # executive summary block (Cycle-3 Loop 3, item 1): must have RENDERED
    # non-empty, with a $ figure — server-computed text actually injected.
    assert p["exec_text"].strip(), "executive summary block (#execSummary) rendered empty"
    assert "$" in p["exec_text"], \
        f"executive summary has no $ figure: {p['exec_text'][:200]!r}"
    return (f"{p['daily_rows']} daily rows · {p['band_divs']} band divs · hero strip "
            f"carries a $ figure · exec block rendered non-empty")


def _checks_coupon(p):
    assert p["daily_rows"] > 0, "coupon daily table (#dailyT) rendered zero rows"
    assert p["band_divs"] > 0, "coupon-size mix rendered zero band divs"
    assert p["coupon_strip"].strip(), "coupon summary strip (#couponStrip) rendered empty"
    assert "$" in p["coupon_strip"], \
        f"coupon summary strip has no $ figure: {p['coupon_strip'][:200]!r}"
    assert p["top_rows"] > 0, "claimant concentration table (#topT) rendered zero rows"
    return (f"{p['daily_rows']} daily rows · {p['band_divs']} band divs · summary strip "
            f"carries a $ figure · {p['top_rows']} concentration rows")


# (page, injected data file, signal assertions). Each page is executed with
# ITS OWN data block — coupon.html embeds coupon_data.json, never data.json.
PAGES = [("index.html", "data.json", _checks_index),
         ("coupon.html", "coupon_data.json", _checks_coupon)]


def run_page(page, data_rel, checks, shim):
    data = open(os.path.join(ROOT, data_rel)).read()
    html = open(os.path.join(ROOT, page)).read()
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert scripts, f"{page} has no inline <script> blocks"

    failures = 0
    probe = {"daily_rows": 0, "band_divs": 0, "strip_text": "", "exec_text": "",
             "coupon_strip": "", "top_rows": 0}
    for i, script in enumerate(scripts):
        # the built page ships with the data already injected (no marker
        # left); the template still carries the slot — either way this runs
        # the REAL data file, never the parse gate's {} stand-in.
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
            print(f"FAIL {page} script[{i}] raised at runtime:")
            for line in err[:8]:
                print(f"    {line}")
            continue
        m = re.search(r"__RENDER_PROBE__(\{.*\})", r.stdout)
        assert m, f"{page} script[{i}] ran but the probe line is missing:\n{r.stdout[-500:]}"
        p = json.loads(m.group(1))
        for k in ("daily_rows", "band_divs", "top_rows"):
            probe[k] = max(probe[k], p.get(k, 0))
        for k in ("strip_text", "exec_text", "coupon_strip"):
            probe[k] = probe[k] or p.get(k, "")
        print(f"ok {page} script[{i}] executed clean "
              f"(daily_rows={p['daily_rows']}, band_divs={p['band_divs']})")
    if failures:
        return failures
    print(f"ok {page} signal: {checks(probe)}")
    return 0


def main():
    shim = open(os.path.join(HERE, "domshim.js")).read()
    want = os.path.basename(sys.argv[1]) if len(sys.argv) > 1 else None
    pages = [x for x in PAGES if want is None or x[0] == want]
    assert pages, f"no known page matches {want!r} — known: {[p[0] for p in PAGES]}"
    failures = sum(run_page(page, data_rel, checks, shim)
                   for page, data_rel, checks in pages)
    if failures:
        print(f"test_render_exec: FAIL ({failures} script(s) raised at runtime)")
        sys.exit(1)
    print("test_render_exec: PASS")


if __name__ == "__main__":
    main()

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
    top_rows: (html("topT").match(/<tr\b/g) || []).length,
    legend_chips: (html("bleg").match(/class="bandchip"/g) || []).length,
    v2_tiles: (html("rtiles").match(/class="tile"/g) || []).length,
    v2_foot: html("rfoot").replace(/<[^>]+>/g, ""),
    legline: html("legline").replace(/<[^>]+>/g, ""),
    // 2026-09-15 iteration: A1 creator-wallet card, A4 header age, X4 stacked hourly, C4 monitor prose
    creator_rows: (html("creatorT").match(/<tr\b/g) || []).length,
    creator_tiles: (html("creatorTiles").match(/class="tile"/g) || []).length,
    creator_tabs: (html("creatorTabs").match(/data-tab=/g) || []).length,
    creator_note: html("creatorNote").replace(/<[^>]+>/g, ""),
    gen_text: text("gen"),
    hourly_groups: (html("hourly").match(/var\(--band[0-9]+\)/g) || []).length,
    hourly_line: (html("hourly").match(/<polyline/g) || []).length,
    gT_text: html("gT").replace(/<[^>]+>/g, ""),
    patsum: html("patSum").replace(/<[^>]+>/g, ""),
    ftiles: html("ftiles").replace(/<[^>]+>/g, ""),
    gaps_rows: (html("gapsT").match(/<tr\b/g) || []).length,
    mix_toggle: (html("mixToggle").match(/data-mix=/g) || []).length
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
    # Creator Rewards v2 (2026-09-15): 13 legend chips (b0005 + b005 added,
    # b010/b1 kept), the v2 tile row (TWO tiles since the iteration — no
    # implied user spend) with a $ figure and the HKT footer, the system
    # free top-up line rendered — and no cap figure or cap sentence anywhere.
    assert p["legend_chips"] == 13, f"legend rendered {p['legend_chips']} chips, want 13"
    assert p["v2_tiles"] == 2, f"rewards_v2 rendered {p['v2_tiles']} tiles, want 2"
    assert "HKT" in p["v2_foot"] and "cap" not in p["v2_foot"].lower(), p["v2_foot"]
    assert "$4" not in p["v2_foot"] and "4.00" not in p["v2_foot"], f"cap figure leaked: {p['v2_foot']!r}"
    assert "system free top-ups" in p["legline"] and "$" in p["legline"], p["legline"]
    assert "legacy" not in p["legline"].lower(), p["legline"]
    # A1: four tabs, a tile row and a top-10 table rendered on the default
    # (since-resume) tab; the note names wallets, never "creators"
    assert p["creator_tabs"] == 4, f"creator card rendered {p['creator_tabs']} tabs, want 4"
    assert p["creator_tiles"] >= 3, f"creator card rendered {p['creator_tiles']} tiles"
    assert 1 <= p["creator_rows"] <= 11, f"creator table rendered {p['creator_rows']} rows"
    assert "resumed" in p["creator_note"].lower() and "HKT" in p["creator_note"], p["creator_note"]
    # A4: header carries an HKT time and an age, computed client-side
    assert "HKT" in p["gen_text"] and ("ago" in p["gen_text"] or "just now" in p["gen_text"]), p["gen_text"]
    # X4: hourly bars are group-coloured and the creator-wallet line exists
    assert p["hourly_groups"] > 0 and p["hourly_line"] == 1, (p["hourly_groups"], p["hourly_line"])
    # X3: count/USD toggle present; ftiles subtitle names the groups
    assert p["mix_toggle"] == 2, p["mix_toggle"]
    assert "rewards $" in p["ftiles"] and "credits $" in p["ftiles"], p["ftiles"][:300]
    # C4: the pattern monitor publishes NO counts, amounts or verdicts
    for bad in ("flagged", "monitored", "$"):
        assert bad not in p["gT_text"].split("Retired")[0], f"pattern monitor prose carries {bad!r}: {p['gT_text'][:200]!r}"
    # (#patSum is static markup now — the shim records only script writes, so
    # an EMPTY probe is the pass; any script-written count or status is red)
    assert not re.search(r"\d|flagged|monitored", p["patsum"]), p["patsum"]
    # C11: gaps table rendered with an Opened column
    assert p["gaps_rows"] > 1, p["gaps_rows"]
    # a day on/after the v2 resume carries both new bands; every day before
    # it carries neither (the mix bar is unchanged for history)
    D = json.load(open(os.path.join(ROOT, "data.json")))
    new = [d for d in D["facts"]["daily"] if d["d"] >= "2026-09-14"]
    old = [d for d in D["facts"]["daily"] if d["d"] < "2026-09-14"]
    assert any("b005" in d["bands"] and "b0005" in d["bands"] for d in new), "no day since 2026-09-14 renders b005 + b0005"
    assert not any("b005" in d["bands"] or "b0005" in d["bands"] for d in old), "a pre-v2 day carries a v2 band"
    return (f"{p['daily_rows']} daily rows · {p['band_divs']} band divs · hero strip "
            f"carries a $ figure · exec block rendered non-empty · 13 legend chips · "
            f"2 rewards_v2 tiles · system top-up line · creator card {p['creator_tabs']} tabs / "
            f"{p['creator_rows']} rows · header {p['gen_text']!r} · hourly stacked by group · "
            f"monitor prose status-free · v2 bands only on days ≥ 2026-09-14")


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
             "coupon_strip": "", "top_rows": 0, "legend_chips": 0, "v2_tiles": 0,
             "v2_foot": "", "legline": "", "creator_rows": 0, "creator_tiles": 0,
             "creator_tabs": 0, "creator_note": "", "gen_text": "", "hourly_groups": 0,
             "hourly_line": 0, "gT_text": "", "patsum": "", "ftiles": "", "gaps_rows": 0,
             "mix_toggle": 0}
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
        for k in ("daily_rows", "band_divs", "top_rows", "legend_chips", "v2_tiles",
                  "creator_rows", "creator_tiles", "creator_tabs", "hourly_groups",
                  "hourly_line", "gaps_rows", "mix_toggle"):
            probe[k] = max(probe[k], p.get(k, 0))
        for k in ("strip_text", "exec_text", "coupon_strip", "v2_foot", "legline",
                  "creator_note", "gen_text", "gT_text", "patsum", "ftiles"):
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

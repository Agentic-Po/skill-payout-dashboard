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

full.html (data.json — the full render the private edition shows):
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

Loop 2 (2026-09-27): all of a page's scripts now run in ONE shared realm
(tests/render_harness.js, as a browser does), with the page's own markup
parsed into the shim's id tree (tests/pagehost.py). full.html additionally
must finish with window.__renderErrors EMPTY on the real data (a section
that degraded to "unavailable" on real data is red here), must have removed
the fail-closed loading banner, and:
  * stale banner: hidden at build+89 min; shown after the 60 s re-check
    once the (fake) clock passes build+90 min; shown on visibilitychange
    after a clock jump that fires no timer;
  * fail-closed: invalid inlined JSON, a syntax error in the script, or a
    null data block leaves the loading banner up (and never throws past
    window.onerror); the removal is the script's last statement and is
    conditional on zero errors.

Requires node on PATH (present on GitHub runners); in CI a missing node
FAILS — a skipped gate is a lying-green gate.

  python3 tests/test_render_exec.py [page.html]     (no network, <30s)
"""
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import pagehost as P  # noqa: E402

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
(function () {
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
    ftiles_html: html("ftiles"),
    gaps_rows: (html("gapsT").match(/<tr\b/g) || []).length,
    mix_toggle: (html("mixToggle").match(/data-mix=/g) || []).length
  };
  return out;
})()
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
    _checks_headline(p, json.load(open(os.path.join(ROOT, "data.json"))))
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


def _checks_headline(p, data):
    """Group subtitles exist exactly when that window carries nonzero USD."""
    tiles = re.findall(r'<div class="k">(.*?)</div><div class="v">(.*?)</div>(.*?)</div></div>',
                       p["ftiles_html"], re.S)
    assert len(tiles) == 6, "headline tiles missing"
    out = next((t for t in tiles if t[0] == "Outflow — 24h"), None)
    assert out is not None, "24h outflow tile missing"
    assert "$" in out[1], "24h outflow value missing"
    window = data["facts"]["windows"][0]
    labels = {"skill_rewards":"rewards", "credit_grants":"credits",
              "system_topups":"system top-ups", "topups_delivered":"top-ups",
              "ops":"ops", "micro":"dust"}
    groups = window.get("groups")
    if groups is not None:
        for key in data["facts"].get("group_keys", labels):
            present = bool(groups.get(key, {}).get("usd"))
            shown = bool(re.search(r"(?<![\w-])" + re.escape(labels[key]) + r" \$(?=[\d,])", out[2]))
            if key == "topups_delivered":
                shown = bool(re.search(r"(?:^| · )top-ups \$(?=[\d,])", out[2]))
            assert shown == present, "24h group subtitle does not match its window"
    else:
        assert f"{window['out_tx']} tx" in out[2] and f"{window['out_wallets']} wallets" in out[2]
    warning = "as of last complete fetch"
    assert (warning in p["ftiles"]) == (not data["scope"]["complete"]), "headline coverage warning incorrect"


def check_headline_windows():
    """A zero 24h window still paints all tiles; incomplete coverage stays visible."""
    data = json.load(open(os.path.join(ROOT, "data.json")))
    page = P.build_from_template(open(os.path.join(ROOT, "template.html")).read())
    cases = []
    import copy
    for complete in (True, False):
        fixture = copy.deepcopy(data)
        window = fixture["facts"]["windows"][0]
        for key in ("out_usd", "out_tx", "out_wallets", "economy_out_usd", "ops_out_usd"):
            window[key] = 0
        for group in window["groups"].values():
            for key in ("usd", "n", "wallets"):
                group[key] = 0
        fixture["scope"]["complete"] = complete
        cases.append(fixture)
    nonzero = copy.deepcopy(data)
    nonzero["facts"]["windows"][0]["groups"]["skill_rewards"]["usd"] = 1
    nonzero["facts"]["windows"][0]["groups"]["credit_grants"]["usd"] = 2
    cases.append(nonzero)
    last_good = copy.deepcopy(nonzero)
    last_good['scope']['complete'] = False
    last_good['scope']['build_generated_iso'] = '2099-01-01T00:00:00Z'
    last_good['scope']['source_coverage'] = {'coverage_ok':False,'pending':[{'complete':False}]}
    cases.append(last_good)
    for fixture in cases:
        result = P.run(page, json.dumps(fixture), [{"probe": PROBE}])[0]
        assert not result["uncaught"] and not result["renderErrors"], "headline fixture degraded"
        assert _banner(result, "loadBanner").get("removed"), "headline fixture kept loading banner"
        _checks_headline(result["probe"], fixture)
        if not fixture["facts"]["windows"][0]["out_usd"]:
            assert re.search(r'<div class="v">\$0(?:\.0+)?</div>', result["probe"]["ftiles_html"]), "zero window did not render a zero dollar value"
    print("ok headline windows: zero/nonzero subtitles match data; incomplete coverage warning retained")
    return 0


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
PAGES = [("full.html", "data.json", _checks_index),
         ("coupon.html", "coupon_data.json", _checks_coupon)]


def run_page(page, data_rel, checks, shim=None):
    data = open(os.path.join(ROOT, data_rel)).read()
    html = open(os.path.join(ROOT, page)).read()
    scripts = P.scripts_of(html)
    assert scripts, f"{page} has no inline <script> blocks"
    # the built page ships with the data already injected (no marker left);
    # a template still carries the slot — either way this runs the REAL data
    # file, never the parse gate's {} stand-in. All scripts share one realm.
    r = P.run(html, data, [{"probe": PROBE}])[0]
    if r["uncaught"]:
        print(f"FAIL {page} raised at runtime (uncaught):")
        for u in r["uncaught"][:8]:
            print(f"    {u['where']}: {u['msg']}")
        return 1
    p = r["probe"]
    assert p and "__probe_error" not in p, f"{page} probe failed: {p}"
    print(f"ok {page} {len(scripts)} script(s) executed clean in one realm "
          f"(daily_rows={p['daily_rows']}, band_divs={p['band_divs']})")
    if page == "full.html":
        errs = r["renderErrors"]
        assert errs == [], f"full.html: sections degraded on REAL data: {errs}"
        ids = r["dump"]["ids"]
        assert ids.get("loadBanner", {}).get("removed"), \
            "full.html: clean run did not remove the fail-closed loading banner"
        assert ids.get("renderBanner", {}).get("hidden", True), "page banner raised on real data"
        print("ok full.html window.__renderErrors == [] · loading banner removed · page banner hidden")
    print(f"ok {page} signal: {checks(p)}")
    return 0


def _banner(res, bid):
    return res["dump"]["ids"].get(bid, {})


def check_stale_and_fail_closed():
    """Clock-driven stale banner + fail-closed loading banner, on the template."""
    tpl = open(os.path.join(ROOT, "template.html")).read()
    page = P.build_from_template(tpl)
    data = open(os.path.join(ROOT, "data.json")).read()
    gen = P.generated_ms(data)
    MIN = 60000
    runs = [
        {"now": gen + 89 * MIN},                                        # 0 fresh
        {"now": gen + 89 * MIN, "after": [{"advance": 2 * MIN}]},       # 1 interval re-check
        {"now": gen + 10 * MIN, "after": [{"setNow": gen + 95 * MIN},
                                          {"visibility": "visible"}]},  # 2 tab re-shown
        {"now": gen + 10 * MIN, "after": [{"setNow": gen + 95 * MIN}]},  # 3 no timer, no event
        {"now": gen, "rawData": "{\"facts\": "},                       # 4 invalid JSON
        {"now": gen, "rawData": "null"},                                # 5 no data at all
    ]
    r = P.run(page, data, runs)
    assert _banner(r[0], "staleBanner").get("hidden", True), "stale banner up at build+89 min"
    assert not _banner(r[1], "staleBanner").get("hidden", True), \
        "stale banner NOT raised by the 60 s re-check after build+90 min"
    assert not _banner(r[2], "staleBanner").get("hidden", True), \
        "stale banner NOT raised on visibilitychange after build+90 min"
    assert _banner(r[3], "staleBanner").get("hidden", True), \
        "stale banner changed with no timer and no event (test is not measuring the trigger)"
    for i in (0, 1, 2):
        assert not r[i]["uncaught"] and r[i]["renderErrors"] == [], (i, r[i]["uncaught"], r[i]["renderErrors"])
    txt = r[1]["dump"]["ids"]["staleBanner"]["text"] or ""
    assert "old" in txt and "stale" in txt, txt
    # invalid inlined JSON: the script never parses -> loading banner stays
    assert r[4]["uncaught"], "invalid JSON did not fail the script?"
    assert not _banner(r[4], "loadBanner").get("removed"), "invalid JSON removed the loading banner"
    # null data: every data section degrades to "unavailable", nothing
    # escapes, and the PAGE banner (not the loading banner) says so — the
    # script did finish, so the loading banner goes (QA F1)
    assert not r[5]["uncaught"], f"null data escaped renderSection: {r[5]['uncaught']}"
    assert r[5]["renderErrors"], "null data rendered with zero errors?"
    assert not _banner(r[5], "renderBanner").get("hidden", True), "null data did not raise the page banner"
    # syntax error in the script -> loading banner stays
    broken = page.replace("renderSection(\"Headline strip\"", "renderSection((\"Headline strip\"", 1)
    assert broken != page
    rb = P.run(broken, data, [{"now": gen}])[0]
    assert rb["uncaught"] and not _banner(rb, "loadBanner").get("removed"), \
        "a syntax error removed the loading banner"
    # static: banner visible by default in markup (with JS on it is hidden by
    # a <head> class and shown if the script has not finished in 5 s — QA F2:
    # no alarming flash on slow loads); removal is the LAST statement of the
    # last script, unconditional (QA F1: a failed SECTION raises its own
    # banner; only a script that never reaches its end keeps this one)
    st = P.parse_static(page).tree
    lb = st.get("loadBanner")
    assert lb and "hidden" not in lb["attrs"], "loading banner missing from markup or hidden by default"
    last = P.scripts_of(page)[-1].strip().splitlines()[-1].strip()
    assert re.fullmatch(r'\{const lb=document\.getElementById\("loadBanner"\);if\(lb\)lb\.remove\(\);\}', last), \
        f"last statement is not the loading-banner removal: {last!r}"
    head = page.split("</head>")[0] if "</head>" in page else page.split("<style>")[0] + page.split("</style>")[0]
    assert 'className+=" js"' in head and ".js #loadBanner{display:none}" in head \
        and "classList.add(\"show\")" in head, "no-flash <head> guard for the loading banner is missing"
    print("ok stale banner: hidden at +89 min, raised by the 60 s re-check and by "
          "visibilitychange past +90 min · fail-closed: invalid JSON / syntax error / null "
          "data keep the loading banner; removal is the script's last statement; no-flash head guard present")
    return 0


def check_public_page():
    """index.html — the one-screen public summary. Its payload is exactly
    util.public_summary(data.json); it renders clean on real data, goes stale
    past the threshold, degrades visibly on null data, and fails closed."""
    import sys as _s
    _s.path.insert(0, ROOT)
    import util
    D = json.load(open(os.path.join(ROOT, "data.json")))
    built = open(os.path.join(ROOT, "index.html")).read()
    want = json.dumps(util.public_summary(D)).replace("</", "<\\/")
    assert "const D0=" + want + ";" in built, "index.html payload != util.public_summary(data.json) — rebuild the page"
    data = open(os.path.join(ROOT, "data.json")).read()
    gen = P.generated_ms(data)
    MIN = 60000
    r = P.run(built, data, [{"now": gen + 10 * MIN}, {"now": gen + 95 * MIN}])
    for i in (0, 1):
        assert not r[i]["uncaught"] and r[i]["renderErrors"] == [], (i, r[i]["uncaught"], r[i]["renderErrors"])
    ids = r[0]["dump"]["ids"]
    assert ids.get("loadBanner", {}).get("removed"), "index.html: clean run kept the loading banner"
    assert _banner(r[0], "staleBanner").get("hidden", True), "index.html: stale banner up at +10 min"
    assert not _banner(r[1], "staleBanner").get("hidden", True), "index.html: stale banner NOT up at +95 min"
    assert ids["execSummary"]["text"] == D["exec_summary"]["text"], "index.html summary text != data.json"
    for t in ("tBal", "t24", "t7", "t30"):
        assert (ids[t]["text"] or "").startswith("$"), f"index.html tile {t} shows {ids[t]['text']!r}"
    tpl = open(os.path.join(ROOT, "template_public.html")).read().replace("__STALE_MIN__", "90")
    page = "<!doctype html>\n<html lang=\"en\">\n" + tpl + "\n</html>"
    rn = P.run(page, data, [{"now": gen, "rawData": "null"}, {"now": gen, "rawData": "{\"facts\": "}])
    assert not rn[0]["uncaught"] and rn[0]["renderErrors"], "null data: no section degraded, or one escaped"
    assert not _banner(rn[0], "renderBanner").get("hidden", True), "null data did not raise the page banner"
    assert rn[1]["uncaught"] and not _banner(rn[1], "loadBanner").get("removed"), "invalid JSON removed the loading banner"
    print("ok index.html (public summary): payload == util.public_summary · clean on real data · "
          "stale at +95 min · null data -> page banner · invalid JSON keeps the loading banner")
    return 0


def main():
    shim = None
    want = os.path.basename(sys.argv[1]) if len(sys.argv) > 1 else None
    pages = [x for x in PAGES if want is None or x[0] == want]
    assert pages, f"no known page matches {want!r} — known: {[p[0] for p in PAGES]}"
    failures = sum(run_page(page, data_rel, checks, shim)
                   for page, data_rel, checks in pages)
    if want in (None, "full.html") and not failures:
        failures += check_stale_and_fail_closed()
        failures += check_headline_windows()
    if want in (None, "index.html") and not failures:
        failures += check_public_page()
    if failures:
        print(f"test_render_exec: FAIL ({failures} script(s) raised at runtime)")
        sys.exit(1)
    print("test_render_exec: PASS")


if __name__ == "__main__":
    main()

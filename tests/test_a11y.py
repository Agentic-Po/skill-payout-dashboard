#!/usr/bin/env python3
"""Accessibility + contrast gate for the main page (council loop 2, 2026-09-27).

Static checks on template.html (plus one execution under the shim for the
script-built controls). No restyling of meaning is asserted here — only
that the page's controls, tables, charts and chips are reachable and legible:

  * the creator-window tabs and the USD/count mix toggle are real
    <button type="button"> elements with aria-pressed — never a click-only
    <span> — and exactly one of each group is pressed;
  * the page has one <main> landmark;
  * every <th>, static or script-built, carries scope;
  * every chart <svg> has role="img" and an aria-label (or a <title>);
  * daily rows are not tab stops (no tabindex; bindTips(dailyT, false));
  * Escape closes tooltips; :focus-visible has an outline;
    prefers-reduced-motion disables smooth scroll and transitions;
  * contrast >= 4.5:1, computed from the CSS custom properties in BOTH the
    light and the dark token blocks: band-chip text on the chip, tooltip
    chip text, the --bad pill on surface and on bg, pressed-button text on
    --acc, and the registry warning pill; the old hard-coded #d33 is gone.

  python3 tests/test_a11y.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pagehost as P  # noqa: E402

TPL = open(os.path.join(P.ROOT, "template.html")).read()
failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


# ---------------- tokens ----------------
def _block(css, selector_rx):
    m = re.search(selector_rx + r"\s*\{(.*?)\}", css, re.S)
    assert m, f"token block {selector_rx!r} not found"
    return dict(re.findall(r"--([\w-]+)\s*:\s*([^;]+);", m.group(1)))


def themes():
    style = re.search(r"<style>(.*?)</style>", TPL, re.S).group(1)
    light = _block(style, r"(?m)^:root")
    dark = _block(style, r"@media \(prefers-color-scheme: dark\)\{:root")
    dark_forced = _block(style, r':root\[data-theme="dark"\]')
    light_forced = _block(style, r':root\[data-theme="light"\]')
    check(dark == dark_forced, "dark tokens differ between the media query and [data-theme=dark]")
    check(light == light_forced, "light tokens differ between :root and [data-theme=light]")
    return {"light": light, "dark": dark}, style


def _lum(hexc):
    h = hexc.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    rgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def resolve(val, tokens):
    val = val.strip()
    m = re.fullmatch(r"var\(--([\w-]+)(?:,[^)]*)?\)", val)
    if m:
        return resolve(tokens[m.group(1)], tokens)
    assert re.fullmatch(r"#[0-9a-fA-F]{3,6}", val), f"not a resolvable colour: {val!r}"
    return val


def rule(style, selector):
    m = re.search(r"(?<![\w.-])" + re.escape(selector) + r"\s*\{([^}]*)\}", style)
    assert m, f"CSS rule {selector!r} not found"
    return dict(re.findall(r"([\w-]+)\s*:\s*([^;]+)", m.group(1)))


T, STYLE = themes()
chip = rule(STYLE, ".bandchip")
tipchip = rule(STYLE, ".tip .bandchip")
pairs = [
    ("band chip text on chip", chip.get("color", ""), chip.get("background", "")),
    ("tooltip chip text on tooltip chip", tipchip.get("color", ""), tipchip.get("background", "")),
    ("--bad pill on surface", "var(--bad)", "var(--surface)"),
    ("--bad pill on page bg", "var(--bad)", "var(--bg)"),
    ("pressed tab / toggle text on --acc", "var(--surface)", "var(--acc)"),
    ("registry warning pill text on --warn", "var(--surface)", "var(--warn)"),
    ("body text on surface", "var(--ink)", "var(--surface)"),
]
check("border-left" in chip and "var(--chip" in chip["border-left"],
      ".bandchip must carry the band hue as a left stripe (border-left: … var(--chip))")
for theme, tokens in T.items():
    for name, fg, bg in pairs:
        try:
            c = contrast(resolve(fg, tokens), resolve(bg, tokens))
        except (AssertionError, KeyError) as e:
            failures.append(f"{theme}: {name}: cannot resolve ({e})")
            continue
        check(c >= 4.5, f"{theme}: {name} contrast {c:.2f}:1 < 4.5:1 ({fg} on {bg})")
check("#d33" not in TPL.lower(), "hard-coded #d33 still present — use var(--bad)")
check("color:#fff" not in TPL.replace(" ", ""), "hard-coded white text on a token background remains")
# chips must not set a background hue inline any more (the stripe carries it)
check(not re.search(r'class="bandchip" style="background:', TPL), "a band chip still paints its hue as background")

# ---------------- controls ----------------
check(not re.search(r"<span[^>]*data-(tab|mix)=", TPL), "creator tabs / mix toggle still rendered as <span>")
check(re.search(r'<button type="button" class="pill" data-tab="\$\{k\}" aria-pressed=', TPL),
      "creator tabs are not <button type=button aria-pressed>")
check(re.search(r'<button type="button" class="pill" data-mix="\$\{m\}" aria-pressed=', TPL),
      "mix toggle is not <button type=button aria-pressed>")
check(len(re.findall(r"<main\b", TPL)) == 1 and "</main>" in TPL, "expected exactly one <main> landmark")
bare_th = [m.group(0) for m in re.finditer(r"<th(?=[\s>])[^>]*>", TPL) if "scope=" not in m.group(0)]
check(not bare_th, f"<th> without scope: {bare_th[:5]}")
for m in re.finditer(r"<svg\b[^>]*>", TPL):
    tag = m.group(0)
    check('role="img"' in tag and ("aria-label=" in tag or "<title" in TPL[m.end():m.end() + 200]),
          f"chart svg without role=img + label: {tag[:90]}")
# daily rows: not tab stops
body = TPL[TPL.index("function buildRows"):TPL.index("const dHead")]
check("tabindex" not in body, "daily rows set tabindex")
check("bindTips(dailyT,false)" in TPL, "daily table must bind tooltips with focusable=false")
check('if(focusable!==false)el.setAttribute("tabindex","0")' in TPL, "bindTips ignores its focusable flag")
check(re.search(r'addEventListener\("keydown",e=>\{if\(e\.key==="Escape"', TPL), "no Escape handler closing tooltips")
check(re.search(r":focus-visible\{outline:", STYLE), "no :focus-visible outline")
rm = re.search(r"@media \(prefers-reduced-motion: reduce\)\{([^\n]*)", STYLE)
check(rm and "scroll-behavior:auto" in rm.group(1) and "transition:none" in rm.group(1),
      "prefers-reduced-motion must disable smooth scroll and transitions")

# ---------------- rendered controls + tables (execution) ----------------
if P.have_node():
    page = P.build_from_template(TPL)
    data = open(os.path.join(P.ROOT, "data.json")).read()
    r = P.run(page, data, [{"now": P.generated_ms(data)}])[0]
    ids = r["dump"]["ids"]
    for cid, n in (("creatorTabs", 4), ("mixToggle", 2)):
        h = ids.get(cid, {}).get("html") or ""
        btns = re.findall(r'<button type="button"[^>]*aria-pressed="(true|false)"', h)
        check(len(btns) == n and btns.count("true") == 1,
              f"#{cid}: want {n} buttons with exactly one aria-pressed=true, got {btns}")
    for w in r["dump"]["writes"]:
        for m in re.finditer(r"<th(?=[\s>])[^>]*>", w.get("html", "")):
            if "scope=" not in m.group(0):
                failures.append(f"script-built <th> without scope: {m.group(0)}")
                break
elif os.environ.get("CI"):
    failures.append("node not available in CI")

for f in failures:
    print("FAIL", f)
if failures:
    print(f"test_a11y: FAIL ({len(failures)})")
    sys.exit(1)
print("ok contrast (light + dark): " + " · ".join(
    f"{n} {min(contrast(resolve(fg, T[t]), resolve(bg, T[t])) for t in T):.1f}:1" for n, fg, bg in pairs))
print("test_a11y: PASS")

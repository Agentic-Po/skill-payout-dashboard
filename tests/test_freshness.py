#!/usr/bin/env python3
"""ONE staleness threshold (freshness.STALE_MINUTES) — edges and wiring.

  * health(): threshold-1 min healthy, exactly threshold healthy (the page's
    `ageM > STALE_MIN` is strict), threshold+1 UNHEALTHY; a failed last run is
    UNHEALTHY at any age; the CLI weekly.yml calls agrees, and an unreadable
    age is UNHEALTHY.
  * wiring: template.html carries the __STALE_MIN__ placeholder (no literal),
    the committed index.html renders the constant, refresh.py's degraded flags
    read freshness (no 1.5 literal), weekly.yml asks `freshness.py health`
    with an age in minutes and no longer hard-codes 48 h.
"""
import os, re, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import freshness  # noqa: E402

T = freshness.STALE_MINUTES
fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


check(freshness.health("success", T - 1) == "healthy", "threshold-1 must be healthy")
check(freshness.health("success", T) == "healthy", "exactly the threshold must be healthy (strict >)")
check(freshness.health("success", T + 1) == "UNHEALTHY", "threshold+1 must be UNHEALTHY")
check(freshness.health("failure", 0) == "UNHEALTHY", "a failed last run is UNHEALTHY even when fresh")
check(freshness.health("", 0) == "UNHEALTHY", "an unknown conclusion is UNHEALTHY")
check(freshness.is_stale(T + 0.01) and not freshness.is_stale(T), "is_stale must be strict >")


def cli(*a):
    return subprocess.run([sys.executable, os.path.join(ROOT, "freshness.py"), "health", *a],
                          capture_output=True, text=True).stdout.strip()


check(cli("success", str(T - 1)) == "healthy", "CLI threshold-1 must print healthy")
check(cli("success", str(T + 1)) == "UNHEALTHY", "CLI threshold+1 must print UNHEALTHY")
check(cli("success", "") == "UNHEALTHY", "CLI with an unreadable age must print UNHEALTHY")

tpl = open(os.path.join(ROOT, "template.html")).read()
check("const STALE_MIN=__STALE_MIN__;" in tpl, "template.html must take STALE_MIN from the __STALE_MIN__ placeholder")
check(not re.search(r"const STALE_MIN=\d", tpl), "template.html hard-codes STALE_MIN")
for page in ("full.html", "index.html"):
    idx = open(os.path.join(ROOT, page)).read()
    check(f"const STALE_MIN={T};" in idx, f"{page} does not render STALE_MIN={T}")
    check("__STALE_MIN__" not in idx, f"{page} shipped an unsubstituted __STALE_MIN__")
pt = open(os.path.join(ROOT, "template_public.html")).read()
check("const STALE_MIN=__STALE_MIN__;" in pt, "template_public.html must take STALE_MIN from the placeholder")

src = open(os.path.join(ROOT, "refresh.py")).read()
for flag in ("_exec_degraded", "_cp_degraded"):
    m = re.search(rf"^{flag} = (.*)$", src, re.M)
    check(m and "freshness.STALE_MINUTES" in m.group(1), f"refresh.py {flag} must read freshness.STALE_MINUTES")
check('replace("__STALE_MIN__", str(freshness.STALE_MINUTES))' in src,
      "refresh.py must substitute __STALE_MIN__ from freshness")

wk = open(os.path.join(ROOT, ".github", "workflows", "weekly.yml")).read()
check('freshness.py health "$CONCLUSION" "$AGE_MIN"' in wk, "weekly.yml must call freshness.py health with AGE_MIN")
check("/ 60 ))" in wk and "AGE_MIN=" in wk, "weekly.yml must compute the page age in minutes")
check("-lt 48" not in wk, "weekly.yml still hard-codes the 48 h threshold")
cpt = open(os.path.join(ROOT, "template_coupon.html")).read()
check("__STALE_MIN__" in cpt and "ageH>2.5" not in cpt, "coupon template must use __STALE_MIN__, not a hard-coded 2.5 h")
if os.path.exists(os.path.join(ROOT, "coupon.html")):
    check("__STALE_MIN__" not in open(os.path.join(ROOT, "coupon.html")).read(), "coupon.html left __STALE_MIN__ unsubstituted")

for f in fails:
    print("FAIL", f)
if fails:
    print(f"test_freshness: FAIL ({len(fails)})")
    sys.exit(1)
print(f"ok health edges at {T - 1}/{T}/{T + 1} min; ok wiring (templates, full.html, index.html, refresh.py, weekly.yml)")
print("test_freshness: PASS")

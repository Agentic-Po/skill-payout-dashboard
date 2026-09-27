#!/usr/bin/env python3
"""Data-mutation isolation gate (council loop 2, 2026-09-27).

The page is ONE inline script. Before loop 2 a single missing or malformed
key threw at top level and blanked every section below it — the page still
served, CI stayed green on real data, and the viewer saw a half-empty page
with nothing saying so. This gate runs the page (built from template.html)
under tests/domshim.js with the REAL data.json, mutated one target at a time:

  targets: every top-level key of data.json, plus facts.windows,
           facts.monthly, facts.daily, facts.balance_series, infer.guard
  ops:     delete · set null · first numeric leaf -> NaN · -> string "n/a"

and asserts, per mutation:
  * no uncaught error (window.onerror is the last resort, not the plan);
  * the sections that failed (window.__renderErrors) are a subset of the
    sections that READ the mutated key (DEPENDS below) — every other section
    rendered exactly as on the unmutated run;
  * each failed section's card shows only the plain "unavailable"
    placeholder (no figures left behind, no stack trace);
  * a failure raises the page banner and keeps the fail-closed loading
    banner; a clean run removes the loading banner and keeps the page banner
    hidden;
  * no NaN / Infinity / [object Object] text anywhere on the page.

A mutation the page handles by design (an optional block absent -> its card
stays hidden) is a clean run, not a failure.

  python3 tests/test_render_mutation.py            (template.html, ~5 s)
  python3 tests/test_render_mutation.py --report   (print per-mutation table,
                                                    no assertions — used to
                                                    record the pre-loop-2
                                                    baseline)
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pagehost as P  # noqa: E402

# section name (renderSection's first argument) -> content ids it writes.
SECTIONS = {
    "Executive summary": ["execSummary"],
    "Headline strip": ["plainStrip"],
    "Headline tiles": ["ftiles"],
    "Month by month": ["trendT"],
    "Daily flows": ["dailyT", "bleg"],
    "Treasury balance": ["balT", "balHead"],
    "Transfers per hour": ["hourly"],
    "Scope": ["scope"],
    "Large inflows": ["bigin"],
    "Flow summary": ["winT", "monT"],
    "Top creator wallets": ["creatorT", "creatorTiles"],
    "Cognition consumption": ["cogTiles"],
    "Flow diagram": ["nDist"],
    "Address registry": ["addrT"],
    "Collector sweep": ["sinkBody"],
    "Loop closure": ["gtiles"],
    "Payout classification": ["itiles", "fineT"],
    "Pattern monitor": ["gT"],
    "Top recipients": ["recipT"],
    "Inflow sources": ["srcT", "inLedgerT"],
    "Stripe snapshot": ["stripeLine"],
    "Server-recorded": ["serverLine"],
    "SWARM era": ["swarmTiles"],
    "Open items": ["regT"],
    "Data gaps": ["gapsT"],
}
CARD = {
    "Executive summary": "execSummary", "Headline strip": "plainStrip",
    "Headline tiles": "ftiles", "Month by month": "trendCard",
    "Daily flows": "dailyCard", "Treasury balance": "balCard",
    "Transfers per hour": "hourlyCard", "Scope": "scope", "Large inflows": "bigin",
    "Flow summary": "flowsCard", "Top creator wallets": "creatorCard",
    "Cognition consumption": "cogCard", "Flow diagram": "flowCard",
    "Address registry": "registryCard", "Collector sweep": "sinkCard",
    "Loop closure": "gtiles", "Payout classification": "classCard",
    "Pattern monitor": "patCard", "Top recipients": "recipCard",
    "Inflow sources": "srcCard", "Stripe snapshot": "stripeCard",
    "Server-recorded": "serverCard", "SWARM era": "swarmCard",
    "Open items": "registerCard", "Data gaps": "gapsCard",
}
# Sections that do not render into a card of their own (they write small
# lines into other cards, or banners) — they may fail, never blank a card.
CARDLESS = {"Freshness banners", "Header age", "Insights"}

ALL = set(SECTIONS) | CARDLESS
F_ALL = ALL - {"Executive summary", "Address registry", "Collector sweep",
               "Stripe snapshot", "Server-recorded", "Open items", "Data gaps",
               "Loop closure", "Pattern monitor", "Freshness banners"}
# which sections READ which key — a failure outside this set is a leak.
DEPENDS = {
    "schema_version": set(),
    "scope": {"Scope", "Headline tiles", "Freshness banners", "Header age",
              "Treasury balance"},
    "facts": F_ALL | {"Server-recorded", "Stripe snapshot", "Open items", "Data gaps"},
    "infer": {"Loop closure", "Payout classification", "Pattern monitor"},
    "server": {"Server-recorded", "Freshness banners"},
    "stripe_snap": {"Stripe snapshot"},
    "insights": {"Insights"},
    "open_items": {"Open items", "Headline strip"},
    "gaps": {"Data gaps"},
    "registry": {"Address registry"},
    "sink": {"Collector sweep", "Headline tiles", "Flow diagram"},
    "exec_summary": {"Executive summary"},
    "facts.windows": {"Headline strip", "Headline tiles", "Flow summary", "Flow diagram"},
    "facts.monthly": {"Month by month", "Flow summary"},
    "facts.daily": {"Daily flows"},
    "facts.balance_series": {"Treasury balance"},
    "infer.guard": {"Loop closure"},
}
OPS = ("delete", "null", "nan", "string")
BAD = ("NaN", "Infinity", "[object Object]")


def targets(data):
    t = [[k] for k in data.keys()]
    t += [["facts", "windows"], ["facts", "monthly"], ["facts", "daily"],
          ["facts", "balance_series"], ["infer", "guard"]]
    if "sink" not in data:
        t.append(["sink"])
    return t


def rendered(ids, cid):
    v = ids.get(cid)
    return bool(v) and not v["detached"] and not v["removed"] and v["textLen"] > 0 \
        and not v["placeholder"]


def main():
    report = "--report" in sys.argv
    if not P.have_node():
        if os.environ.get("CI"):
            print("FAIL: node not available in CI — mutation gate cannot run")
            sys.exit(1)
        print("SKIP: node not available — mutation gate not enforced this run")
        sys.exit(0)
    tpl = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") \
        else os.path.join(P.ROOT, "template.html")
    page = P.build_from_template(open(tpl).read())
    data_text = open(os.path.join(P.ROOT, "data.json")).read()
    data = json.loads(data_text)
    now = P.generated_ms(data_text) + 5 * 60000
    muts = [(t, op) for t in targets(data) for op in OPS]
    runs = [{"now": now, "dump": "summary"}] + \
           [{"now": now, "dump": "summary", "mutation": {"path": t, "op": op}} for t, op in muts]
    res = P.run(page, data_text, runs)
    base = res[0]["dump"]["ids"]
    content_ids = [c for ids in SECTIONS.values() for c in ids]
    base_ok = {c for c in content_ids if rendered(base, c)}
    failures = []
    if res[0]["uncaught"] or (res[0]["renderErrors"] or []):
        failures.append(f"unmutated run is not clean: {res[0]['uncaught']} {res[0]['renderErrors']}")
    rows = []
    for (t, op), r in zip(muts, res[1:]):
        key = ".".join(t)
        ids = r["dump"]["ids"]
        blank = sorted(c for c in base_ok if not rendered(ids, c))
        errs = sorted({e["name"] for e in (r["renderErrors"] or [])})
        unc = [u["msg"] for u in r["uncaught"]]
        badt = sorted(c for c, v in ids.items() if v.get("bad") and not v["detached"]
                      and v["static"])
        rows.append((key, op, r["applied"], unc, errs, blank, badt))
        if report:
            continue
        where = f"{key} {op} (leaf {r['applied']})"
        if unc:
            failures.append(f"{where}: uncaught error {unc[:2]}")
        allowed = DEPENDS.get(key)
        if allowed is None:
            failures.append(f"{where}: no DEPENDS entry for {key!r} — declare which sections read it")
            continue
        leak = set(errs) - allowed - {"page"}
        if leak:
            failures.append(f"{where}: sections outside {key}'s readers failed: {sorted(leak)}")
        allowed_ids = {c for s in allowed if s in SECTIONS for c in SECTIONS[s]}
        lost = [c for c in blank if c not in allowed_ids]
        if lost:
            failures.append(f"{where}: unrelated content blanked: {lost}")
        for s in errs:
            cid = CARD.get(s)
            if cid and cid in ids and not ids[cid]["placeholder"]:
                failures.append(f"{where}: failed section {s!r} card #{cid} lacks the placeholder")
            for c in SECTIONS.get(s, []):
                if c in ids and not ids[c]["detached"] and ids[c]["textLen"] > 0 \
                        and not ids[c]["placeholder"] and c != cid:
                    failures.append(f"{where}: failed section {s!r} left #{c} half-rendered")
        rb, lb = ids.get("renderBanner", {}), ids.get("loadBanner", {})
        if errs:
            if rb.get("hidden", True):
                failures.append(f"{where}: {errs} failed but the page banner is hidden")
            if lb.get("removed"):
                failures.append(f"{where}: {errs} failed but the loading banner was removed")
        else:
            if not lb.get("removed"):
                failures.append(f"{where}: clean run left the loading banner up")
            if not rb.get("hidden", True):
                failures.append(f"{where}: clean run raised the page banner")
        bad = sorted(c for c, v in ids.items() if v.get("bad") and not v["detached"])
        if bad:
            failures.append(f"{where}: non-finite text rendered in {bad}")
    if report:
        print(f"{'target':24} {'op':7} {'uncaught':40} blanked (of {len(base_ok)} content ids)")
        for key, op, leaf, unc, errs, blank, badt in rows:
            u = (unc[0][:38] if unc else "-")
            print(f"{key:24} {op:7} {u:40} {len(blank):2} {','.join(blank)[:150]}"
                  + (f"  errs={errs}" if errs else "")
                  + (f"  NaN-text in {badt} (leaf {leaf})" if badt else ""))
        return
    for f in failures:
        print("FAIL", f)
    print(f"{len(muts)} mutations over {len(targets(data))} targets; "
          f"{sum(1 for r in rows if r[4])} degraded to 'unavailable' sections, "
          f"{sum(1 for r in rows if not r[4])} handled by design")
    if failures:
        print(f"test_render_mutation: FAIL ({len(failures)})")
        sys.exit(1)
    print("test_render_mutation: PASS")


if __name__ == "__main__":
    main()

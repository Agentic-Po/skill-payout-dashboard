#!/usr/bin/env python3
"""Decision-first page gates (council loop 1, 2026-09-27).

  1. Month-by-month card closes: for every month payouts + swaps/ops == out
     and refills + returns == in; the months sum to the all-history window;
     each CLOSED month equals the sum of its days in facts.daily (days are
     sealed by the digest ledger, so a closed month cannot move either).
  2. First-screen order: exec summary -> lifetime sentence -> tiles ->
     month-by-month card -> daily flows, and top creator wallets sit below
     in "Understand", not in "Now".
  3. Wording lint: inflow is our own money ("refills" + "returns") — never
     "new"/"external" funding, and "top-up" is reserved for Stripe packs
     delivered OUT. Self-tests on a seeded bad phrase so the lint cannot
     silently rot into a no-op.

  python3 tests/test_page.py      (offline, <1 s)
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CENT = 0.011

# inflow phrasings the owner ruled out (2026-09-27: all inbound refills are
# ours; "top-ups" names Stripe packs delivered to users, not money in)
BANNED = [r"\bnew funding\b", r"\bexternal funding\b", r"\bmanual funding\b",
          r"\bnew money\b", r"\btreasury top-ups?\b", r"\btop-ups? in\b",
          r"In: top-ups", r"\btop-up funding\b", r"money entering from outside",
          r"\bdeliberate top-ups?\b", r"top-ups? of this wallet"]
# public surfaces the lint reads (data.json carries the rendered prose)
LINT_TARGETS = ["template.html", "data.json", "template_public.html"]


def lint(text):
    return [p for p in BANNED if re.search(p, text, re.I)]


def main():
    D = json.load(open(os.path.join(ROOT, "data.json")))
    F = D["facts"]
    M = F["monthly"]
    allw = F["windows"][3]
    assert allw["label"] == "all history", allw["label"]

    # 1. closure
    for m in M:
        assert abs(m["economy_out_usd"] + m["ops_out_usd"] - m["out_usd"]) <= CENT, \
            f"{m['label']}: payouts + swaps/ops != out"
        assert abs(m["in_external_usd"] + m["in_recycled_usd"] - m["in_usd"]) <= CENT, \
            f"{m['label']}: refills + returns != in"
    tol = CENT * len(M)
    for k in ("out_usd", "economy_out_usd", "ops_out_usd", "in_usd", "in_external_usd", "in_recycled_usd"):
        s = sum(m[k] for m in M)
        assert abs(s - allw[k]) <= tol, f"months sum {k} {s:.2f} != all-history {allw[k]:.2f}"
    by_month = {}
    for d in F["daily"]:
        by_month.setdefault(d["d"][:7], []).append(d)
    closed = 0
    for m in M:
        if "partial" in m["label"]:
            continue
        days = by_month.get(m["label"][:7], [])
        s = sum(d["out_usd"] for d in days)
        assert abs(s - m["out_usd"]) <= CENT * max(len(days), 1), \
            f"{m['label']}: month out {m['out_usd']} != sum of its days {s:.2f}"
        closed += 1
    print(f"ok month-by-month closes: {len(M)} months, {closed} closed months == sum of sealed days, "
          f"months sum to all-history on every column")

    # 2. first-screen order
    t = open(os.path.join(ROOT, "template.html")).read()
    order = ['id="execSummary"', 'id="plainStrip"', 'id="ftiles"', 'id="trendCard"',
             "<h2>Daily flows</h2>", "Understand —", 'id="creatorCard"']
    pos = [t.find(o) for o in order]
    assert all(p >= 0 for p in pos), f"missing anchor: {[o for o, p in zip(order, pos) if p < 0]}"
    assert pos == sorted(pos), "first-screen order drifted: " + " -> ".join(o for _, o in sorted(zip(pos, order)))
    print("ok first-screen order: summary -> sentence -> tiles -> month-by-month -> daily; creator wallets in Understand")

    # 3. wording lint (+ self-test)
    assert lint("we received $5 of new funding") and lint("Top-ups in"), "wording lint self-test failed"
    assert not lint("refills in · returns in · top-ups delivered · system free top-ups"), "lint over-matches"
    bad = {rel: lint(open(os.path.join(ROOT, rel)).read()) for rel in LINT_TARGETS}
    bad = {k: v for k, v in bad.items() if v}
    assert not bad, f"banned inflow wording on a public surface: {bad}"
    print(f"ok wording lint: {len(BANNED)} banned inflow phrasings absent from {', '.join(LINT_TARGETS)} (self-test passed)")
    print("test_page: PASS")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Figure-parity gate (council loop 2, 2026-09-27): a page change must not
move a single rendered figure unless it means to.

Renders the page from template.html at a BASE revision (default origin/main)
and from the working template.html, on the SAME data.json with the same
pinned clock (scope.generated_iso + 5 min), under tests/domshim.js — and
asserts the multiset of digit-bearing text is identical: every static text
node, every text node the scripts wrote, and every tooltip (title /
data-tip / data-tiphtml) text node that contains a digit. Relative ages
("N min ago", "Data is N hours old") and timestamps are masked.

No baseline file is committed — data.json changes four times an hour, so a
stored baseline would be stale by the next cron run. The comparison is
template-vs-template on today's data.

A change that is SUPPOSED to move figures (a new column, a reworded
number) states that in its PR and runs with FIGURE_PARITY_ALLOW=1, which
prints the diff and exits 0.

  python3 tests/test_figure_parity.py [BASE_REF]      (default origin/main)
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pagehost as P  # noqa: E402


def base_template(ref):
    r = subprocess.run(["git", "-C", P.ROOT, "show", f"{ref}:template.html"],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def main():
    if not P.have_node():
        if os.environ.get("CI"):
            print("FAIL: node not available in CI — figure parity cannot run")
            sys.exit(1)
        print("SKIP: node not available — figure parity not enforced this run")
        sys.exit(0)
    ref = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    base = base_template(ref)
    if base is None:
        msg = f"base ref {ref!r} not available (shallow checkout? run `git fetch origin main`)"
        if os.environ.get("CI"):
            print("FAIL:", msg)
            sys.exit(1)
        print("SKIP:", msg)
        sys.exit(0)
    work = open(os.path.join(P.ROOT, "template.html")).read()
    data = open(os.path.join(P.ROOT, "data.json")).read()
    now = P.generated_ms(data) + 5 * 60000
    pages = [P.build_from_template(base), P.build_from_template(work)]
    res = [P.run(pg, data, [{"now": now}])[0] for pg in pages]
    for name, r in zip((ref, "working"), res):
        assert not r["uncaught"], f"{name} template raised on real data: {r['uncaught']}"
    a, b = (P.digit_multiset(pg, r) for pg, r in zip(pages, res))
    only_a, only_b = a - b, b - a
    print(f"{ref}: {sum(a.values())} digit-bearing text nodes · working: {sum(b.values())}")
    if not only_a and not only_b:
        print("test_figure_parity: PASS (identical digit-bearing text)")
        return
    for (kind, t), n in sorted(only_a.items()):
        print(f"  - [{kind}] x{n}: {t[:220]}")
    for (kind, t), n in sorted(only_b.items()):
        print(f"  + [{kind}] x{n}: {t[:220]}")
    if os.environ.get("FIGURE_PARITY_ALLOW") == "1":
        print("test_figure_parity: DIFF ALLOWED (FIGURE_PARITY_ALLOW=1)")
        return
    print(f"test_figure_parity: FAIL ({len(only_a)} removed, {len(only_b)} added)")
    sys.exit(1)


if __name__ == "__main__":
    main()

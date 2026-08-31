#!/usr/bin/env python3
"""The coupon crawl must agree, row for row, with the private coupon ledger.

Same argument as tests/test_reconcile.py: this repo reaches the Coupon
Distributor through Blockscout v2 (with an eth_getLogs fallback); the private
ledger reaches the same wallet through its own independent crawler. A hole in
either is invisible from inside the crawler that has it.

BOTH directions are compared: the private ledger holds every transfer touching
the wallet, so its rows are split by sender — from the wallet = a claim
(coupon_out/), to the wallet = an inflow (coupon_in/), including the bridge
mint from the zero address.

  COUPON_LEDGER_PATH=~/Documents/moca-ledger-private/data_wallets/coupon \
      python3 tests/test_coupon_reconcile.py

Skips clean (exit 0) when the path is unset or absent — CI has no access to
the private repo, a laptop does.
"""
import os, sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import rows as R

# 3 recent closed days per direction, per the spec. The current UTC day is
# excluded entirely: the two crawlers lag differently inside a day, so a
# disagreement there says nothing about correctness.
N_DAYS = 3


def _day(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d")


def _by_day(it, today):
    out = {}
    for r in it:
        d = _day(r["ts_epoch"])
        if d < today:
            out.setdefault(d, set()).add((r["tx_hash"].lower(), r["log_index"],
                                          r["value_wei"]))
    return out


def main():
    root = os.environ.get("COUPON_LEDGER_PATH")
    if not root:
        print("SKIP: COUPON_LEDGER_PATH unset — nothing to reconcile against")
        return 0
    data_dir = os.path.expanduser(root)
    if not os.path.isdir(data_dir):
        print(f"SKIP: {data_dir} is not a directory — nothing to reconcile against")
        return 0

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    theirs_all = list(R.canonical_rows("coupon_ledger", path=data_dir))
    if not theirs_all:
        print("SKIP: the private coupon ledger has no rows")
        return 0

    fails = checked = 0
    for direction, source, pick in (
            ("out", "coupon_out", lambda r: r["from_addr"] == R.COUPON),
            ("in", "coupon_in", lambda r: r["to_addr"] == R.COUPON)):
        ours = _by_day(R.canonical_rows(source), today)
        theirs = _by_day((r for r in theirs_all if pick(r)), today)
        if not ours or not theirs:
            print(f"SKIP {direction}: one source has no closed day at all")
            continue
        # UNION over the overlap of the two COVERED RANGES, never the
        # intersection of the days each happens to have rows for — a whole day
        # missing on one side is exactly what this test exists to catch, and
        # intersecting would drop it silently.
        lo, hi = max(min(ours), min(theirs)), min(max(ours), max(theirs))
        covered = sorted(d for d in set(ours) | set(theirs) if lo <= d <= hi)
        if not covered:
            print(f"SKIP {direction}: the two sources' covered ranges do not overlap")
            continue
        days = covered[-N_DAYS:]
        print(f"coupon_{direction}: reconciling {len(days)} closed day(s): {', '.join(days)}")
        for d in days:
            a, b = ours.get(d, set()), theirs.get(d, set())
            checked += 1
            if a != b:
                fails += 1
                print(f"FAIL {direction} {d}: dashboard={len(a)} ledger={len(b)} "
                      f"| only here: {len(a - b)} | only there: {len(b - a)}")
                for t in list(a - b)[:5]:
                    print("   only in dashboard:", t)
                for t in list(b - a)[:5]:
                    print("   only in coupon ledger:", t)
            else:
                print(f"ok {direction} {d}: {len(a)} row(s) identical (tx_hash, log_index, value_wei)")
    assert checked, "no closed day was compared in either direction"
    assert fails == 0, f"{fails} of {checked} day(s) disagree between the two crawlers"
    print("test_coupon_reconcile: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Two independent crawlers, one chain: they must agree row for row.

This repo reaches Base through Blockscout's v2 API; Agentic-Po/moca-ledger
reaches it through raw eth_getLogs. A silent gap in either (the v2 recovery
holes that forced the 24h cross-check, a stalled crawl window) is invisible
from inside the crawler that has it. Comparing them is the only check that
sees it.

Direction-aware on purpose: moca-ledger holds EVERY MOCA transfer, so it is
only comparable to this repo's outbound ledger once filtered to rows whose
sender is the treasury.

  MOCA_LEDGER_PATH=~/Documents/moca-ledger python3 tests/test_reconcile.py

Skips clean (exit 0) when the clone is absent — CI clones it, a laptop may not.
"""
import os, sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import rows as R

# Matches the weekly cron: with N_DAYS=3 on a Monday job, days 4-7 of every
# week were never reconciled at all, so a gap only had to survive three days
# to escape permanently.
N_DAYS = 7


def _day(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d")


def main():
    root = os.environ.get("MOCA_LEDGER_PATH")
    if not root:
        print("SKIP: MOCA_LEDGER_PATH unset — nothing to reconcile against")
        return 0
    data_dir = os.path.join(os.path.expanduser(root), "data")
    if not os.path.isdir(data_dir):
        print(f"SKIP: {data_dir} is not a directory — nothing to reconcile against")
        return 0

    # The current UTC day is excluded ENTIRELY: the two crawlers lag
    # differently inside a day (10-min cron vs 15-min refresh), so a
    # disagreement there is expected and says nothing about correctness.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    ours = {}
    for r in R.canonical_rows("treasury_out"):
        if r["token"] != "MOCA":
            continue
        d = _day(r["ts_epoch"])
        if d < today:
            ours.setdefault(d, set()).add((r["tx_hash"].lower(), r["log_index"], r["value_wei"]))

    theirs = {}
    for r in R.canonical_rows("moca_ledger", path=data_dir):
        if r["from_addr"] != R.TREASURY:
            continue
        d = _day(r["ts_epoch"])
        if d < today:
            theirs.setdefault(d, set()).add((r["tx_hash"].lower(), r["log_index"], r["value_wei"]))

    # UNION over the overlap of the two COVERED RANGES, not the intersection
    # of the days each source happens to have rows for. A day where one
    # crawler produced ZERO rows has no key in its dict; intersecting drops
    # that day silently — which is precisely the whole-day recovery hole this
    # test exists to catch. Inside the overlapping range, a missing day is an
    # EMPTY set and therefore a loud FAIL.
    if not ours or not theirs:
        print("SKIP: one source has no closed day at all")
        return 0
    lo = max(min(ours), min(theirs))
    hi = min(max(ours), max(theirs))
    covered = sorted(d for d in set(ours) | set(theirs) if lo <= d <= hi)
    if not covered:
        print("SKIP: the two sources' covered ranges do not overlap")
        return 0
    days = covered[-N_DAYS:]
    print(f"reconciling {len(days)} closed day(s): {', '.join(days)}")

    fails = 0
    for d in days:
        a, b = ours.get(d, set()), theirs.get(d, set())
        only_ours, only_theirs = a - b, b - a
        if only_ours or only_theirs:
            fails += 1
            print(f"FAIL {d}: dashboard={len(a)} moca-ledger={len(b)} "
                  f"| only here: {len(only_ours)} | only there: {len(only_theirs)}")
            for t in list(only_ours)[:5]:
                print("   only in dashboard:", t)
            for t in list(only_theirs)[:5]:
                print("   only in moca-ledger:", t)
        else:
            print(f"ok {d}: {len(a)} rows identical (tx_hash, log_index, value_wei)")
    assert fails == 0, f"{fails} of {len(days)} day(s) disagree between the two crawlers"
    print("test_reconcile: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

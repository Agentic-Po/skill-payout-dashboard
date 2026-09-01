#!/usr/bin/env python3
"""A live quote that disagrees with the market close is stale, not a price move.

2026-09-01: Blockscout's exchange_rate for MOCA sat frozen at 0.0077424 for
12+ hours while the market traded ~0.00845. It passed the 5x anchor band, so
every run that reached Blockscout published a balance ~$3-4K below every run
that fell through to DexScreener — the same wallet, the same MOCA, two
different headline numbers within the hour.
"""
import re, sys, os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) if os.path.basename(os.path.dirname(os.path.abspath(__file__))) == "tests" else os.getcwd()
src = open(os.path.join(ROOT, "refresh.py")).read()

assert re.search(r"^MARKET_AGREE\s*=\s*0\.0\d", src, re.M), \
    "MARKET_AGREE band missing from refresh.py"
assert "rate rejected as stale" in src, \
    "blockscout stale-quote rejection missing"
# the gate must sit INSIDE the blockscout branch, before the accept
i_gate = src.index("rate rejected as stale")
i_accept = src.index('RATE[sym], RATE_SRC[sym] = r, "blockscout"')
assert i_gate < i_accept, "stale gate must run before the blockscout accept"
band = float(re.search(r"^MARKET_AGREE\s*=\s*([\d.]+)", src, re.M).group(1))
assert 0.02 <= band <= 0.10, f"MARKET_AGREE {band} outside a sane 2-10% window"

# and the published rate must agree with the latest market close
import json
D = json.load(open(os.path.join(ROOT, "data.json")))
S = json.load(open(os.path.join(ROOT, "day_rates.json")))
for sym, live in D["facts"]["rate"].items():
    mrs = (S.get("market_rates") or {}).get(sym) or {}
    if not mrs or not live:
        continue
    mkt = mrs[max(mrs)]
    assert mkt * (1 - band) < live < mkt * (1 + band), \
        f"published {sym} rate {live} disagrees with market close {mkt} by more than {band:.0%}"
    print(f"ok {sym} live {live} within {band:.0%} of market close {mkt}")
print("test_rate_stale: PASS")

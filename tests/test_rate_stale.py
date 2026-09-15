#!/usr/bin/env python3
"""A live quote that disagrees with the market print is stale, not a price move
— and, since 2026-09-14, no day may be priced by the implied leg.

2026-09-01: Blockscout's exchange_rate for MOCA sat frozen at 0.0077424 for
12+ hours while the market traded ~0.00845. It passed the 5x anchor band, so
every run that reached Blockscout published a balance ~$3-4K below every run
that fell through to DexScreener — the same wallet, the same MOCA, two
different headline numbers within the hour.

2026-09-14: the implied oracle read the new $0.05 equips as $0.10 invokes and
persisted MOCA at exactly 2x the market close. The implied leg is retired as
a pricer from IMPLIED_NEEDS_MARKET_FROM. Checks (a)-(d):
  (a) no market_rates day >= today (closed candles only; today's running
      candle lives in market_open)
  (b) a synthetic >=5-row cluster at 2x market on a day >= the retirement
      date never prices "implied" (refresh._price_day, extracted by ast)
  (c) open_day_rate is absent, or stamped market-open within x1.10 of the
      last known day rate
  (d) no "implied" stamp on any day >= the retirement date
"""
import ast, json, re, sys, os
from datetime import datetime, timezone
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

# and the published rate must agree with the latest market print: today's
# running candle (market_open) when it is today's, else the last close
D = json.load(open(os.path.join(ROOT, "data.json")))
S = json.load(open(os.path.join(ROOT, "day_rates.json")))
today = D["scope"]["generated_iso"][:10]
for sym, live in D["facts"]["rate"].items():
    mrs = (S.get("market_rates") or {}).get(sym) or {}
    mo = (S.get("market_open") or {}).get(sym) or {}
    if not live:
        continue
    if mo.get("d") == today and mo.get("close"):
        mkt, lab = mo["close"], f"market_open {mo['d']}"
    elif mrs:
        mkt, lab = mrs[max(mrs)], f"market close {max(mrs)}"
    else:
        continue
    assert mkt * (1 - band) < live < mkt * (1 + band), \
        f"published {sym} rate {live} disagrees with {lab} {mkt} by more than {band:.0%}"
    print(f"ok {sym} live {live} within {band:.0%} of {lab} {mkt}")

FROM = re.search(r'^IMPLIED_NEEDS_MARKET_FROM\s*=\s*"([\d-]+)"', src, re.M).group(1)
# (a) closed candles only
for sym, mrs in (S.get("market_rates") or {}).items():
    late = [d for d in mrs if d >= today]
    assert not late, f"{sym} market_rates carries non-closed day(s) {late} (today {today})"
print(f"ok (a) market_rates holds closed days only (max {max((max(m) for m in S['market_rates'].values() if m), default='-')} < {today})")

# (b) the pricing decision, extracted from refresh.py by ast and executed on a
# synthetic day: 5 rows that back-solve to exactly 2x the market close must
# NOT price "implied" on/after the retirement date (and must "market" when
# a banded close exists)
tree = ast.parse(src)
fns = {n.name: ast.get_source_segment(src, n) for n in ast.walk(tree)
       if isinstance(n, ast.FunctionDef) and n.name in ("_price_day", "_market_band")}
assert set(fns) == {"_price_day", "_market_band"}, f"refresh.py lost {set(fns) ^ {'_price_day', '_market_band'}}"
ns = {"datetime": datetime, "statistics": __import__("statistics"),
      "IMPLIED_NEEDS_MARKET_FROM": FROM, "MARKET_AGREE": band}
for const in ("MARKET_DRIFT_PER_DAY", "MARKET_BAND_CAP", "MARKET_MAX_GAP_DAYS"):
    ns[const] = float(re.search(rf"^{const}\s*=\s*([\d.]+)", src, re.M).group(1))
exec(fns["_market_band"], ns)
exec(fns["_price_day"], ns)
_price_day = ns["_price_day"]
mkt_close = 0.00924424
persisted = {"2026-09-13": 0.0088675}
# 12 equips of 5.4 MOCA (= $0.05 at the close) read as $0.10 rows imply 2x
vals = [0.05 / mkt_close] * 12
kind, rate = _price_day("2026-09-14", vals, mkt_close * 2, mkt_close, persisted, "2026-09-15")
assert kind == "market" and abs(rate - mkt_close) < 1e-12, f"2x cluster priced {kind} {rate}"
kind, rate = _price_day("2026-09-20", vals, mkt_close * 2, None, persisted, "2026-09-21")
assert kind == "carry-forward" and rate is None, f"no-close day priced {kind} {rate}"
# ... while a day BEFORE the retirement still walks the implied history leg
kind, rate = _price_day("2026-08-01", [0.10 / 0.0085] * 6, 0.0085, None, {"2026-07-31": 0.0085}, "2026-09-15")
assert kind == "implied", f"history day lost the implied leg: {kind}"
# open day: market-open inside x1.10 of the nearest known day, else carry-forward
kind, rate = _price_day("2026-09-15", [], 0.009, None, {"2026-09-14": mkt_close}, "2026-09-15", mkt_close * 1.05)
assert kind == "market-open", kind
kind, rate = _price_day("2026-09-15", [], 0.009, None, {"2026-09-14": mkt_close}, "2026-09-15", mkt_close * 1.2)
assert kind == "carry-forward", kind
print("ok (b) synthetic 2x cluster on/after the retirement never prices implied; history leg intact; open-day rule banded")

# (c) + (d) on the committed state
for sym, days in S["day_rates"].items():
    srcs = (S.get("day_rate_src") or {}).get(sym) or {}
    mrs = (S.get("market_rates") or {}).get(sym) or {}
    for d, v in days.items():
        if d >= FROM:
            assert srcs.get(d) != "implied", f"{sym} {d} is implied-priced after {FROM}"
            if mrs.get(d):
                assert abs(v / mrs[d] - 1) <= band, \
                    f"{sym} {d} day rate {v} disagrees with market close {mrs[d]} (2x-oracle class)"
    od = (S.get("open_day_rate") or {}).get(sym)
    if od:
        assert srcs.get(od["d"]) == "market-open", f"{sym} open-day rate {od} is not stamped market-open"
        prior = [k for k in sorted(days) if k < od["d"]]
        assert prior and days[prior[-1]] / 1.10 < od["rate"] < days[prior[-1]] * 1.10, \
            f"{sym} open-day rate {od} outside x1.10 of last known day {prior[-1:]}"
print(f"ok (c)(d) no implied day rate from {FROM}; open-day rate absent or market-open within x1.10")
print("test_rate_stale: PASS")

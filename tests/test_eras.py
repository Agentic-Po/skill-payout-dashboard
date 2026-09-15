#!/usr/bin/env python3
"""Era-aware classifier gate (Creator Rewards v2, 2026-09-15). Offline, < 10 s.

  1. Fixture rows per era: the ROW timestamp selects the grid, never the run
     date. Both boundaries are exercised to the minute.
  2. band() per fixture: b0005/b005 exist only from the v2 resume; pre-v2
     sub-$0.06 dust stays micro; b010/b1 are kept forever.
  3. HEAD-classifier parity: every row before the v1 close (2026-08-21T13:55Z)
     classifies byte-identically to the frozen v1 algorithm below, and the
     all-time usd_ce over those rows is FROZEN to the cent — the proof that
     the 08-21 $1 farm was not relabelled.
  4. Golden era-switch windows from the real shards, priced at the banked
     2026-09-14 market close: pre-cap 14:19Z-19:12Z and cap-on 19:12Z-03:12Z.
     Frozen from the classifier's first run (council rule: within ±2 of the
     brief, freeze the classifier's own numbers, report the delta).
  5. Legacy $1 top-ups: 91 rows / $90.60 / 91 wallets through the freeze
     instant, first seen 22 Aug; zero "invoke (retired)" rows exist.

  python3 tests/test_eras.py
"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import shards
from classify import (classify_usd, band, pin_rate, era_for, ERAS, PAUSED_UTC, RESUMED_UTC,
                      CAP_ON_UTC, LEGACY, RETIRED, BAND_KEYS)

# ---- frozen goldens (2026-09-15, shards complete through 2026-09-15T04:06Z) ----
CLOSE_0914 = 0.00924424                     # banked market close, day_rates.json
FREEZE = "2026-09-15T03:07:00"              # legacy-ledger freeze instant
PRE_PAUSE_USD_CE = 33054.36                 # all-time creator earnings, rows < PAUSED_UTC
PRE_PAUSE_CE_ROWS = 80692
GOLDEN = {
    # brief said 295/234/61/27 (14:21 boundary); adversary 297/236/62. Classifier: 235 equips.
    "pre_cap": {"tx": 297, "equip": 235, "invoke": 62, "usd": 12.06, "creators": 27},
    # brief said 321/221/100/$11.47 from shards ending 03:07Z — five minutes short
    # of the window's 03:12Z end; the complete window is +7 equips.
    "cap_on": {"tx": 328, "equip": 228, "invoke": 100, "usd": 11.78, "creators": 35},
}
LEGACY_GOLDEN = {"n": 91, "usd": 90.60, "wallets": 91, "first": "2026-08-22"}

FIXTURES = [
    # (usd, ts, coarse, fine, tier, band)
    (1.00, "2026-08-15T10:00:00", "equip", "equip", None, "b1"),
    (0.10, "2026-08-15T10:00:00", "invoke", "invoke", None, "b010"),
    (0.05, "2026-08-15T10:00:00", "micro", "test", None, "micro"),         # pre-v2 dust
    (1.00, "2026-08-21T13:54:59", "equip", "equip", None, "b1"),           # last v1 minute
    (1.00, "2026-08-21T13:55:00", "growth", "$1 top-up (legacy)", 1, "b1"),  # first pause minute
    (0.10, "2026-08-21T13:55:00", "invoke", "invoke (retired)", None, "b010"),
    (1.00, "2026-09-01T00:00:00", "growth", "$1 top-up (legacy)", 1, "b1"),
    (0.10, "2026-09-01T00:00:00", "invoke", "invoke (retired)", None, "b010"),
    (0.0503, "2026-09-14T14:18:59", "micro", "test", None, "micro"),      # one minute early: still pause
    (0.0503, "2026-09-14T14:19:00", "equip", "equip", None, "b005"),       # first v2 minute
    (0.05, "2026-09-15T00:00:00", "equip", "equip", None, "b005"),
    (0.005, "2026-09-15T00:00:00", "invoke", "invoke", None, "b0005"),
    (0.0044, "2026-09-15T00:00:00", "invoke", "invoke", None, "b0005"),    # -12% edge
    (0.0056, "2026-09-15T00:00:00", "invoke", "invoke", None, "b0005"),    # +12% edge
    (0.002, "2026-09-15T00:00:00", "micro", "test", None, "micro"),
    (0.10, "2026-09-15T00:00:00", "invoke", "invoke (retired)", None, "b010"),
    (1.00, "2026-09-15T00:00:00", "growth", "$1 top-up (legacy)", 1, "b1"),
    (3.00, "2026-08-15T00:00:00", "growth", "$3 credit", 3, "b3"),
    (3.00, "2026-09-15T00:00:00", "growth", "$3 credit", 3, "b3"),
    (9.40, "2026-09-15T00:00:00", "growth", "stripe $10", 10, "b10"),
    (0.30, "2026-09-15T00:00:00", "nonstandard", "nonstandard (small)", None, "other"),
    (25.0, "2026-08-15T00:00:00", "growth", "stripe $25", 25, "b25"),        # 25/0.94 inside ±15%
    (7.0, "2026-08-15T00:00:00", "nonstandard", "nonstandard (large)", None, "other"),
    (7.0, "2026-09-15T00:00:00", "nonstandard", "nonstandard (large)", None, "other"),
]


# frozen v1 reference — the classifier as it stood at HEAD before eras
def _v1_reference(usd):
    def snap(v, pts, tol):
        for p in pts:
            if abs(v - p) / p <= tol:
                return p
        return None
    if usd < 0.06:
        return "micro", "test", None
    if snap(usd, (0.10,), 0.08) is not None:
        return "invoke", "invoke", None
    if snap(usd, (1,), 0.08) is not None:
        return "equip", "equip", None
    for amt, fine in ((3, "$3 credit"), (5, "referral $5")):
        if snap(usd, (amt,), 0.08) is not None:
            return "growth", fine, amt
    p = snap(usd / 0.94, (10, 20, 25, 50, 100), 0.15)
    if p is not None:
        return "growth", f"stripe ${p}", p
    return "nonstandard", ("nonstandard (small)" if usd < 0.5 else "nonstandard (large)"), None


def _rows():
    D = json.load(open(os.path.join(ROOT, "data.json")))
    dr = json.load(open(os.path.join(ROOT, "day_rates.json")))
    toks = {a.lower(): s for s, a in D["scope"]["tokens"].items()}
    rates = {s: dict(dr["day_rates"].get(s, {})) for s in toks.values()}
    assert rates["MOCA"].get("2026-09-14") == CLOSE_0914, \
        f"MOCA 2026-09-14 day rate {rates['MOCA'].get('2026-09-14')} != banked close {CLOSE_0914}"

    def pin(sym, day):
        # closed days never reprice; anything on/after the freeze day is priced
        # at the 09-14 close so the goldens are independent of the open day
        if day in rates[sym] and day < "2026-09-15":
            return rates[sym][day]
        if sym == "MOCA" and day >= "2026-09-14":
            return CLOSE_0914
        return pin_rate(rates[sym], day, D["facts"]["rate"].get(sym) or 0)
    out = []
    for i in shards.load(os.path.join(ROOT, "transfers")):
        s = toks.get(i["token"]["address_hash"].lower())
        if not s:
            continue
        ts = i["timestamp"][:19]
        usd = int(i["total"]["value"]) / 10 ** int(i["total"].get("decimals") or 18) * pin(s, ts[:10])
        c, f, t = classify_usd(usd, ts)
        out.append((ts, s, usd, c, f, t, i["to"]["hash"].lower()))
    return out


def main():
    # 0. the era table itself
    assert [e["name"] for e in ERAS] == ["v1", "pause", "v2"]
    assert PAUSED_UTC == "2026-08-21T13:55" and RESUMED_UTC == "2026-09-14T14:19" and CAP_ON_UTC == "2026-09-14T19:12"
    assert list(ERAS[2]["rewards"]) == ["invoke", "equip"], "v2 rewards must list invoke before equip"
    assert era_for("2026-08-21T13:54:59")["name"] == "v1" and era_for("2026-08-21T13:55:00")["name"] == "pause"
    assert era_for("2026-09-14T14:18:59")["name"] == "pause" and era_for("2026-09-14T14:19:00")["name"] == "v2"
    assert RETIRED == {"invoke_v1": {"cutoff": PAUSED_UTC, "point": 0.10, "tol": 0.15}}
    assert LEGACY["topup1"]["since"] == PAUSED_UTC and LEGACY["topup1"]["max_per_wallet"] == 1
    assert BAND_KEYS[:5] == ["micro", "b0005", "b005", "b010", "b1"] and len(BAND_KEYS) == 13
    print("ok era table: v1 | pause 2026-08-21T13:55 | v2 2026-09-14T14:19 · cap 19:12 · 13 band keys")

    # 1 + 2. fixtures
    for usd, ts, c, f, t, b in FIXTURES:
        got = classify_usd(usd, ts)
        assert got == (c, f, t), f"classify_usd({usd}, {ts}) = {got}, want {(c, f, t)}"
        gb = band(usd, ts)
        assert gb == b, f"band({usd}, {ts}) = {gb}, want {b}"
    try:
        classify_usd(1.0)
        raise AssertionError("classify_usd accepted a day-blind call")
    except TypeError:
        pass
    print(f"ok {len(FIXTURES)} fixture rows classify + band by ROW timestamp; day-blind call is a TypeError")

    rows = _rows()
    # 3. HEAD parity before the v1 close, to the cent
    pre = [r for r in rows if r[0] < PAUSED_UTC]
    diffs = [r for r in pre if _v1_reference(r[2]) != (r[3], r[4], r[5])]
    assert not diffs, f"{len(diffs)} pre-pause rows differ from the v1 reference, e.g. {diffs[:3]}"
    ce = round(sum(r[2] for r in pre if r[3] in ("invoke", "equip")), 2)
    n_ce = sum(1 for r in pre if r[3] in ("invoke", "equip"))
    assert abs(ce - PRE_PAUSE_USD_CE) < 0.005 and n_ce == PRE_PAUSE_CE_ROWS, \
        f"pre-pause usd_ce ${ce:,.2f} / {n_ce} rows != frozen ${PRE_PAUSE_USD_CE:,.2f} / {PRE_PAUSE_CE_ROWS}"
    farm = sum(1 for r in pre if r[0][:10] == "2026-08-21" and r[4] == "equip")
    assert farm == 17053, f"08-21 pre-13:55 equips {farm} != 17053 (the farm must stay creator rewards)"
    print(f"ok pre-pause parity: {len(pre):,} rows identical to the v1 reference · usd_ce ${ce:,.2f} ({n_ce:,} rows) · 08-21 farm 17,053 equips intact")

    # 4. golden windows
    def win(a, b):
        rs = [r for r in rows if a <= r[0] < b and r[4] in ("equip", "invoke")]
        return {"tx": len(rs), "equip": sum(1 for r in rs if r[4] == "equip"),
                "invoke": sum(1 for r in rs if r[4] == "invoke"),
                "usd": round(sum(r[2] for r in rs), 2), "creators": len({r[6] for r in rs})}
    for name, (a, b) in {"pre_cap": (RESUMED_UTC, CAP_ON_UTC), "cap_on": (CAP_ON_UTC, "2026-09-15T03:12")}.items():
        got = win(a, b)
        for k, want in GOLDEN[name].items():
            tol = 0.01 if k == "usd" else 0
            assert abs(got[k] - want) <= tol, f"{name}.{k}: {got[k]} != golden {want} (window {got})"
        print(f"ok golden {name}: {got}")

    # 5. legacy + retired
    leg = [r for r in rows if r[4] == LEGACY["topup1"]["fine"] and r[0] <= FREEZE]
    assert len(leg) == LEGACY_GOLDEN["n"] and abs(round(sum(r[2] for r in leg), 2) - LEGACY_GOLDEN["usd"]) < 0.005
    assert len({r[6] for r in leg}) == LEGACY_GOLDEN["wallets"] and min(r[0] for r in leg)[:10] == LEGACY_GOLDEN["first"]
    assert all(r[3] == "growth" for r in leg)
    assert not [r for r in rows if r[4] == "invoke (retired)"], "an 'invoke (retired)' row exists — the tripwire should have fired"
    pause = [r for r in rows if PAUSED_UTC <= r[0] < RESUMED_UTC]
    assert not [r for r in pause if r[3] in ("invoke", "equip")], "a creator-reward class inside the pause"
    print(f"ok legacy $1 top-ups through {FREEZE}: {len(leg)} rows · ${sum(r[2] for r in leg):,.2f} · "
          f"{len({r[6] for r in leg})} wallets · first {LEGACY_GOLDEN['first']} · 0 retired invokes · pause era carries no reward class")
    print("test_eras: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

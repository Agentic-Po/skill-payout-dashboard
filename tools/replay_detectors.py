#!/usr/bin/env python3
"""Replay the CURRENT detector rules over the August 2026 reward farm.

Evidence, not tuning (council loop 1, item 5, 2026-09-21). The question is
plain: had cap_detect.py's C1-C6 and alerts.py's Tukey outflow fence existed
on 2026-08-21 — the $1-equip era, 17,053 equip-sized payouts in one UTC day,
~$22.9K over ~60 h — would they have fired, when, and on what?

Units conversion (stated, not hidden): the detectors count REWARD UNITS
(equip = 1, invoke = 0.1, cap = 80 units/creator/clock-hour). classify.py
sizes every row on its OWN era's grid, so a v1 $1.00 equip classifies as
`equip` = 1 unit and a v1 $0.10 invoke as `invoke` = 0.1 unit — the era's
reward unit, exactly as a v2 $0.05 equip is 1 unit today. Nothing is
rescaled by price: 80 units was $80/h in August and is $4/h now.

Era gate (stated, not hidden): as shipped, cap_detect evaluates only rows
at/after RESUMED_UTC (2026-09-14) and clock-hours from CAP_HOUR0 — before
that no cap existed to breach, so the SHIPPED code would not have looked at
August at all. This replay lifts that gate (the three module constants are
moved to the epoch) and runs the RULES unchanged. Both answers are printed.

Reads the committed shards + day_rates.json + data.json only. Prints. Writes
NOTHING — no state, no artifact, public or private.

  python3 tools/replay_detectors.py [--from 2026-08-19] [--to 2026-08-24]
"""
import json, os, statistics, sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import shards                                   # noqa: E402
import cap_detect as cd                         # noqa: E402
from classify import classify_usd, pin_rate, era_for, UNITS, CAP_UNITS  # noqa: E402


def _arg(flag, default):
    a = sys.argv[1:]
    return a[a.index(flag) + 1] if flag in a else default


def load_rows(lo, hi):
    """(ts_dt, usd_pinned, to_lower, ts_iso, fine, tok) for treasury OUT rows in [lo, hi)."""
    D = json.load(open(os.path.join(ROOT, "data.json")))
    dr = json.load(open(os.path.join(ROOT, "day_rates.json")))
    toks = {a.lower(): s for s, a in D["scope"]["tokens"].items()}
    rates = {s: dict(dr["day_rates"].get(s, {})) for s in toks.values()}
    out, all_hist = [], []
    for i in shards.load(os.path.join(ROOT, "transfers")):
        s = toks.get(i["token"]["address_hash"].lower())
        if not s:
            continue
        ts = i["timestamp"][:19]
        usd = int(i["total"]["value"]) / 1e18 * pin_rate(rates[s], ts[:10], D["facts"]["rate"][s])
        all_hist.append((datetime.fromisoformat(ts), usd))
        if not (lo <= ts < hi):
            continue
        _c, fine, _t = classify_usd(usd, ts)
        out.append((datetime.fromisoformat(ts), usd, i["to"]["hash"].lower(), ts, fine, s))
    out.sort(key=lambda r: r[0])
    return out, all_hist


def tukey_fence(all_hist, upto):
    """alerts.py's rule, restated from its definition: median + 3 x IQR of the
    zero-filled hourly day-pinned USD outflow series over the FULL history
    up to `upto` (the baseline the run at that hour would have had)."""
    bucket = {}
    for ts, usd in all_hist:
        if ts >= upto:
            continue
        h = ts.replace(minute=0, second=0, microsecond=0)
        bucket[h] = bucket.get(h, 0.0) + usd
    if not bucket:
        return None
    h0, h1 = min(bucket), upto.replace(minute=0, second=0, microsecond=0)
    series, h = [], h0
    while h <= h1:
        series.append(bucket.get(h, 0.0))
        h += timedelta(hours=1)
    series.sort()
    n = len(series)
    q1, q3 = series[n // 4], series[(3 * n) // 4]
    return {"median": statistics.median(series), "iqr": q3 - q1, "hours": n}


def main():
    lo, hi = _arg("--from", "2026-08-19"), _arg("--to", "2026-08-24")
    rows, all_hist = load_rows(lo, hi)
    print(f"REPLAY: {len(rows):,} treasury OUT rows {lo} .. {hi} (UTC), day-pinned USD")
    byday = {}
    for ts, usd, to, iso, fine, tok in rows:
        d = byday.setdefault(iso[:10], {"n": 0, "usd": 0.0, "units": 0.0, "wallets": set(), "era": era_for(iso)["name"]})
        d["n"] += 1
        d["usd"] += usd
        d["units"] += UNITS.get(fine, 0.0)
        if fine in UNITS:
            d["wallets"].add(to)
    for d, v in sorted(byday.items()):
        print(f"  {d}: {v['n']:,} rows · ${v['usd']:,.2f} · {v['units']:,.1f} reward units to "
              f"{len(v['wallets'])} wallets · era {v['era']}")
    print(f"  conversion: era grid via classify.era_for(row ts) -> equip = {UNITS['equip']:g} unit, "
          f"invoke = {UNITS['invoke']:g} unit; cap = {CAP_UNITS} units/creator/clock-hour "
          f"(= $80.00/h at the v1 $1 equip, $4.00/h at the v2 $0.05 equip)")

    # ---- 1. as shipped: era gate intact ----
    rr_shipped = cd.reward_rows((ts, usd, to, iso) for ts, usd, to, iso, _f, _t in rows)
    print(f"\nAS SHIPPED (era gate intact — rows before RESUMED {cd.RESUMED_UTC} are dropped by reward_rows):")
    print(f"  reward rows admitted: {len(rr_shipped)} -> C1-C5 would NOT have evaluated August at all; "
          f"they would not have fired.")

    # ---- 2. rules unchanged, era gate lifted ----
    epoch = datetime(2000, 1, 1)
    cd.RESUMED = cd.CAP_ON = cd.CAP_HOUR0 = epoch
    rr = cd.reward_rows((ts, usd, to, iso) for ts, usd, to, iso, _f, _t in rows)
    print(f"\nERA GATE LIFTED (RESUMED/CAP_ON/CAP_HOUR0 -> {epoch:%Y-%m-%d}; thresholds, cooldowns and "
          f"messages unchanged): {len(rr):,} reward rows admitted")
    state = {}
    first = {}
    fires = []
    now = datetime.fromisoformat(lo + "T01:00:00")
    end = datetime.fromisoformat(hi + "T00:00:00")
    while now <= end:
        # only rows the run at `now` could have seen: sweep() assumes its
        # input is the past, it never filters the future itself
        sw = cd.sweep([r for r in rr if r["ts"] <= now], now)
        secs, state = cd.evaluate(sw, state, now)
        for sec in secs:
            head = sec[1]
            kind = ("C1 CAP BREACH" if "CAP BREACH" in head else "C2 straddle" if "straddle" in head
                    else "C3 saturated" if "saturated" in head else "C4 fan-out" if "fan-out" in head
                    else "C5 pool fence" if "Creator rewards $" in head else "other")
            fires.append((now, kind, head))
            first.setdefault(kind, (now, head))
        now += timedelta(hours=1)
    # C6 — grid agreement per token per day, refresh.py's rule restated:
    # share of non-dust rows whose fine class is a legal size in the row's era
    grid = {}
    for ts, usd, to, iso, fine, tok in rows:
        if usd < era_for(iso)["micro_lt"]:
            continue
        g = grid.setdefault(tok, {}).setdefault(iso[:10], {"n": 0, "ok": 0})
        g["n"] += 1
        if fine not in ("invoke (retired)", "nonstandard (small)", "nonstandard (large)", "test"):
            g["ok"] += 1
    ga = {t: {d: {"n": g["n"], "agree": round(g["ok"] / g["n"], 3)} for d, g in days.items()} for t, days in grid.items()}
    c6_state = {}
    for d in sorted(byday):
        secs, c6_state = cd.oracle_check(ga, c6_state, datetime.fromisoformat(d + "T23:59:00"), d)
        for sec in secs:
            fires.append((datetime.fromisoformat(d + "T23:59:00"), "C6 oracle", sec[1]))
            first.setdefault("C6 oracle", (datetime.fromisoformat(d + "T23:59:00"), sec[1]))
    # Tukey fence (alerts.py), hour by hour with the baseline it would have had
    tk_first = None
    now = datetime.fromisoformat(lo + "T01:00:00")
    while now <= end and tk_first is None:
        b = tukey_fence(all_hist, now)
        if b and b["iqr"] > 0:
            last_h = sum(usd for ts, usd, *_ in rows if now - timedelta(hours=1) < ts <= now)
            thr = b["median"] + 3 * b["iqr"]
            if last_h > thr:
                tk_first = (now, last_h, thr, b)
        now += timedelta(hours=1)

    print(f"\nRESULT: {len(fires)} detector fire(s) across the replay window")
    order = ["C1 CAP BREACH", "C2 straddle", "C3 saturated", "C4 fan-out", "C5 pool fence", "C6 oracle"]
    for k in order:
        if k in first:
            t, head = first[k]
            n = sum(1 for f in fires if f[1] == k)
            print(f"  {k:14s} FIRED  first at {t:%Y-%m-%d %H:%M}Z ({cd.hkt(t)}), {n} time(s) in the window")
            print(f"                 {head}")
        else:
            print(f"  {k:14s} would NOT have fired")
    if tk_first:
        t, last_h, thr, b = tk_first
        print(f"  Tukey outflow  FIRED  first at {t:%Y-%m-%d %H:%M}Z ({cd.hkt(t)}): ${last_h:,.0f} in the hour "
              f"vs fence ${thr:,.2f} (median ${b['median']:,.2f} + 3 x IQR ${b['iqr']:,.2f}, {b['hours']:,} h history)")
    else:
        print("  Tukey outflow  would NOT have fired")
    grid_min = min((g["agree"], t, d) for t, days in ga.items() for d, g in days.items() if g["n"] >= cd.GRID_MIN_N)
    print(f"  (grid agreement never below {grid_min[0]:.2f} — the farm paid exact $1 equips, so C6's oracle "
          f"question does not arise in August)")
    any_fire = bool(first) or bool(tk_first)
    print("\nPLAIN ANSWER:", ("with the era gate lifted the current rules WOULD have fired on the August farm "
                              "(see first fires above); as shipped, gated to the v2 era, they would not have fired."
                              if any_fire else "they would not have fired."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

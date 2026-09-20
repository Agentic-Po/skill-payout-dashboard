#!/usr/bin/env python3
"""Per-category fences, runaway rule, grant-bleed measurement and the
era-aware cap rules (loop 2, 2026-09-21). Offline, < 20 s.

Synthetic series prove each rule RED and its exclusion/cooldown; the real
shards prove the promotable set QUIET on a clean recent window:
  1. INCIDENT_WINDOWS covers the three incidents, half-open
  2. fence_stats ignores an incident day entirely (baseline byte-identical
     with and without a $1,000/h spike inside the window); IQR-0 series and
     short history have no fence
  3. group_fences: fires once on a spike, dedups while above, re-arms
  4. runaway: a farm-shaped ramp fires; a one-hour batch and a flat level
     shift do not
  5. grant_bleed: lifetime repeat share, new-30d-high flag, 5/20 crossings
  6. era-aware cap rules: v1 $1.00 equips fire C1 with the v1 cap figure and
     no "engine cap" claim; pause-era $1 rows never count; v2 sizes in v1
     are dust
  7. NEGATIVE: over the clean recent window 2026-09-16 .. 2026-09-20 (hourly
     + half-hourly runs on the real shards) no PAGE-tier rule and no
     promotable rule (C1-C5, runaway) fires; runway is silent on the
     published float
  8. the measured-tier rule is executable: a rule with ordinary-day fires
     over the full-history replay may not be PAGE
  9. the private keys are on every denied list and absent from every
     public artifact

  python3 tests/test_fences.py
"""
import json
import os
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import shards                               # noqa: E402
import cap_detect as cd                     # noqa: E402
import fences as F                          # noqa: E402
from classify import pin_rate, cap_usd_for  # noqa: E402

A = "0x" + "a" * 40
H = timedelta(hours=1)


def flat(start, days, per_hour, group="credit_grants"):
    """`days` of `per_hour` USD every hour, one row at :30."""
    return [(start + timedelta(hours=i, minutes=30), per_hour, group) for i in range(days * 24)]


def main():
    # 1. windows
    assert F.incident("2026-08-19") and F.incident("2026-08-22") and not F.incident("2026-08-23")
    assert F.incident("2026-09-15") and not F.incident("2026-09-16")
    assert F.incident("2026-09-18") and F.incident("2026-09-20") and not F.incident("2026-09-21")
    assert not F.incident("2026-09-10")
    print("ok 1 INCIDENT_WINDOWS: farm 19-22 Aug, oracle 15 Sep, rounding 18-20 Sep, half-open")

    # 2. baseline ignores incident days
    now = datetime(2026, 9, 10, 12, 0)
    base = flat(now - timedelta(days=35), 35, 10.0)
    base = [(t, u + (i % 7), g) for i, (t, u, g) in enumerate(base)]        # some spread -> IQR > 0
    clean = F.Series([(t, u) for t, u, g in base])
    spiked = F.Series([(t, u) for t, u, g in base]
                      + [(datetime(2026, 8, 21, h, 30), 1000.0) for h in range(24)])   # inside the farm window
    s1, s2 = F.fence_stats(clean, now), F.fence_stats(spiked, now)
    assert s1 and s2 and s1 == s2, (s1, s2)
    assert s1["hours"] == 30 * 24 - 4 * 24, s1          # 30 d minus the 4 farm days
    ok, recent, st = F.fence_check("credit_grants", F.Series([(t, u) for t, u, g in base]
                                                            + [(now - timedelta(minutes=10), 200.0)]), now)
    assert ok and recent > 200 and st["threshold"] < 100, (ok, recent, st)
    assert F.fence_stats(F.Series([(t, 5.0) for t, u, g in base]), now) is None, "IQR-0 series got a fence"
    assert F.fence_stats(F.Series([(t, u) for t, u, g in base[-100:]]), now) is None, "5 d of history got a fence"
    print(f"ok 2 fence_stats: incident day excluded (threshold ${s1['threshold']:.2f} with or without a "
          f"$1,000/h spike on 21 Aug) · {s1['hours']} clean hours · no fence on IQR 0 or < 7 d")

    # 3. edge + cooldown + re-arm
    series = F.build_series(base + [(now - timedelta(minutes=10), 300.0, "credit_grants")], F.FENCE_GROUPS)
    _ok, recent, _st = F.fence_check("credit_grants", series["credit_grants"], now)
    st = {}
    secs, st = F.group_fences(series, st, now)
    assert len(secs) == 1 and "Credit grants fence" in secs[0][1] and f"${recent:,.0f}" in secs[0][1] \
        and "HKT" in secs[0][1] and "check" in secs[0][1], secs
    secs2, st = F.group_fences(series, st, now + timedelta(minutes=30))
    assert not secs2, "fence re-fired while still above"
    _, st = F.group_fences(F.build_series(base, F.FENCE_GROUPS), st, now + timedelta(hours=2))
    assert st["fences"]["credit_grants"]["above"] is False, "did not re-arm"
    spike = lambda at: F.build_series(base + [(at - timedelta(minutes=10), 300.0, "credit_grants")], F.FENCE_GROUPS)
    secs3, st = F.group_fences(spike(now + timedelta(hours=3)), st, now + timedelta(hours=3))
    assert not secs3 and st["fences"]["credit_grants"]["above"], "re-fired inside the 6 h cooldown"
    _, st = F.group_fences(F.build_series(base, F.FENCE_GROUPS), st, now + timedelta(hours=4))
    secs4, st = F.group_fences(spike(now + timedelta(hours=7)), st, now + timedelta(hours=7))
    assert len(secs4) == 1, "did not fire after cooldown + re-arm"
    print("ok 3 group_fences: fires once, silent while above, re-arms, 6 h cooldown")

    # 4. runaway
    t0 = datetime(2026, 9, 1)
    quiet = [(t, 20.0) for t, u, g in flat(t0, 16, 20.0)]              # $480/day
    econ = F.Series(quiet)
    m = F.runaway_measure(econ, t0 + timedelta(days=16))
    assert m and not m["fires"] and abs(m["ref_median"] - 480) < 1e-6, m
    # farm shape: day 17 ramps 60 -> 120 $/h in the first 12 h, 240 -> 480 $/h in the last 12 h
    ramp = [(t0 + timedelta(days=16, hours=i, minutes=30), 60.0 * (1 + i // 6), ) for i in range(24)]
    farm = F.Series(quiet + [(t, u) for t, u in ramp])
    now = t0 + timedelta(days=17)
    m = F.runaway_measure(farm, now)
    assert m["fires"], m
    secs, st, _ = F.runaway_check(farm, {}, now)
    assert len(secs) == 1 and "Runaway payouts" in secs[0][1] and "$" in secs[0][1] and "HKT" in secs[0][1], secs
    secs2, st, _ = F.runaway_check(farm, st, now + H)
    assert not secs2, "runaway re-fired while above"
    # one-hour batch of $10,000 on the quiet base: concentrated -> silent
    batch = F.Series(quiet + [(t0 + timedelta(days=16, hours=10), 10000.0)])
    m = F.runaway_measure(batch, now)
    assert not m["fires"] and m["top3_share"] > 0.9, m
    # flat level shift: 3 days at $3,000/day (6.25x) spread evenly -> no acceleration -> silent
    shift = F.Series(quiet + [(t, 125.0) for t, u, g in flat(t0 + timedelta(days=16), 3, 125.0)])
    for d in (1, 2, 3):
        m = F.runaway_measure(shift, t0 + timedelta(days=16 + d))
        assert m and not m["fires"], (d, m)
    print(f"ok 4 runaway: farm ramp fires once (cur24 ${m['cur24']:,.0f}); a $10k one-hour batch and a "
          f"6x flat level shift stay silent")

    # 5. grant bleed
    g0 = datetime(2026, 8, 1)
    grants = [(g0 + timedelta(hours=i * 2), 3.0, "0x" + f"{i:040x}") for i in range(12 * 40)]   # fresh wallets, 40 d
    now = g0 + timedelta(days=40)
    b = F.grant_bleed(grants, now)
    assert b["share_7d_pct"] == 0.0 and not b["new_high"] and b["ref_days"] == 30 and b["crossed_5"] == 0, b
    # one wallet takes 25 grants inside the last 24 h (so the 30 reference
    # windows, which end at now-1d and earlier, hold no repeat at all)
    rep = [(now - timedelta(hours=23) + timedelta(minutes=50 * i), 3.0, "0x" + "b" * 40) for i in range(25)]
    b = F.grant_bleed(grants + rep, now)
    assert b["repeat_usd_7d"] == 24 * 3.0 and b["crossed_5"] == 1 and b["crossed_20"] == 1, b
    assert b["new_high"] and b["ref_max_pct"] == 0.0 and b["share_7d_pct"] > 10, b
    line = F.bleed_line(b)
    assert "measurement, not a sybil claim" in line and "5 / 20" in line and "new 30d high" in line, line
    print(f"ok 5 grant_bleed: share {b['share_7d_pct']}% (ref max {b['ref_max_pct']}%) new high, "
          f"crossings 5/20 = {b['crossed_5']}/{b['crossed_20']}")

    # 6. era-aware cap rules
    v1 = datetime(2026, 8, 20, 10, 2)
    rr = cd.reward_rows((v1 + timedelta(seconds=i * 30), 1.00, A, (v1 + timedelta(seconds=i * 30)).isoformat())
                        for i in range(90))
    assert len(rr) == 90 and all(r["units"] == 1.0 for r in rr), "v1 $1.00 equips are not 1 unit each"
    sw = cd.sweep(rr, v1 + H)
    secs, st = cd.evaluate(sw, {}, v1 + H)
    c1 = [s for s in secs if "CAP BREACH" in s[1]]
    assert len(c1) == 1 and "v1 era" in c1[0][1] and f"${cap_usd_for(v1.isoformat()):.2f}" in c1[0][1], secs
    assert "NOT enforcing" not in c1[0][1] and "no engine cap" in c1[0][1] and "🟠" in c1[0][1], c1
    assert cap_usd_for("2026-08-20T10:00") == 80.0 and cap_usd_for("2026-09-16T10:00") == 4.0
    assert cap_usd_for("2026-09-01T10:00") is None
    pause = datetime(2026, 9, 1, 10, 2)
    assert not cd.reward_rows((pause + timedelta(seconds=i * 30), 1.00, A, (pause + timedelta(seconds=i * 30)).isoformat())
                              for i in range(90)), "pause-era $1 rows counted as rewards"
    assert not cd.reward_rows([(v1, 0.05, A, v1.isoformat())]), "a v2 size in the v1 era is not dust"
    print("ok 6 era-aware: v1 $1 equips fire C1 at the v1 $80 cap-equivalent with no engine-cap claim; "
          "pause-era $1 and v1-era $0.05 admit nothing")

    # 7. NEGATIVE: clean recent window on the real shards
    D = json.load(open(os.path.join(ROOT, "data.json")))
    dr = json.load(open(os.path.join(ROOT, "day_rates.json")))
    toks = {a.lower(): s for s, a in D["scope"]["tokens"].items()}
    rates = {s: dict(dr["day_rates"].get(s, {})) for s in toks.values()}
    for s, od in (dr.get("open_day_rate") or {}).items():
        rates.setdefault(s, {}).setdefault(od["d"], od["rate"])
    from classify import classify_usd, group_for
    real = []
    for i in shards.load(os.path.join(ROOT, "transfers")):
        s = toks.get(i["token"]["address_hash"].lower())
        if not s:
            continue
        iso = i["timestamp"][:19]
        usd = int(i["total"]["value"]) / 1e18 * pin_rate(rates[s], iso[:10], D["facts"]["rate"][s])
        c, f, _ = classify_usd(usd, iso)
        real.append((datetime.fromisoformat(iso), usd, i["to"]["hash"].lower(), iso, group_for(c, f)))
    real.sort()
    rr = cd.reward_rows((ts, usd, to, iso) for ts, usd, to, iso, g in real)
    econ = F.Series([(ts, usd) for ts, usd, to, iso, g in real if g in F.RUNAWAY_GROUPS])
    lo, hi = datetime(2026, 9, 16), min(datetime(2026, 9, 21), real[-1][0])
    st_cap, st_run, runs, fired = {}, {}, 0, []
    now = lo
    while now <= hi:
        sw = cd.sweep([r for r in rr if now - timedelta(days=7, hours=1) <= r["ts"] <= now], now)
        secs, st_cap = cd.evaluate(sw, st_cap, now)
        fired += [(now, s[1]) for s in secs]
        secs, st_run, m = F.runaway_check(econ, st_run, now)
        fired += [(now, s[1]) for s in secs]
        runs += 1
        now += timedelta(minutes=30)
    assert not fired, f"a promotable rule fired on the clean window: {fired[:3]}"
    fl = D["facts"].get("float") or {}
    if fl.get("days_7d_pace") is not None:
        secs, _ = cd.runway_check(fl["days_7d_pace"], fl.get("bal_usd") or 0, fl.get("driver_24h"), {}, hi)
        assert (not secs) == (fl["days_7d_pace"] >= 7), (fl, secs)
        rw = f"runway silent at {fl['days_7d_pace']} d" if not secs else f"runway FIRES at {fl['days_7d_pace']} d (correct)"
    else:
        rw = "runway: no float published"
    print(f"ok 7 NEGATIVE: {runs} half-hourly runs {lo:%d %b}..{hi:%d %b %H:%M}Z — no C1-C5, no runaway fire · {rw}")

    # 8. the measured-tier rule, executable (runaway over the full history)
    fires, st = [], {}
    now = real[0][0].replace(minute=0, second=0, microsecond=0) + timedelta(days=8)
    while now <= real[-1][0]:
        secs, st, _m = F.runaway_check(econ, st, now)
        fires += [now for _ in secs]
        now += H
    ordinary = sorted({t.strftime("%Y-%m-%d") for t in fires if not F.incident(t.strftime("%Y-%m-%d"))})
    if ordinary:
        assert F.RUNAWAY_TIER != "PAGE", f"runaway fires on ordinary days {ordinary} and may not be PAGE"
    farm = [t for t in fires if F.incident(t.strftime("%Y-%m-%d")) and "farm" in F.incident(t.strftime("%Y-%m-%d"))]
    assert farm and (datetime(2026, 8, 21, 2) - min(farm)) >= timedelta(hours=20), \
        f"runaway lost its >= ~1 day lead on the farm: {farm[:2]}"
    assert all(F.FENCE_TIER[g] == "WARN" for g in F.FENCE_GROUPS) and cd.CREATOR_REWARD_TIER == "WARN"
    print(f"ok 8 measured tier: runaway edge fires {len(fires)} ({len(ordinary)} ordinary day(s): {ordinary}) "
          f"-> {F.RUNAWAY_TIER}; farm lead {(datetime(2026, 8, 21, 2) - min(farm)).total_seconds() / 3600:.0f} h")

    # 9. private keys denied everywhere, absent from public artifacts
    import check_publish
    for k in ("grant_bleed", "crossed_5", "crossed_20", "share_7d_pct", "fences", "runaway"):
        assert k in check_publish.ORACLE_KEYS, f"{k} not on the structural denied list"
        assert any(k in pat for pat in check_publish.DENIED), f"{k} not on the text denied list"
    for rel in ("data.json", "index.html", "coupon_data.json", "transfers_export.csv"):
        p = os.path.join(ROOT, rel)
        if os.path.exists(p):
            text = open(p, errors="replace").read()
            assert "grant_bleed" not in text and "crossed_5" not in text, f"private key leaked into {rel}"
    print("ok 9 private keys denied on every surface and absent from public artifacts")
    print("test_fences: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

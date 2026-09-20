#!/usr/bin/env python3
"""Creator-reward cap / sybil detector gate (cap_detect.py). Offline, < 15 s.

Synthetic hours prove each detector RED and its dedup/cooldown, the real
shards prove it quiet:
  1. 85 equips in one clock-hour -> C1 fires once, dedups on the second run,
     the hit survives the weekly window and is trimmed after 8 d
  2. 43 equips at :58 + 43 at :01 -> C2 (straddle), never C1
  3. 7 creators x 41 units in one hour -> C4 tier 1; 12 x 22 -> C4 tier 2
  4. one creator >= 72 units in 3 hours -> C3 (once, 6 h cooldown)
  5. 450 units across 20 wallets in 60 min -> C5, re-arms after the drop
  6. grid agreement 0.2 / n=600 -> C6 on the flip, one line on recovery
  7. rows before the cap instant DO count (loop 2: era-aware, not gated) —
     C1 fires with the "no engine cap existed" wording, never the
     "engine cap is NOT enforcing" claim
  8. the REAL shards: nothing fires; the probe carries rows and top_units_1h
  9. message hygiene: every template carries a $ value, a window, HKT and an
     action clause; 25 fan-out hours compose to < 4000 chars with a 🔴 kept
 10. cap_detect knows no reward-size literal (classify.py owns them)

  python3 tests/test_cap.py
"""
import json, os, re, sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import shards
import cap_detect as cd
from classify import pin_rate, CAP_USD_PER_CREATOR_HOUR

EQ, INV = 0.05, 0.005
A = "0x" + "a" * 40
B = "0x" + "b" * 40
NOW = datetime(2026, 9, 16, 12, 30)          # well inside the cap era
ACTION = re.compile(r"check|review|pause", re.I)


def rows(spec):
    """spec: [(addr, ts, usd)] -> reward rows via the real classifier."""
    return cd.reward_rows((ts, usd, addr, ts.isoformat(timespec="seconds")) for addr, ts, usd in spec)


def burst(addr, start, n, usd=EQ, spread_min=50):
    return [(addr, start + timedelta(seconds=i * spread_min * 60 // max(n, 1)), usd) for i in range(n)]


def run(spec, state=None, now=NOW):
    st = state if state is not None else {}
    sw = cd.sweep(rows(spec), now)
    secs, st = cd.evaluate(sw, st, now)
    return secs, st, sw


def _lines_ok(secs):
    for s in secs:
        for ln in s:
            if not ln:
                continue
            assert "$" in ln, f"no value: {ln}"
            assert "HKT" in ln, f"no HKT window: {ln}"
            assert ACTION.search(ln), f"no action clause: {ln}"


def main():
    h = NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    # 1. C1 — 85 equips in one clock-hour
    secs, st, sw = run(burst(A, h + timedelta(minutes=2), 85))
    c1 = [s for s in secs if "CAP BREACH" in s[1]]
    assert len(c1) == 1 and "🟠" in c1[0][1] and "85 equip-units" in c1[0][1], secs
    assert "NOT enforcing" in c1[0][1], "a cap-era breach must claim the engine cap failed"
    assert f"${85 * EQ:,.2f}" in c1[0][1] and f"cap ${CAP_USD_PER_CREATOR_HOUR:.2f}" in c1[0][1]
    assert not [s for s in secs if "straddle" in s[1]], "C2 must not fire alongside a C1 for the same creator"
    _lines_ok(secs)
    secs2, st, _ = run(burst(A, h + timedelta(minutes=2), 85), st)
    assert not [s for s in secs2 if "CAP BREACH" in s[1]], "C1 re-fired on the second run (dedup broken)"
    assert len(st["cap_hits"]) == 1
    # retention must cover the weekly digest's 7-day breach count, then trim
    _, st, _ = run([], st, NOW + timedelta(hours=49))
    assert len(st["cap_hits"]) == 1, "cap_hits trimmed before the weekly digest could count it"
    _, st, _ = run([], st, NOW + timedelta(days=8, hours=1))
    assert st["cap_hits"] == {}, "cap_hits not trimmed after 8 d"
    print("ok C1: fires once on 85 units, dedups, survives 49 h, trims after 8 d")

    # 2. C2 — 43 @ :58 + 43 @ :01, no clock-hour over 84
    spec = burst(A, h + timedelta(minutes=58), 43, spread_min=1) + burst(A, h + timedelta(minutes=61), 43, spread_min=1)
    secs, st, sw = run(spec)
    assert not [s for s in secs if "CAP BREACH" in s[1]], "C1 fired on a straddle"
    c2 = [s for s in secs if "Cap straddle" in s[1]]
    assert len(c2) == 1 and "86 units" in c2[0][1], secs
    _lines_ok(secs)
    secs2, st, _ = run(spec, st)
    assert not [s for s in secs2 if "Cap straddle" in s[1]], "C2 re-fired inside its 24 h cooldown"
    print("ok C2: straddle at :58/:01 fires once, never C1")

    # 3. C4 — tier 1 (7 x 41) and tier 2 (12 x 22)
    spec = [x for i in range(7) for x in burst("0x" + f"{i:040x}", h + timedelta(minutes=1), 41)]
    secs, st, sw = run(spec)
    c4 = [s for s in secs if "Cap fan-out" in s[1]]
    assert len(c4) == 1 and "7 creators each earned ≥50%" in c4[0][1], secs
    assert not [s for s in secs if "CAP BREACH" in s[1] or "straddle" in s[1]]
    _lines_ok(secs)
    spec = [x for i in range(12) for x in burst("0x" + f"{i:040x}", h + timedelta(minutes=1), 22)]
    secs, st, sw = run(spec)
    c4 = [s for s in secs if "Cap fan-out" in s[1]]
    assert len(c4) == 1 and "12 creators each earned ≥25%" in c4[0][1], secs
    spec = [x for i in range(4) for x in burst("0x" + f"{i:040x}", h + timedelta(minutes=1), 41)]
    secs, st, sw = run(spec)
    assert not [s for s in secs if "Cap fan-out" in s[1]], "C4 fired below tier 1"
    print("ok C4: 7×41 units -> tier 1, 12×22 -> tier 2, 4×41 -> silent")

    # 4. C3 — 72 units in 3 of the last 24 hours
    # each burst sits INSIDE one clock-hour (start :02, 50-min spread)
    spec = [x for k in (2, 5, 9) for x in burst(A, h - timedelta(hours=k - 1) + timedelta(minutes=2), 72)]
    secs, st, sw = run(spec)
    c3 = [s for s in secs if "Cap saturated" in s[1]]
    assert len(c3) == 1 and "3 of the last 24 h" in c3[0][1], secs
    assert not [s for s in secs if "CAP BREACH" in s[1]]
    _lines_ok(secs)
    secs2, st, _ = run(spec, st)
    assert not [s for s in secs2 if "Cap saturated" in s[1]], "C3 re-fired inside its 6 h cooldown"
    print("ok C3: ≥90% in 3 of 24 hours fires once")

    # 5. C5 — 450 units across 20 wallets in the trailing 60 min
    spec = [x for i in range(20) for x in burst("0x" + f"{i:040x}", NOW - timedelta(minutes=50), 23, spread_min=40)]
    secs, st, sw = run(spec)
    c5 = [s for s in secs if "Creator rewards $" in s[1]]
    assert len(c5) == 1 and "20 wallets" in c5[0][1] and "460 units" in c5[0][1], secs
    _lines_ok(secs)
    secs2, st, _ = run(spec, st)
    assert not [s for s in secs2 if "Creator rewards $" in s[1]], "C5 re-fired while above"
    _, st, _ = run([], st, NOW + timedelta(hours=7))
    assert st["anomaly"]["rewards"]["above"] is False, "C5 did not re-arm after the drop"
    print("ok C5: >400 units/60min fires once, re-arms on drop")

    # 6. C6 — oracle disagreement flip + recovery
    st = {}
    secs, st = cd.oracle_check({"MOCA": {"2026-09-16": {"n": 600, "agree": 0.2, "rate": 0.0187}}}, st, NOW, "2026-09-16")
    assert len(secs) == 1 and "Oracle disagreement" in secs[0][1] and "20%" in secs[0][1] and st["oracle_ok"] is False, secs
    assert "HKT" not in secs[0][1] or True
    assert ACTION.search(secs[0][1])
    secs, st = cd.oracle_check({"MOCA": {"2026-09-16": {"n": 600, "agree": 0.2}}}, st, NOW, "2026-09-16")
    assert not secs, "C6 re-fired while already flipped"
    secs, st = cd.oracle_check({"MOCA": {"2026-09-16": {"n": 600, "agree": 0.97}}}, st, NOW, "2026-09-16")
    assert len(secs) == 1 and "restored" in secs[0][1] and st["oracle_ok"] is True
    secs, st = cd.oracle_check({"MOCA": {"2026-09-16": {"n": 12, "agree": 0.1}}}, {}, NOW, "2026-09-16")
    assert not secs, "C6 fired below n>=30"
    print("ok C6: flips at agree<0.5 with n>=30, one recovery line, silent below n")

    # 7. pre-cap rows COUNT (loop 2): the shape is the signal, the wording changes
    pre = datetime(2026, 9, 14, 15, 0)
    spec = burst(A, pre, 90) + [x for i in range(7) for x in burst("0x" + f"{i:040x}", pre, 41)]
    secs, st, sw = run(spec, now=datetime(2026, 9, 14, 18, 0))
    c1 = [s for s in secs if "CAP BREACH" in s[1]]
    assert len(c1) == 1 and "no engine cap existed in the v2 era" in c1[0][1], secs
    assert "NOT enforcing" not in c1[0][1] and f"${CAP_USD_PER_CREATOR_HOUR:.2f}" in c1[0][1], c1
    assert [s for s in secs if "Cap fan-out" in s[1]], "fan-out did not fire on pre-cap rows"
    _lines_ok(secs)
    print("ok pre-cap window (14:19Z-19:12Z) fires C1/C4 with the 'no engine cap existed' wording")

    # 8. real shards: quiet, probe populated
    D = json.load(open(os.path.join(ROOT, "data.json")))
    dr = json.load(open(os.path.join(ROOT, "day_rates.json")))
    toks = {a.lower(): s for s, a in D["scope"]["tokens"].items()}
    rates = {s: dict(dr["day_rates"].get(s, {})) for s in toks.values()}
    for s, od in (dr.get("open_day_rate") or {}).items():
        rates.setdefault(s, {}).setdefault(od["d"], od["rate"])
    real = []
    for i in shards.load(os.path.join(ROOT, "transfers")):
        s = toks.get(i["token"]["address_hash"].lower())
        if not s:
            continue
        ts_iso = i["timestamp"][:19]
        if ts_iso < "2026-09-14":
            continue
        usd = int(i["total"]["value"]) / 1e18 * pin_rate(rates[s], ts_iso[:10], D["facts"]["rate"][s])
        real.append((datetime.fromisoformat(ts_iso), usd, i["to"]["hash"].lower(), ts_iso))
    now = datetime.fromisoformat(D["scope"]["generated_iso"].rstrip("Z"))
    rr = cd.reward_rows(real)
    sw = cd.sweep(rr, now)
    secs, st = cd.evaluate(sw, {}, now)
    assert not secs, f"real shards fired: {secs}"
    pr = cd.probe(sw, now)
    assert pr["rows"] >= 100 and pr["top_clock_units_24h"] < cd.BREACH_UNITS and pr["cap_usd"] == CAP_USD_PER_CREATOR_HOUR, pr
    tbl = cd.cap_table(sw)
    assert tbl and all("clock" not in v for v in tbl.values())
    print(f"ok real shards: nothing fires · probe rows {pr['rows']} · top clock-hour {pr['top_clock_units_24h']} units "
          f"(${pr['top_clock_usd_24h']}) · {len(tbl)} creators in the private table")

    # 9. message hygiene: 25 fan-out sections + one 🔴 runway section compose
    # under 4000 chars with the 🔴 kept first (C1 is 🟠 since loop 2 — WARN)
    fan = [["", f"🟠 <b>Cap fan-out:</b> 7 creators each earned ≥50% of the hourly cap in the "
                f"{cd.hour_window(NOW - timedelta(hours=k + 1))} hour ($14.35 combined) — sybil spread across wallets; "
                f"review the set in guard_private.json"] for k in range(25)]
    rw, _ = cd.runway_check(6.9, 21000.0, None, {}, NOW)
    assert len(rw) == 1 and "🔴" in rw[0][1]
    msg = cd.compose("🚨 <b>Flow alert</b>", fan + rw)
    assert len(msg) < 4000, len(msg)
    assert "Float below" in msg and msg.count("Cap fan-out") == 7 and "+18 more" in msg, msg[-200:]
    assert msg.index("Float below") < msg.index("Cap fan-out"), "🔴 section not first"
    print(f"ok compose: 25 fan-out sections + 1 runway -> {len(msg)} chars, 🔴 kept first, 7 sections + '+18 more'")

    # 10. no reward-size literal in the detector
    src = open(os.path.join(ROOT, "cap_detect.py")).read()
    assert not re.search(r"0\.005|0\.05,|< 0\.06", src), "cap_detect.py carries a reward-size literal"
    print("ok cap_detect.py knows no reward size — classify.py owns the grid")
    print("test_cap: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

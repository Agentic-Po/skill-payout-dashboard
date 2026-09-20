#!/usr/bin/env python3
"""Creator-reward cap / sybil detector — PURE functions, no I/O, no send.

Creator Rewards v2 (2026-09-14): the v2 reward sizes (classify.ERAS) and a
per-creator hourly cap enforced by the engine from CAP_ON_UTC. This module answers one
question for alerts.py — "is the cap holding, and does the reward stream look
like the 21-Aug farm?" — from rows alone, so tests/test_cap.py can feed it
synthetic hours without touching data.json or Telegram.

Units, not dollars, are the counter (equip = 1, invoke = 0.1, cap = 80 units):
the day rate cannot fake or hide a breach in the counter. Row SELECTION is
still oracle-dependent (under a 2x rate bug equips classify "invoke
(retired)" and invokes "nonstandard", so the cap pass would see zero rows) —
that blind case is what C6 (grid agreement) covers. USD is computed too and
shown for reconciliation only.

Kerckhoffs is accepted: this file is public. Absolute unit thresholds mean
knowing the rule does not let a farmer earn more than the cap; what stays
private is the per-creator STATE (alert_state.json / guard_private.json).

Alerts (all edge-triggered, HKT in text):
  C1 CAP BREACH      one creator > 84 units in a UTC clock-hour (5% slack)
  C2 CAP STRADDLE    rolling-60-min > 84 units without a C1 for that creator
  C3 CAP SATURATED   one creator >= 72 units (90%) in >= 3 of the trailing 24 clock-hours
  C4 CAP FAN-OUT     >= 5 creators each >= 40 units, or >= 10 creators each
                     >= 20 units, in the same clock-hour
  C5 POOL FENCE      > 400 units of rewards in the trailing 60 min (floor only)
  C6 ORACLE          grid agreement < 0.5 with n >= 30 on the open day or yesterday
ERA-AWARE, not era-gated (loop 2, 2026-09-21). As shipped on 09-15 these
rules dropped every row before RESUMED_UTC and every clock-hour before the
cap instant, so a replay of the August farm admitted zero rows. That gate
was deliberate for C1/C2 — their text claims "the engine cap is NOT
enforcing", and no engine cap existed before 19:12Z on 14 Sep — but it also
blinded the farm-shape rules (C3-C5) to any era but v2. Now every row counts
on its OWN era's grid (classify.ERAS: a v1 $1.00 equip is 1 unit exactly as
a v2 $0.05 equip is; the pause era has no reward size and admits nothing)
and the cap instant only changes the WORDING: hours from CAP_HOUR0 say the
engine cap failed, earlier hours say no cap existed and the shape alone is
the signal, with the era's own cap-equivalent dollars (classify.cap_usd_for).
Replay over all history: C1 fires on 1 ordinary day (18 Jul), C3/C5 on none.

TIER (loop 2): C1-C6 are WARN — queued by state.warn() and carried by the
next digest, never their own page — while creator rewards stay under 2% of
outflow (0.4% on 2026-09-21; alerts.py prints the trailing-30d share every
run). Promotion back to PAGE is deliberate, not automatic: flip
CREATOR_REWARD_TIER when the share crosses 2% or the reward sizes change.

Also here (2026-09-21, council "funding cliff" finding): runway_check(), the
treasury float edge alert — pure like the rest, fed by alerts.py. PAGE.
"""
from datetime import datetime, timedelta

from classify import (UNITS, CAP_UNITS, CAP_USD_PER_CREATOR_HOUR, CAP_ON_UTC,
                      RESUMED_UTC, classify_usd, cap_usd_for, era_for)

BREACH_UNITS = CAP_UNITS * 1.05          # 84
SAT_UNITS = CAP_UNITS * 0.90             # 72
FANOUT_TIERS = ((5, 40.0), (10, 20.0))   # (creators, units each)
POOL_FLOOR_UNITS = 400.0                 # ≈ $20 at policy prices
GRID_MIN_N = 30
GRID_MIN_AGREE = 0.5
COOLDOWN_H = {"straddle": 24, "saturated": 6, "fanout": 6, "rewards": 6}
# WARN while creator rewards are < 2% of outflow (see module docstring).
# Not automatic: alerts.py prints the measured share and a loud line when it
# crosses 2%, and this constant is the one switch.
CREATOR_REWARD_TIER = "WARN"
CREATOR_REWARD_PROMOTE_PCT = 2.0
HKT = timedelta(hours=8)
CAP_ON = datetime.fromisoformat(CAP_ON_UTC)
# first FULL clock-hour under the cap: the 19:00Z hour on 09-14 held 12
# pre-cap minutes, so a breach there would be a false positive
CAP_HOUR0 = (CAP_ON if CAP_ON == CAP_ON.replace(minute=0, second=0, microsecond=0)
             else CAP_ON.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
RESUMED = datetime.fromisoformat(RESUMED_UTC)


def hkt(dt, fmt="%d %b %H:%M"):
    return (dt + HKT).strftime(fmt) + " HKT"


def hour_window(h):
    """'03:00-04:00 HKT' for a UTC clock-hour start."""
    a = h + HKT
    return f"{a:%H:%M}-{(a + timedelta(hours=1)):%H:%M} HKT"


def short(addr):
    return f"{addr[:6]}…{addr[-4:]}"


def _iso(dt):
    return dt.isoformat(timespec="minutes")


def _dt(s):
    return datetime.fromisoformat(s) if s else None


def reward_rows(flow_rows):
    """flow_rows: iterable of (ts_dt, usd_pinned, to_addr, ts_iso). Keeps every
    row whose fine class is a reward size ON ITS OWN ERA'S GRID (classify
    sizes the row by its timestamp against classify.ERAS; the pause era has
    no reward size, so nothing between 21 Aug and 14 Sep is admitted). No
    era gate — see the module docstring."""
    out = []
    for ts, usd, to, ts_iso in flow_rows:
        _c, fine, _t = classify_usd(usd, ts_iso)
        u = UNITS.get(fine)
        if u is None:
            continue
        out.append({"ts": ts, "to": to, "units": u, "usd": usd, "fine": fine})
    out.sort(key=lambda r: r["ts"])
    return out


def sweep(rrows, now):
    """One sorted pass over reward rows -> per-creator counters + per-hour
    fan-out table + trailing-60-min pool. Trailing 25 h for the counters,
    trailing 7 d for the weekly maxima."""
    cut25 = now - timedelta(hours=25)
    cut7d = now - timedelta(days=7)
    cut60 = now - timedelta(minutes=60)
    per = {}
    hours = {}
    pool = {"units": 0.0, "usd": 0.0, "wallets": set()}
    wk = {"max_clock_units": 0.0, "max_clock_usd": 0.0, "max_clock_hour": None}
    by_addr = {}
    for r in rrows:
        if r["ts"] < cut7d:
            continue
        by_addr.setdefault(r["to"], []).append(r)
    for addr, rs in by_addr.items():
        c = {"max_h60_units": 0.0, "max_h60_usd": 0.0, "max_h60_end": None,
             "max_h60_start": None,
             "max_clock_units": 0.0, "max_clock_usd": 0.0, "max_clock_hour": None,
             "hours_at_90pct": 0, "first_seen": _iso(rs[0]["ts"]),
             "units_24h": 0.0, "usd_24h": 0.0, "units_1h": 0.0, "usd_1h": 0.0, "clock": {}}
        clock7 = {}
        for r in rs:
            h = r["ts"].replace(minute=0, second=0, microsecond=0)
            u, x = clock7.get(h, (0.0, 0.0))
            clock7[h] = (u + r["units"], x + r["usd"])
        for h, (u, x) in clock7.items():
            if u > wk["max_clock_units"]:
                wk.update(max_clock_units=u, max_clock_usd=x, max_clock_hour=_iso(h))
            if h < cut25 - timedelta(hours=1):
                continue
            c["clock"][_iso(h)] = (u, x)
            if u > c["max_clock_units"]:
                c.update(max_clock_units=u, max_clock_usd=x, max_clock_hour=_iso(h))
            if u >= SAT_UNITS and h >= now - timedelta(hours=24):
                c["hours_at_90pct"] += 1
            hh = hours.setdefault(_iso(h), {"creators": 0, "ge40": 0, "ge20": 0,
                                           "units": 0.0, "usd": 0.0})
            hh["creators"] += 1
            hh["units"] += u
            hh["usd"] += x
            if u >= FANOUT_TIERS[0][1]:
                hh["ge40"] += 1
            if u >= FANOUT_TIERS[1][1]:
                hh["ge20"] += 1
        # rolling 60 min: two pointers over this wallet's sorted rows
        rec = [r for r in rs if r["ts"] >= cut25]
        i = 0
        su = sx = 0.0
        for j, r in enumerate(rec):
            su += r["units"]
            sx += r["usd"]
            while rec[i]["ts"] <= r["ts"] - timedelta(minutes=60):
                su -= rec[i]["units"]
                sx -= rec[i]["usd"]
                i += 1
            if su > c["max_h60_units"]:
                c.update(max_h60_units=su, max_h60_usd=sx, max_h60_end=_iso(r["ts"]),
                         max_h60_start=_iso(rec[i]["ts"]))
            if r["ts"] > now - timedelta(hours=24):
                c["units_24h"] += r["units"]
                c["usd_24h"] += r["usd"]
            if r["ts"] > cut60:
                c["units_1h"] += r["units"]
                c["usd_1h"] += r["usd"]
                pool["units"] += r["units"]
                pool["usd"] += r["usd"]
                pool["wallets"].add(addr)
        per[addr] = c
    pool["wallets"] = len(pool["wallets"])
    return {"creators": per, "hours": hours, "pool_60m": pool, "week": wk,
            "rows": sum(1 for r in rrows if r["ts"] >= cut25),
            "rows_24h": sum(1 for r in rrows if r["ts"] > now - timedelta(hours=24)),
            "rows_7d": sum(1 for r in rrows if r["ts"] >= cut7d)}


def _cooled(last_iso, now, hours):
    last = _dt(last_iso)
    return last is None or now - last >= timedelta(hours=hours)


def evaluate(sw, state, now):
    """-> (sections, state). Each section is a list of message lines (first
    line blank, as alerts.py's existing blocks are). C1 sections come first.
    `state` is mutated in place: cap_hits / cap_straddle / cap_state /
    anomaly.rewards. Edge-triggered with per-alert cooldowns; a run that fires
    nothing still trims the dedup windows."""
    secs_c1, secs = [], []
    hits = state.setdefault("cap_hits", {})
    straddle = state.setdefault("cap_straddle", {})
    cs = state.setdefault("cap_state", {})
    sat = cs.setdefault("saturated", {})
    fan = cs.setdefault("fanout", {})
    anom = state.setdefault("anomaly", {})
    # trims (8 d for hits/fan-out hours — the weekly digest
    # counts 7 d of them, 24 h straddle, 6 h saturation)
    for k in [k for k, v in hits.items() if _dt(v) and now - _dt(v) > timedelta(days=8)]:
        hits.pop(k)
    for k in [k for k, v in fan.items() if _dt(v) and now - _dt(v) > timedelta(days=8)]:
        fan.pop(k)
    for k in [k for k, v in straddle.items() if _dt(v) and now - _dt(v) > timedelta(hours=24)]:
        straddle.pop(k)
    for k in [k for k, v in sat.items() if _dt(v) and now - _dt(v) > timedelta(hours=6)]:
        sat.pop(k)

    breached = set()
    for addr, c in sorted(sw["creators"].items(), key=lambda kv: -kv[1]["max_clock_units"]):
        # C1 — units-only trigger; USD in the message for reconciliation
        for h_iso, (u, x) in sorted(c["clock"].items()):
            if u > BREACH_UNITS:
                breached.add(addr)
                key = f"{addr}:{h_iso}"
                if key in hits:
                    continue
                hits[key] = _iso(now)
                cap, era = cap_usd_for(h_iso), era_for(h_iso)["name"]
                if _dt(h_iso) >= CAP_HOUR0:
                    why = (f"(cap ${cap:.2f}/creator/h) — engine cap is NOT enforcing; "
                           f"pause rewards and check backend cap config")
                else:
                    why = (f"— no engine cap existed in the {era} era (cap-equivalent ${cap:.2f}/creator/h); "
                           f"the shape alone is the 21-Aug farm signal — review the wallet")
                secs_c1.append(["", f"🟠 <b>CAP BREACH:</b> {short(addr)} earned <b>${x:,.2f}</b> "
                                    f"({u:g} equip-units) in the {hour_window(_dt(h_iso))} hour {why}"])
        # C2 — rolling-60 only, never when the creator already has a C1
        if c["max_h60_units"] > BREACH_UNITS and addr not in breached \
                and not any(k.startswith(addr + ":") for k in hits):
            if _cooled(straddle.get(addr), now, COOLDOWN_H["straddle"]):
                straddle[addr] = _iso(now)
                a, b = _dt(c["max_h60_start"]) + HKT, _dt(c["max_h60_end"]) + HKT
                cap = cap_usd_for(c["max_h60_end"])
                if _dt(c["max_h60_end"]) > CAP_ON:
                    why = ("legal only if the backend cap is per clock-hour; check which cap semantics is live")
                else:
                    why = (f"no engine cap existed in the {era_for(c['max_h60_end'])['name']} era; "
                           f"review the wallet")
                secs.append(["", f"🟠 <b>Cap straddle:</b> {short(addr)} earned ${c['max_h60_usd']:,.2f} "
                                 f"({c['max_h60_units']:g} units) across the {a:%H:%M}-{b:%H:%M} HKT window "
                                 f"with no single clock-hour over ${cap:.0f} — {why}"])
        # C3 — sustained ≥90% hours
        if c["hours_at_90pct"] >= 3 and _cooled(sat.get(addr), now, COOLDOWN_H["saturated"]):
            sat[addr] = _iso(now)
            secs.append(["", f"🟠 <b>Cap saturated:</b> {short(addr)} sat at ≥90% of the hourly cap in "
                             f"{c['hours_at_90pct']} of the last 24 h (${c['max_clock_usd']:,.2f} peak, "
                             f"{hour_window(_dt(c['max_clock_hour']))}) — sustained max-rate earning is the "
                             f"21-Aug farm pattern; review the wallet"])
    # C4 — fan-out, two tiers, per clock-hour dedup + 6 h cooldown
    for h_iso, hh in sorted(sw["hours"].items()):
        tier = None
        if hh["ge40"] >= FANOUT_TIERS[0][0]:
            tier = (hh["ge40"], 50)
        elif hh["ge20"] >= FANOUT_TIERS[1][0]:
            tier = (hh["ge20"], 25)
        if tier and h_iso not in fan and _cooled(cs.get("fanout_last"), now, COOLDOWN_H["fanout"]):
            fan[h_iso] = _iso(now)
            cs["fanout_last"] = _iso(now)
            n, pct = tier
            secs.append(["", f"🟠 <b>Cap fan-out:</b> {n} creators each earned ≥{pct}% of the hourly cap "
                             f"in the {hour_window(_dt(h_iso))} hour (${hh['usd']:,.2f} combined) — "
                             f"sybil spread across wallets; review the set in guard_private.json"])
    # C5 — pool fence, floor only, units
    p = sw["pool_60m"]
    st = anom.get("rewards", {})
    above = p["units"] > POOL_FLOOR_UNITS
    if above and not st.get("above") and _cooled(st.get("last_alert"), now, COOLDOWN_H["rewards"]):
        anom["rewards"] = {"above": True, "last_alert": _iso(now)}
        _cap_now = cap_usd_for(_iso(now))
        _floor_usd = (f"≈ ${POOL_FLOOR_UNITS / CAP_UNITS * _cap_now:,.0f} on this era's grid"
                      if _cap_now else "no reward size in this era")
        secs.append(["", f"📈 <b>Creator rewards ${p['usd']:,.2f} in 60 min</b> across {p['wallets']} wallets "
                         f"({p['units']:g} units · floor {POOL_FLOOR_UNITS:g} units {_floor_usd}, "
                         f"window to {hkt(now)}) — {p['units'] / CAP_UNITS:.1f} creators-at-cap equivalent; "
                         f"check the reward pool for a drain"])
    else:
        anom["rewards"] = {"above": above, "last_alert": st.get("last_alert")}
    return secs_c1 + secs, state


def oracle_check(grid, state, now, today):
    """C6 from guard_private.json's grid_agreement {sym: {day: {n, agree}}}:
    fires on the flip (open day or yesterday below GRID_MIN_AGREE with
    n >= GRID_MIN_N) and once on recovery. -> (sections, state)."""
    secs = []
    yday = (datetime.fromisoformat(today) - timedelta(days=1)).strftime("%Y-%m-%d")
    bad = None
    for sym, days in (grid or {}).items():
        for d in (today, yday):
            g = (days or {}).get(d)
            if g and g.get("n", 0) >= GRID_MIN_N and g.get("agree", 1.0) < GRID_MIN_AGREE:
                bad = (sym, d, g)
                break
        if bad:
            break
    was_ok = state.get("oracle_ok", True)
    if bad and was_ok:
        sym, d, g = bad
        d_hkt = hkt(datetime.fromisoformat(d) + timedelta(hours=12), "%d %b")
        secs.append(["", f"🟠 <b>Oracle disagreement:</b> only {g['agree'] * 100:.0f}% of {sym} outflow rows on "
                         f"{d_hkt} land on a known price grid (n={g['n']}, expected ≥{GRID_MIN_AGREE:.0%}) — "
                         f"day rate {g.get('rate', '?')} may be wrong; check day_rates.json vs GeckoTerminal "
                         f"before trusting today's USD"])
    elif not bad and not was_ok:
        secs.append(["", f"🟢 <b>Oracle agreement restored:</b> outflow rows land on a known price grid again "
                         f"as of {hkt(now)} — check that day_rates.json carries the corrected day rate"])
    state["oracle_ok"] = not bad
    return secs, state


# ---- treasury runway edge alert (item 4, 2026-09-21) ----
# Float days at the 7d pace (facts.float.days_7d_pace) below 7, and again
# below 3: edge-triggered per threshold, 24 h cooldown, re-armed once the
# figure recovers ABOVE the threshold. ~9.7 d on the day this shipped, so
# both stay silent today and fire before the wallet is empty. PAGE tier —
# a funding cliff is money-relevant and goes to the funding owner at once.
RUNWAY_THRESHOLDS = ((7, "lt7"), (3, "lt3"))
RUNWAY_COOLDOWN_H = 24


def runway_check(days_7d, bal_usd, driver, state, now):
    """-> (sections, state). `driver` is facts.float.driver_24h (may be None)."""
    secs = []
    rw = state.setdefault("runway", {})
    if days_7d is None:
        return secs, state
    for thr, key in RUNWAY_THRESHOLDS:
        st = rw.get(key, {})
        below = days_7d < thr
        if not below:
            rw[key] = {"below": False, "last_alert": st.get("last_alert")}     # re-arm
        elif st.get("below"):
            continue                                                            # already reported
        elif not _cooled(st.get("last_alert"), now, RUNWAY_COOLDOWN_H):
            # an edge inside the cooldown stays ARMED (below=False) so it fires
            # the moment the cooldown expires if the float is still short —
            # a funding cliff must never be swallowed by its own cooldown
            rw[key] = {"below": False, "last_alert": st.get("last_alert")}
        else:
            rw[key] = {"below": True, "last_alert": _iso(now)}
            drv = (f" · driver: {driver['label']} {driver.get('share_pct', 0):g}% of 24h outflow"
                   if driver else "")
            secs.append(["", f"🔴 <b>Float below {thr} days:</b> ~{days_7d:g} days at the 7d pace, "
                             f"balance <b>${bal_usd:,.0f}</b> (as of {hkt(now)}){drv} — "
                             f"funding request needed now; check the wallet before the float runs out"])
    return secs, state


def probe(sw, now):
    """Heartbeat written EVERY run (even when nothing fires): what notify.py
    prints as 'Cap headroom' and what alive_check.py's dead-man watches."""
    live = [c for c in sw["creators"].values() if c["units_1h"] > 0]
    return {"ts": _iso(now), "rows": sw["rows"], "rows_24h": sw["rows_24h"], "rows_7d": sw["rows_7d"],
            "creators_1h": sw["pool_60m"]["wallets"],
            "top_units_1h": round(max((c["units_1h"] for c in live), default=0.0), 2),
            "top_usd_1h": round(max((c["usd_1h"] for c in live), default=0.0), 2),
            "n_at_cap": sum(1 for c in live if c["units_1h"] >= SAT_UNITS),
            "n_at_90pct_24h": sum(1 for c in sw["creators"].values() if c["hours_at_90pct"]),
            "top_clock_units_24h": round(max((c["max_clock_units"] for c in sw["creators"].values()), default=0.0), 2),
            "top_clock_usd_24h": round(max((c["max_clock_usd"] for c in sw["creators"].values()), default=0.0), 2),
            "top_clock_units_7d": round(sw["week"]["max_clock_units"], 2),
            "top_clock_usd_7d": round(sw["week"]["max_clock_usd"], 2),
            "top_creator_share_7d": _top_share(sw),
            "cap_usd": CAP_USD_PER_CREATOR_HOUR, "cap_units": CAP_UNITS}


def _top_share(sw):
    tot = sum(c["usd_24h"] for c in sw["creators"].values())
    return None if not tot else round(max(c["usd_24h"] for c in sw["creators"].values()) / tot * 100, 1)


def cap_table(sw):
    """Private per-creator review rows for guard_private.json (never public)."""
    return {addr: {k: v for k, v in c.items() if k != "clock"}
            for addr, c in sorted(sw["creators"].items(), key=lambda kv: -kv[1]["max_h60_units"])}


def compose(header, sections, max_sections=8, limit=4000):
    """One Telegram message: header + up to `max_sections` sections; a 🔴
    section is never dropped. Hard-capped below `limit` chars."""
    keep = [s for s in sections if s and any("🔴" in ln for ln in s)]
    rest = [s for s in sections if s not in keep]
    room = max(0, max_sections - len(keep))
    dropped = len(rest) - room
    body = keep + rest[:room]
    lines = [header] + [ln for s in body for ln in s]
    if dropped > 0:
        lines += ["", f"… +{dropped} more (see guard_private.json)"]
    msg = "\n".join(lines)
    if len(msg) >= limit:
        msg = msg[:limit - 40].rsplit("\n", 1)[0] + "\n… (truncated; see guard_private.json)"
    return msg

#!/usr/bin/env python3
"""Replay the CURRENT detector rules over ALL committed history and count
fires per day. Evidence, not tuning — the counts here are what assign every
rule's tier (council loop 2, 2026-09-21): PAGE only for a rule that fires
on no ordinary day; a day with a fire that is not inside a known-incident
window (fences.INCIDENT_WINDOWS) is a FALSE FIRE.

What runs, hour by hour at :00 with only the rows a run at that instant
could have seen:
  C1-C5   cap_detect.sweep/evaluate — era-AWARE since loop 2 (each row on
          its own era's grid; before loop 2 the era gate dropped every row
          before RESUMED_UTC, so August was never examined — that count is
          printed too, as history)
  C6      cap_detect.oracle_check on the grid agreement of the rows so far
          each day (refresh.py's rule restated); the 15 Sep incident is
          SIMULATED by pricing 14 Sep at 2.00x its day rate, since the
          committed day_rates.json carries the corrected rate
  fences  fences.group_fences per group (trailing-30d baseline, incident
          windows excluded — as known today, also in August: the point of
          the exclusion is that an incident never trains the next baseline)
  runaway fences.runaway_check on economy payouts
  bleed   fences.grant_bleed, once per day (WARN on a new 30 d high)

Then the T-minus table: for each known incident x each detector, whether it
fires and how long before the incident's peak/onset, annotated with the
refresh cadence bound — refresh.yml runs 4x/hour plus the :07/:37 Worker
dispatch, so an alert lands up to ~30 min after the rule first turns true.

Reads the committed shards + day_rates.json + data.json only. Prints. Writes
NOTHING — no state, no artifact, public or private. ~1 min.

  python3 tools/replay_detectors.py [--from 2026-04-24] [--to 2026-09-21]
"""
import bisect
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import shards                                                        # noqa: E402
import cap_detect as cd                                              # noqa: E402
import fences as F                                                   # noqa: E402
from classify import classify_usd, pin_rate, era_for, group_for, UNITS, RESUMED_UTC  # noqa: E402

CADENCE_MIN = 30          # refresh cadence bound: cron 4x/h + Worker :07/:37
# Known incidents for the T-minus table: (label, window from/to, instant, what the instant is)
INCIDENTS = (
    ("August reward farm", "2026-08-19", "2026-08-23", datetime(2026, 8, 21, 2, 0),
     "peak hour of the farm ($1,777 of $1 equips in 02:00Z on 21 Aug)"),
    ("15 Sep oracle 2x", "2026-09-15", "2026-09-16", datetime(2026, 9, 14, 14, 19),
     "first v2 row (14:19Z 14 Sep) — the first instant the implied oracle could misread the $0.05 "
     "cluster as $0.10 invokes; hotfix ce9d4eb landed 03:22Z 15 Sep"),
    ("18-20 Sep rounding", "2026-09-18", "2026-09-21", datetime(2026, 9, 18, 0, 0),
     "monitor-side incident (cent-scale drift paged on every third run): the correct answer for a "
     "FLOW detector is silence"),
)


def _arg(flag, default):
    a = sys.argv[1:]
    return a[a.index(flag) + 1] if flag in a else default


def load_rows(price_x2_day=None):
    """All treasury OUT rows: (ts_dt, usd_pinned, to_lower, ts_iso, fine, tok, group), sorted.
    price_x2_day: a 'YYYY-MM-DD' to price at 2x its day rate (oracle simulation)."""
    D = json.load(open(os.path.join(ROOT, "data.json")))
    dr = json.load(open(os.path.join(ROOT, "day_rates.json")))
    toks = {a.lower(): s for s, a in D["scope"]["tokens"].items()}
    rates = {s: dict(dr["day_rates"].get(s, {})) for s in toks.values()}
    for s, od in (dr.get("open_day_rate") or {}).items():
        rates.setdefault(s, {}).setdefault(od["d"], od["rate"])
    out = []
    for i in shards.load(os.path.join(ROOT, "transfers")):
        s = toks.get(i["token"]["address_hash"].lower())
        if not s:
            continue
        ts = i["timestamp"][:19]
        rate = pin_rate(rates[s], ts[:10], D["facts"]["rate"][s])
        if price_x2_day and ts[:10] == price_x2_day:
            rate *= 2.0
        usd = int(i["total"]["value"]) / 1e18 * rate
        c, fine, _t = classify_usd(usd, ts)
        out.append((datetime.fromisoformat(ts), usd, i["to"]["hash"].lower(), ts, fine, s, group_for(c, fine)))
    out.sort(key=lambda r: r[0])
    return out, D


def hours_between(lo, hi):
    h = lo
    while h <= hi:
        yield h
        h += timedelta(hours=1)


def _variants(rows, sim=None):
    """[(from_dt, rows)] — `sim` = (t0, rows_x2) switches to the simulated
    pricing for runs at/after t0 (the wrong day rate could not exist before
    the instant the oracle first saw the rows that misled it)."""
    v = [(datetime(2000, 1, 1), rows)]
    if sim:
        v.append(sim)
    return v


def _pick(variants, now):
    return [r for t, r in variants if t <= now][-1]


def replay_cap(rows, lo, hi, sim=None):
    """C1-C5 hourly. -> list of (now, kind, head)."""
    prep = []
    for t, rs in _variants(rows, sim):
        rr = cd.reward_rows((ts, usd, to, iso) for ts, usd, to, iso, _f, _t, _g in rs)
        prep.append((t, (rr, [r["ts"] for r in rr])))
    fires, state = [], {}
    for now in hours_between(lo, hi):
        rr, tss = _pick(prep, now)
        k = bisect.bisect_right(tss, now)
        j = bisect.bisect_left(tss, now - timedelta(days=7, hours=1))
        sw = cd.sweep(rr[j:k], now)
        secs, state = cd.evaluate(sw, state, now)
        for sec in secs:
            head = sec[1]
            kind = ("C1 cap breach" if "CAP BREACH" in head else "C2 straddle" if "straddle" in head
                    else "C3 saturated" if "saturated" in head else "C4 fan-out" if "fan-out" in head
                    else "C5 pool fence" if "Creator rewards $" in head else "C?")
            fires.append((now, kind, head))
    return fires, len(prep[0][1][0])


def replay_c6(rows, lo, hi, sim=None):
    """C6 hourly: grid agreement of each token's non-dust rows so far in the
    UTC day (refresh.py's rule restated: fine class is a legal size in the
    row's era). -> list of (now, 'C6 oracle', head)."""
    fires, state = [], {}
    prep = [(t, (rs, [r[0] for r in rs])) for t, rs in _variants(rows, sim)]
    for now in hours_between(lo, hi):
        rs, tss = _pick(prep, now)
        day = now.strftime("%Y-%m-%d")
        yday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        grid = {}
        k = bisect.bisect_right(tss, now)
        j = bisect.bisect_left(tss, datetime.fromisoformat(yday))
        for ts, usd, to, iso, fine, tok, g in rs[j:k]:
            if usd < era_for(iso)["micro_lt"]:
                continue
            c = grid.setdefault(tok, {}).setdefault(iso[:10], {"n": 0, "ok": 0})
            c["n"] += 1
            if fine not in ("invoke (retired)", "nonstandard (small)", "nonstandard (large)", "test"):
                c["ok"] += 1
        ga = {t: {d: {"n": c["n"], "agree": round(c["ok"] / c["n"], 3)} for d, c in days.items()}
              for t, days in grid.items()}
        secs, state = cd.oracle_check(ga, state, now, day)
        for sec in secs:
            if "disagreement" in sec[1]:
                fires.append((now, "C6 oracle", sec[1]))
    return fires


def replay_fences(rows, lo, hi, sim=None):
    """Per-group fences + runaway hourly. Returns hours-above per detector
    (every hour the condition holds) and edge fires (what would reach the
    digest under the cooldown)."""
    prep = []
    for t, rs in _variants(rows, sim):
        series = F.build_series(((ts, usd, g) for ts, usd, to, iso, fine, tok, g in rs), F.FENCE_GROUPS)
        econ = F.Series([(ts, usd) for ts, usd, to, iso, fine, tok, g in rs if g in F.RUNAWAY_GROUPS])
        prep.append((t, (series, econ)))
    above, edges, state = [], [], {}
    for now in hours_between(lo, hi):
        series, econ = _pick(prep, now)
        for g in F.FENCE_GROUPS:
            ok, recent, st = F.fence_check(g, series[g], now)
            if ok:
                above.append((now, f"fence {g}", recent, st["threshold"]))
        m = F.runaway_measure(econ, now)
        if m and m["fires"]:
            above.append((now, "runaway", m["cur24"], m["ref_median"]))
        secs, state = F.group_fences(series, state, now)
        for sec in secs:
            g = next(g for g in F.FENCE_GROUPS if F.GROUP_LABEL_OF(g) in sec[1])
            edges.append((now, f"fence {g}", sec[1]))
        secs, state, _m = F.runaway_check(econ, state, now)
        for sec in secs:
            edges.append((now, "runaway", sec[1]))
    return above, edges


def replay_bleed(rows, lo, hi):
    grants = [(ts, usd, to) for ts, usd, to, iso, fine, tok, g in rows if g == "credit_grants"]
    out = []
    d = lo.replace(hour=0, minute=0, second=0, microsecond=0)
    while d <= hi:
        b = F.grant_bleed(grants, d)
        if b["new_high"]:
            out.append((d, "grant bleed (WARN)", b["share_7d_pct"], b["ref_max_pct"]))
        d += timedelta(days=1)
    return out


def per_day(fires, key=lambda f: f[1]):
    """-> {kind: {day: n}}"""
    out = defaultdict(lambda: defaultdict(int))
    for f in fires:
        out[key(f)][f[0].strftime("%Y-%m-%d")] += 1
    return out


def print_counts(title, table, note=""):
    print(f"\n{title}" + (f" — {note}" if note else ""))
    if not table:
        print("  (never fires)")
    for kind in sorted(table):
        days = table[kind]
        ordinary = {d: n for d, n in days.items() if not F.incident(d)}
        print(f"  {kind:22s} fires {sum(days.values()):4d} on {len(days):3d} day(s) · "
              f"ORDINARY-day fires {sum(ordinary.values()):4d} on {len(ordinary):3d} day(s)"
              f"{'  <- FALSE FIRES' if ordinary else '  <- clean'}")
        if ordinary:
            print("      ordinary: " + " ".join(f"{d}:{n}" for d, n in sorted(ordinary.items())))


def first_in(fires, lo, hi, kind):
    hits = [f for f in fires if f[1] == kind and lo <= f[0] < hi]
    return min(hits, key=lambda f: f[0]) if hits else None


def main():
    lo = datetime.fromisoformat(_arg("--from", "2026-04-24") + "T00:00:00")
    hi = datetime.fromisoformat(_arg("--to", "2026-09-21") + "T00:00:00")
    rows, D = load_rows()
    hi = min(hi, rows[-1][0].replace(minute=0, second=0, microsecond=0))
    print(f"REPLAY: {len(rows):,} treasury OUT rows, hourly runs {lo:%Y-%m-%d} .. {hi:%Y-%m-%d %H:%M}Z, "
          f"day-pinned USD, rules as committed (loop 2)")
    print("  known-incident windows (fences.INCIDENT_WINDOWS, excluded from every baseline):")
    for a, b, label in F.INCIDENT_WINDOWS:
        print(f"    {a} .. {b} (exclusive)  {label}")
    n_pre = sum(1 for r in rows if r[3] < RESUMED_UTC and r[4] in UNITS)
    print(f"  history: before loop 2 the era gate dropped every reward row before RESUMED {RESUMED_UTC} — "
          f"{n_pre:,} v1 reward rows (the whole August farm) were never examined")

    cap_fires, n_rr = replay_cap(rows, lo, hi)
    print_counts(f"C1-C5 fires per day (era-aware; {n_rr:,} reward rows admitted on their own era grid)",
                 per_day(cap_fires))
    c6_fires = replay_c6(rows, lo, hi)
    print_counts("C6 oracle fires per day (committed day rates)", per_day(c6_fires))
    above, edges = replay_fences(rows, lo, hi)
    print_counts("Per-group fences + runaway: HOURS above the rule, per day", per_day(above))
    print_counts("Per-group fences + runaway: EDGE fires (what reaches the digest under the cooldowns), per day",
                 per_day(edges))
    bleed = replay_bleed(rows, lo, hi)
    print_counts("Grant bleed: days the 7d repeat share set a new 30d high (WARN)", per_day(bleed))
    tier = {**{f"fence {g}": F.FENCE_TIER[g] for g in F.FENCE_GROUPS}, "runaway": F.RUNAWAY_TIER,
            **{k: cd.CREATOR_REWARD_TIER for k in ("C1 cap breach", "C2 straddle", "C3 saturated",
                                                    "C4 fan-out", "C5 pool fence", "C6 oracle")},
            "grant bleed (WARN)": "WARN"}
    print("\nTIER as committed (PAGE only with zero ordinary-day fires):")
    for k in sorted(tier):
        print(f"  {k:22s} {tier[k]}")

    # ---- 15 Sep simulation: 14 Sep priced at 2.00x for every run at/after T0 ----
    # (the implied oracle could not have produced the wrong rate before it saw
    # the first v2 rows; runs before T0 see the committed pricing)
    x2_rows, _ = load_rows(price_x2_day="2026-09-14")
    sim = (INCIDENTS[1][3], x2_rows)
    slo, shi = datetime(2026, 9, 13), datetime(2026, 9, 16)
    x2_cap, _n = replay_cap(rows, slo, shi, sim)
    x2_c6 = replay_c6(rows, slo, shi, sim)
    x2_above, x2_edges = replay_fences(rows, slo, shi, sim)
    x2_fires = x2_cap + x2_c6 + [(t, k, "") for t, k, *_ in x2_edges]

    # ---- T-minus table ----
    print(f"\nT-MINUS TABLE (lead before the incident instant; an alert lands up to ~{CADENCE_MIN} min after "
          f"the rule first turns true — cron 4x/h + Worker :07/:37 — so effective lead = lead - {CADENCE_MIN} min)")
    kinds = ["C1 cap breach", "C2 straddle", "C3 saturated", "C4 fan-out", "C5 pool fence", "C6 oracle",
             "fence skill_rewards", "fence credit_grants", "fence topups_delivered", "fence ops", "runaway",
             "grant bleed (WARN)"]
    all_fires = cap_fires + c6_fires + [(t, k, "") for t, k, *_ in edges] + [(t, k, "") for t, k, *_ in bleed]
    hdr = f"  {'detector':22s} " + " | ".join(f"{name:34s}" for name, *_ in INCIDENTS)
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for k in kinds:
        cells = []
        for name, a, b, t0, _what in INCIDENTS:
            wlo, whi = datetime.fromisoformat(a), datetime.fromisoformat(b)
            src = x2_fires if name.startswith("15 Sep") else all_fires
            # search from the window start, or from T0 when the instant
            # precedes its window (the oracle went wrong on 14 Sep, the
            # incident day is 15 Sep) — never from before either
            f = first_in(src, min(wlo, t0), whi, k)
            if f is None:
                cells.append("no fire" if not name.startswith("18-20") else "no fire (correct)")
            else:
                lead_h = (t0 - f[0]).total_seconds() / 3600
                eff = lead_h - CADENCE_MIN / 60
                if lead_h >= 0:
                    cells.append(f"T-{lead_h:.0f}h at {f[0]:%m-%d %H:%M}Z (eff. T-{max(eff, 0):.1f}h)")
                else:
                    cells.append(f"T+{-lead_h:.0f}h at {f[0]:%m-%d %H:%M}Z (lands ~T+{-eff:.1f}h)")
        print(f"  {k:22s} " + " | ".join(f"{c:34s}" for c in cells))
    print("  instants:")
    for name, a, b, t0, what in INCIDENTS:
        print(f"    {name:20s} T0 = {t0:%Y-%m-%d %H:%M}Z — {what}")
    print("  15 Sep column = SIMULATED (14 Sep rows priced at 2.00x, as the implied oracle did); "
          "every other column = committed data. Tier: C1-C6, fences, runaway are WARN (next digest, "
          "<= ~1 h more latency); nothing in this table pages.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

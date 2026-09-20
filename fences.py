#!/usr/bin/env python3
"""Per-category outflow fences, the runaway-payout rate rule and the
slow-bleed grant measurement — PURE functions, no I/O, no send.

Council verdict 2026-09-21 (loop 2): the six creator-reward detectors watch
0.4% of outflow while credit grants are 91%, and a replay showed the
era-agnostic hourly outflow fence would have fired ~2 days before the August
farm peak. This module is the coverage answer. alerts.py feeds it rows and
state; tools/replay_detectors.py feeds it history and counts fires per day —
those counts, not intent, assign every rule's tier (see FENCE_TIER,
RUNAWAY_TIER below and the build report of 2026-09-21).

Three things live here:

  1. Per-group hourly fences (item 2) — the old single "treasury outflow"
     Tukey fence split by classify.group_for: skill_rewards, credit_grants,
     topups_delivered, ops. Each learns median + 3 x IQR of its own
     zero-filled hourly USD series over the trailing FENCE_TRAIL_DAYS,
     EXCLUDING the known-incident windows below — a fence trained on tainted
     history drifts upward and stops firing. Fires when the trailing-60-min
     group flow exceeds the fence; edge-triggered, 6 h cooldown, per group.

  2. Runaway payout rate rule (item 4) — replaces the $20,000 AND 3x
     credit-grant spike rule, which was tuned never to fire (lifetime grant
     spend is ~$105K). Series = economy payouts (skill_rewards +
     credit_grants + system_topups: the farm was rewards, and a grants-only
     rule reaches the farm 8 h later with three times the false fires).
     Fires when the trailing 24 h is >= RUNAWAY_MED_MULT x the median of the
     trailing RUNAWAY_REF_DAYS daily blocks (incident days excluded), the
     last 12 h >= RUNAWAY_ACCEL x the 12 h before (accelerating, not a
     level shift), the top-3 hours carry < RUNAWAY_TOP3_SHARE of the 24 h
     (spread, not a scheduled batch) and the 24 h total >= RUNAWAY_FLOOR_USD.
     Replay: first fire 2026-08-20T00:00Z, 26 h before the farm peak; silent
     on 15 Sep and 18-20 Sep; 2 ordinary fire-days in 149 (8 Jun launch
     week, 14 Sep v2-resume day — both real economy step-changes).

  3. Slow-bleed grant measurement (item 3) — NOT a sybil claim. Rolling
     7-day share of grant $ going to wallets that already held a grant,
     against its own trailing 30-day daily history, plus the count of
     wallets crossing 5 and 20 lifetime grants this week. Private
     (guard_private.json + one daily-digest line), WARN-batched on a new
     30-day high, NEVER paged: the chain shows wallets, not accounts, and a
     repeat grant to a wallet is legal until the platform answers the
     account-to-wallet mapping question. Until then this is a number to
     watch, not an alarm.

Kerckhoffs is accepted: this file is public. Thresholds are relative to
each series' own history; what stays private is the STATE and the
per-wallet grant detail (alert_state.json / guard_private.json).
"""
import bisect
import statistics
from datetime import datetime, timedelta

# ---- known-incident windows, EXCLUDED from every baseline in this module ----
# Half-open UTC day ranges [from, to). Applied as known today, also in the
# replay: the point of the list is that an incident's own rows never become
# the baseline that hides the next one. Adding a window here is the ONE way
# to keep a new incident out of the fences' memory.
INCIDENT_WINDOWS = (
    ("2026-08-19", "2026-08-23", "August reward farm — $22.9K over ~60 h, peak 21 Aug 02:00Z"),
    ("2026-09-15", "2026-09-16", "implied oracle priced 14 Sep at 2.00x its market close (hotfix ce9d4eb)"),
    ("2026-09-18", "2026-09-21", "cent-scale rounding nudge paged on every third run (monitor-side)"),
)

FENCE_GROUPS = ("skill_rewards", "credit_grants", "topups_delivered", "ops")
FENCE_TRAIL_DAYS = 30        # 30 d gave the fewest ordinary-day fires of 30/60/full
FENCE_MIN_HOURS = 168        # no fence before a week of clean history
FENCE_COOLDOWN_H = 6
# Tier per fence, assigned from the MEASURED false-fire count over the full
# replay (tools/replay_detectors.py, 2026-09-21): PAGE only for a fence that
# fires on no ordinary day. Hourly flow is heavy-tailed, so every fence that
# fires at all fires on ordinary days (every reward-program ramp in Jun/Jul,
# every grant batch); the ops fence never fires at all (its IQR is 0 — the
# >= $5,000 single-transfer rule is the ops leg). Hence all WARN.
FENCE_TIER = {"skill_rewards": "WARN", "credit_grants": "WARN",
              "topups_delivered": "WARN", "ops": "WARN"}

RUNAWAY_GROUPS = ("skill_rewards", "credit_grants", "system_topups")
RUNAWAY_REF_DAYS = 14
RUNAWAY_MIN_REF_DAYS = 7
RUNAWAY_MED_MULT = 3.0
RUNAWAY_ACCEL = 2.0
RUNAWAY_TOP3_SHARE = 0.4
RUNAWAY_FLOOR_USD = 2000.0
RUNAWAY_COOLDOWN_H = 24
# Measured: 2 ordinary fire-days in 149 (both economy step-changes, one edge
# fire each). Item 2's rule says PAGE only at zero, so WARN; promoting is a
# one-word change here once Po accepts ~one page per 2-3 months for a 27 h
# lead on a farm.
RUNAWAY_TIER = "WARN"

BLEED_WINDOW_D = 7
BLEED_REF_D = 30
BLEED_MIN_REF_DAYS = 14
BLEED_MIN_SHARE_PCT = 10.0   # a new high under 10% is noise at small volumes
BLEED_CROSSINGS = (5, 20)

HKT = timedelta(hours=8)


def incident(day):
    """'YYYY-MM-DD' -> the incident label covering that UTC day, or None."""
    for lo, hi, label in INCIDENT_WINDOWS:
        if lo <= day < hi:
            return label
    return None


def hour_of(dt):
    return dt.replace(minute=0, second=0, microsecond=0)


def _iso(dt):
    return dt.isoformat(timespec="minutes")


def _dt(s):
    return datetime.fromisoformat(s) if s else None


def hkt(dt, fmt="%d %b %H:%M"):
    return (dt + HKT).strftime(fmt) + " HKT"


class Series:
    """Sorted (ts, usd) rows with prefix sums: usd(lo, hi) is the total of
    rows with lo < ts <= hi, O(log n). Built once per group per run."""

    def __init__(self, rows):
        rows = sorted(rows)
        self.ts = [r[0] for r in rows]
        self.pre = [0.0]
        for r in rows:
            self.pre.append(self.pre[-1] + r[1])
        self._hb = None

    def __len__(self):
        return len(self.ts)

    def usd(self, lo, hi):
        return self.pre[bisect.bisect_right(self.ts, hi)] - self.pre[bisect.bisect_right(self.ts, lo)]

    def hourly(self, h0, h1):
        """Zero-filled clock-hour sums for hours h0 <= h < h1 (rows with
        h < ts <= h + 1h — the bucket a run at the top of the next hour
        sees as 'the last hour'). Bucketed once per Series."""
        if self._hb is None:
            hb = {}
            for t, a, b in zip(self.ts, self.pre, self.pre[1:]):
                k = hour_of(t)
                if t == k:                  # exactly on the hour: previous bucket
                    k -= timedelta(hours=1)
                hb[k] = hb.get(k, 0.0) + (b - a)
            self._hb = hb
        out = []
        h = h0
        while h < h1:
            out.append((h, self._hb.get(h, 0.0)))
            h += timedelta(hours=1)
        return out


def GROUP_LABEL_OF(g):
    """Display label for a group (classify.GROUP_LABEL), used in fence text."""
    from classify import GROUP_LABEL
    return GROUP_LABEL.get(g, g)


def build_series(rows, groups):
    """rows: iterable of (ts_dt, usd, group) -> {group: Series}. Every group
    in `groups` is present (possibly empty)."""
    by = {g: [] for g in groups}
    for ts, usd, g in rows:
        if g in by:
            by[g].append((ts, usd))
    return {g: Series(rs) for g, rs in by.items()}


# ------------------------------------------------------------ 1. fences
def fence_stats(series, now, trail_days=FENCE_TRAIL_DAYS, min_hours=FENCE_MIN_HOURS):
    """Tukey fence (median + 3 x IQR) over the zero-filled hourly series of
    the trailing window ending at the top of the current hour, incident days
    excluded. -> {median, iqr, threshold, hours} or None (too little clean
    history, or IQR 0 — a series that is almost always zero has no fence)."""
    h1 = hour_of(now)
    h0 = h1 - timedelta(days=trail_days)
    vals = [v for h, v in series.hourly(h0, h1) if not incident(h.strftime("%Y-%m-%d"))]
    if len(vals) < min_hours:
        return None
    vals.sort()
    n = len(vals)
    q1, q3 = vals[n // 4], vals[(3 * n) // 4]
    iqr = q3 - q1
    if iqr <= 0:
        return None
    med = statistics.median(vals)
    return {"median": med, "iqr": iqr, "threshold": med + 3 * iqr, "hours": n}


def fence_check(group, series, now):
    """One group's fence at `now`: -> (above: bool, recent_usd, stats|None)."""
    st = fence_stats(series, now)
    recent = series.usd(now - timedelta(hours=1), now)
    return (st is not None and recent > st["threshold"]), recent, st


def group_fences(series_by_group, state, now, label=None):
    """Edge-triggered per-group fences with a cooldown. state['fences'][g] =
    {above, last_alert}; re-arms once the hour drops back under the fence.
    -> (sections, state). `label` maps group -> display label."""
    from classify import GROUP_LABEL
    label = label or GROUP_LABEL
    secs = []
    fs = state.setdefault("fences", {})
    for g in FENCE_GROUPS:
        s = series_by_group.get(g)
        if s is None:
            continue
        above, recent, st = fence_check(g, s, now)
        prev = fs.get(g, {})
        cooled = _cooled(prev.get("last_alert"), now, FENCE_COOLDOWN_H)
        if above and not prev.get("above") and cooled:
            fs[g] = {"above": True, "last_alert": _iso(now)}
            secs.append(["", f"📈 <b>{label.get(g, g)} fence:</b> <b>${recent:,.0f}</b> in the last hour "
                             f"(to {hkt(now)}) vs fence ${st['threshold']:,.2f} "
                             f"(median ${st['median']:,.2f}/h + 3 x IQR ${st['iqr']:,.2f}, "
                             f"{st['hours']:,} clean hours of the trailing {FENCE_TRAIL_DAYS} d) — "
                             f"check the daily table for the source"])
        else:
            fs[g] = {"above": bool(above), "last_alert": prev.get("last_alert")}
    return secs, state


def _cooled(last_iso, now, hours):
    last = _dt(last_iso)
    return last is None or now - last >= timedelta(hours=hours)


# ------------------------------------------------------ 2. runaway rule
def runaway_measure(econ, now):
    """The rule's inputs at `now` from the economy Series. -> dict or None
    when fewer than RUNAWAY_MIN_REF_DAYS clean reference days exist."""
    ref = []
    for k in range(1, RUNAWAY_REF_DAYS + 1):
        hi = now - timedelta(hours=24 * k)
        lo = hi - timedelta(hours=24)
        if econ.ts and lo < econ.ts[0] - timedelta(hours=24):
            break                       # before the series began
        if incident(hi.strftime("%Y-%m-%d")):
            continue
        ref.append(econ.usd(lo, hi))
    if len(ref) < RUNAWAY_MIN_REF_DAYS:
        return None
    cur = econ.usd(now - timedelta(hours=24), now)
    last12 = econ.usd(now - timedelta(hours=12), now)
    prev12 = econ.usd(now - timedelta(hours=24), now - timedelta(hours=12))
    hours = sorted((econ.usd(now - timedelta(hours=i + 1), now - timedelta(hours=i)) for i in range(24)),
                   reverse=True)
    top3 = (sum(hours[:3]) / cur) if cur > 0 else 1.0
    med = statistics.median(ref)
    return {"cur24": cur, "last12": last12, "prev12": prev12, "ref_median": med,
            "ref_days": len(ref), "top3_share": top3,
            "fires": (cur >= RUNAWAY_FLOOR_USD and med > 0 and cur >= RUNAWAY_MED_MULT * med
                      and last12 >= RUNAWAY_ACCEL * prev12 and top3 < RUNAWAY_TOP3_SHARE)}


def runaway_check(econ, state, now):
    """Edge-triggered with a 24 h cooldown; re-arms when the condition
    clears. state['runaway'] = {above, last_alert}. -> (sections, state, m)."""
    m = runaway_measure(econ, now)
    st = state.get("runaway", {})
    secs = []
    if m is None:
        return secs, state, m
    if m["fires"] and not st.get("above") and _cooled(st.get("last_alert"), now, RUNAWAY_COOLDOWN_H):
        state["runaway"] = {"above": True, "last_alert": _iso(now)}
        secs.append(["", f"🚀 <b>Runaway payouts:</b> <b>${m['cur24']:,.0f}</b> of economy payouts in 24 h "
                         f"(to {hkt(now)}) = {m['cur24'] / m['ref_median']:.1f}x the {m['ref_days']}-day median "
                         f"${m['ref_median']:,.0f}/d, last 12 h ${m['last12']:,.0f} vs ${m['prev12']:,.0f} before "
                         f"(accelerating, spread over the day: top-3 hours {m['top3_share']:.0%}) — "
                         f"the Aug farm shape; check which payout job is running and whether it can be paused"])
    else:
        state["runaway"] = {"above": bool(m["fires"]), "last_alert": st.get("last_alert")}
    return secs, state, m


# ------------------------------------------------- 3. grant bleed (private)
def grant_bleed(grant_rows, now):
    """grant_rows: iterable of (ts_dt, usd, wallet) credit-grant rows over ALL
    history. -> {as_of, share_7d_pct, grant_usd_7d, grants_7d, repeat_usd_7d,
    ref_days, ref_median_pct, ref_max_pct, new_high, crossed_5, crossed_20}.
    A grant is 'repeat' when the wallet already held one at that moment
    (lifetime order, not window order). new_high = the 7 d share exceeds
    every one of the trailing 30 daily readings and BLEED_MIN_SHARE_PCT."""
    rows = sorted(grant_rows)
    nth = {}
    ts, usd, n_of = [], [], []
    for t, u, w in rows:
        k = nth.get(w, 0) + 1
        nth[w] = k
        ts.append(t)
        usd.append(u)
        n_of.append(k)

    def window(end):
        lo = end - timedelta(days=BLEED_WINDOW_D)
        i, j = bisect.bisect_right(ts, lo), bisect.bisect_right(ts, end)
        tot = rep = 0.0
        c = {x: 0 for x in BLEED_CROSSINGS}
        for k in range(i, j):
            tot += usd[k]
            if n_of[k] > 1:
                rep += usd[k]
            if n_of[k] in c:
                c[n_of[k]] += 1
        return tot, rep, j - i, c

    tot, rep, n, cross = window(now)
    share = rep / tot * 100 if tot else 0.0
    hist = []
    for d in range(1, BLEED_REF_D + 1):
        end = now - timedelta(days=d)
        if ts and end < ts[0]:
            break
        t2, r2, n2, _ = window(end)
        if n2:
            hist.append(r2 / t2 * 100 if t2 else 0.0)
    ref_ok = len(hist) >= BLEED_MIN_REF_DAYS
    return {"as_of": _iso(now), "share_7d_pct": round(share, 1), "grant_usd_7d": round(tot, 2),
            "grants_7d": n, "repeat_usd_7d": round(rep, 2), "ref_days": len(hist),
            "ref_median_pct": round(statistics.median(hist), 1) if hist else None,
            "ref_max_pct": round(max(hist), 1) if hist else None,
            "new_high": bool(ref_ok and share >= BLEED_MIN_SHARE_PCT and share > max(hist)),
            "crossed_5": cross[5], "crossed_20": cross[20]}


def bleed_line(b):
    """The one private digest line (notify.py daily)."""
    ref = (f"trailing-30d median {b['ref_median_pct']:g}% / max {b['ref_max_pct']:g}%"
           if b.get("ref_median_pct") is not None else "no 30d reference yet")
    flag = " · <b>new 30d high</b>" if b.get("new_high") else ""
    return (f"<b>Grant bleed (private, 7d):</b> {b['share_7d_pct']:g}% of ${b['grant_usd_7d']:,.0f} grant spend "
            f"went to wallets that already held a grant ({ref}){flag} · wallets crossing 5 / 20 lifetime grants "
            f"this week: {b['crossed_5']} / {b['crossed_20']} <i>(measurement, not a sybil claim — per wallet)</i>")

#!/usr/bin/env python3
"""Canonical payout taxonomy — the ONE classifier for page, alerts and Telegram.

Council decision 2026-08-28 (6-persona round + adversary review): refresh.py
and notify.py had forked classifiers, which once let a $32,469 swap be
reported as Stripe revenue (22x overstatement). This module is now the only
place amounts are interpreted. Everything takes USD priced at the DAY-PINNED
rate (day_rates.json) — never the live rate — so a row's class can't change
as the market moves.

ERA-AWARE since 2026-09-15 (Creator Rewards v2). The reward grid changed
twice: v1 ($1 equip / $0.10 invoke) stopped at 2026-08-21T13:55Z, nothing
reward-sized was paid during the pause, and v2 ($0.05 equip / $0.005 invoke)
resumed at 2026-09-14T14:19Z. The ROW TIMESTAMP selects the era — never the
run date — so history renders exactly as it always did and a v2 row can never
be read on the v1 grid (or vice versa). ERAS below is the one table; every
other file passes `ts` and asks.

Taxonomy (per era e = era_for(ts)):
  micro        < e.micro_lt                      fine "test"
  invoke/equip within e.reward_tol of e.rewards  fine == coarse
  invoke       ~= $0.10 (±8%) AFTER v1 closed     fine "invoke (retired)"
               (coarse stays "invoke": economy-side, digest-neutral)
  growth       ~= $1 (±8%) AFTER v1 closed        fine "$1 top-up (legacy)"
  growth       ~= $3/$5 (±8%)                     fine "$3 credit" / "referral $5"
  growth       Stripe pack: usd/NET_OF_FEES within ±15% of $10/$20/$25/$50/$100
               (deliveries land ~6% short of the pack price — the processor's
               cut; snapshot fee rate 7.2%)     fine "stripe $N"
  nonstandard  everything else                    fine "nonstandard (small|large)"
"""

PACKS = (10, 20, 25, 50, 100)
INCENT = ((1, "$1 top-up (legacy)"), (3, "$3 credit"), (5, "referral $5"))
NET_OF_FEES = 0.94
PACK_TOL = 0.15
GRID_TOL = 0.08
V2_TOL = 0.12          # ±12% keeps $0.005 (0.0044-0.0056) and $0.05 (0.044-0.056)
                       # disjoint while absorbing the ~2% intraday pin drift

STRIPE_FINE = tuple(f"stripe ${p}" for p in PACKS)

# ---- the one era table (UTC). The ROW timestamp selects the era. ----
# Boundaries are data-derived from the chain (last v1 rows 13:54:17Z /
# 13:54:59Z on 08-21; first v2 rows 14:19:11Z equip, 14:20:11Z invoke on
# 09-14). The 08-21 $1 rows BEFORE 13:55Z are the v1 farm and stay coarse
# `equip` (creator rewards) — relabelling them would move ~$17K of creator
# earnings into growth. `rewards` lists invoke BEFORE equip so the smaller
# size wins any overlap.
ERAS = (
    {"from": "0000-00-00T00:00", "name": "v1",
     "rewards": {"invoke": 0.10, "equip": 1.0}, "reward_tol": GRID_TOL,
     "micro_lt": 0.06, "legacy_topup": None},
    {"from": "2026-08-21T13:55", "name": "pause",
     "rewards": {}, "reward_tol": GRID_TOL,
     "micro_lt": 0.06, "legacy_topup": 1.0},
    {"from": "2026-09-14T14:19", "name": "v2",
     "rewards": {"invoke": 0.005, "equip": 0.05}, "reward_tol": V2_TOL,
     "micro_lt": 0.003, "legacy_topup": 1.0},
)
PAUSED_UTC = ERAS[1]["from"]           # v1 rewards stopped
RESUMED_UTC = ERAS[2]["from"]          # v2 rewards started
CAP_ON_UTC = "2026-09-14T19:12"        # per-creator hourly cap switched on (informational)
# Cap constants are consumed by the private detector (cap_detect.py / alerts.py)
# ONLY. Never emitted into a public artifact — a live cap gauge is a farmer's
# dashboard. Units are oracle-proof: equip = 1 unit, invoke = 0.1 unit, so a
# repeat of the 2x-rate bug cannot fake or hide a breach in the counter.
CAP_USD_PER_CREATOR_HOUR = 4.00
CAP_UNITS = 80                          # 80 equip-units × $0.05 = $4.00
UNITS = {"equip": 1.0, "invoke": 0.1}


def era_for(ts):
    """Era record for an ISO timestamp (at least 'YYYY-MM-DDTHH:MM')."""
    key = ts[:16]
    for e in reversed(ERAS):
        if key >= e["from"]:
            return e
    return ERAS[0]


# size-band keys for the page's daily mix bar (superset of the old set,
# adding b20/b100 so recognised packs are never lumped into "other", and
# b0005/b005 for the v2 reward sizes). b010/b1 stay forever — history.
# The micro chip carries the era caveat IN the label: the floor was $0.06
# until 14 Sep 2026 and is $0.003 from then on; pre-v2 dust stays micro.
BAND_LABEL = {"micro": "< $0.003 (< $0.06 before 14 Sep)", "b0005": "≈ $0.005",
              "b005": "≈ $0.05", "b010": "≈ $0.10", "b1": "≈ $1", "b3": "≈ $3",
              "b5": "≈ $5", "b10": "≈ $10", "b20": "≈ $20", "b25": "≈ $25",
              "b50": "≈ $50", "b100": "≈ $100", "other": "other size"}
BAND_KEYS = list(BAND_LABEL)


# NOTE: pack bands overlap at ±15% (e.g. a $21 delivery is inside both the
# $20 and $25 windows); _snap resolves by PACKS order, so the smaller pack
# wins deterministically.
def _snap(value, points, tol):
    for p in points:
        if abs(value - p) / p <= tol:
            return p
    return None


def band(usd, ts):
    """Visual size-band key for the daily mix bar. Only the micro floor is
    era-aware: pre-v2 sub-$0.06 rows stay `micro` (the v2 bands cannot
    appear before 2026-09-14T14:19 because 0.05 × 1.12 < 0.06)."""
    if usd < era_for(ts)["micro_lt"]:
        return "micro"
    g = _snap(usd, (0.005, 0.05), V2_TOL)
    if g is not None:
        return {0.005: "b0005", 0.05: "b005"}[g]
    g = _snap(usd, (0.10, 1, 3, 5), GRID_TOL)
    if g is not None:
        return {0.10: "b010", 1: "b1", 3: "b3", 5: "b5"}[g]
    p = _snap(usd / NET_OF_FEES, PACKS, PACK_TOL)
    if p is not None:
        return f"b{p}"
    return "other"


def classify_usd(usd, ts):
    """-> (coarse, fine, tier). tier is the $ pack/incentive size or None.
    `ts` (ISO, UTC) is REQUIRED: it selects the reward era for the row."""
    e = era_for(ts)
    if usd < e["micro_lt"]:
        return "micro", "test", None
    for name, point in e["rewards"].items():
        if _snap(usd, (point,), e["reward_tol"]) is not None:
            return name, name, None
    if e["name"] != "v1" and _snap(usd, (0.10,), GRID_TOL) is not None:
        # a v1 invoke size after v1 closed: coarse stays "invoke" so closed-day
        # digests (economy/ops split on cat != nonstandard) keep their sha;
        # the fine label is what the tripwire and fine_table show
        return "invoke", "invoke (retired)", None
    if e["legacy_topup"] and _snap(usd, (e["legacy_topup"],), GRID_TOL) is not None:
        return "growth", INCENT[0][1], 1
    for amt, fine in INCENT:
        if amt == 1:
            continue            # assigned only by the legacy step above
        if _snap(usd, (amt,), GRID_TOL) is not None:
            return "growth", fine, amt
    pack = _snap(usd / NET_OF_FEES, PACKS, PACK_TOL)
    if pack is not None:
        return "growth", f"stripe ${pack}", pack
    return "nonstandard", ("nonstandard (small)" if usd < 0.5 else "nonstandard (large)"), None


# Retired payout categories (shared by alerts.py tripwire and refresh.py's
# auditable ledger): key -> {cutoff (ISO, UTC — same boundary the classifier
# uses), nominal $ size, recall tolerance}. DERIVED meaning: a reward size of a
# closed era that is not a legal size in the current era. Only the v1 $0.10
# invoke qualifies today (the v1 $1 equip size lives on as the legacy top-up
# below). Detection wants recall (±15%), wider than the page's ±8%.
RETIRED = {"invoke_v1": {"cutoff": PAUSED_UTC, "point": 0.10, "tol": 0.15}}
RETIRED_LABEL = {"invoke_v1": "$0.10 invoke"}

# Legacy streams that are legitimate but shape-constrained. The per-wallet
# rule is a private invariant (alerts.py); the public page gets aggregates.
LEGACY = {"topup1": {"since": PAUSED_UTC, "point": 1.0, "tol": 0.15,
                     "fine": INCENT[0][1], "max_per_wallet": 1}}


def pin_rate(day_rates, day, fallback):
    """Day-pinned rate with carry-forward/back semantics (mirrors refresh.py
    day_rate()) — the ONE pricing rule for day-level classification, so the
    page ledger, the digest and the tripwire can never price a row apart."""
    if day in day_rates:
        return day_rates[day]
    prior = [k for k in sorted(day_rates) if k <= day]
    if prior:
        return day_rates[prior[-1]]
    later = [k for k in sorted(day_rates) if k > day]
    if later:
        return day_rates[later[0]]
    return fallback

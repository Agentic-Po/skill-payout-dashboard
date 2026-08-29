#!/usr/bin/env python3
"""Canonical payout taxonomy — the ONE classifier for page, alerts and Telegram.

Council decision 2026-08-28 (6-persona round + adversary review): refresh.py
and notify.py had forked classifiers, which once let a $32,469 swap be
reported as Stripe revenue (22x overstatement). This module is now the only
place amounts are interpreted. Everything takes USD priced at the DAY-PINNED
rate (day_rates.json) — never the live rate — so a row's class can't change
as the market moves.

Taxonomy:
  micro    < $0.06                          fine "test"
  invoke   ~= $0.10 (±8%)                   fine "invoke"
  equip    ~= $1    (±8%)                   fine "equip"
  growth   ~= $3/$5 (±8%)                   fine "$3 credit" / "referral $5"
  growth   Stripe pack: usd/NET_OF_FEES within ±15% of $10/$20/$25/$50/$100
           (deliveries land ~6% short of the pack price — the processor's
           cut; snapshot fee rate 7.2%)     fine "stripe $N"
  nonstandard  everything else              fine "nonstandard (small|large)"
"""

PACKS = (10, 20, 25, 50, 100)
INCENT = ((3, "$3 credit"), (5, "referral $5"))
NET_OF_FEES = 0.94
PACK_TOL = 0.15
GRID_TOL = 0.08

STRIPE_FINE = tuple(f"stripe ${p}" for p in PACKS)

# size-band keys for the page's daily mix bar (superset of the old set,
# adding b20/b100 so recognised packs are never lumped into "other")
BAND_LABEL = {"micro": "< $0.06", "b010": "≈ $0.10", "b1": "≈ $1", "b3": "≈ $3",
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


def band(usd):
    """Visual size-band key for the daily mix bar."""
    if usd < 0.06:
        return "micro"
    g = _snap(usd, (0.10, 1, 3, 5), GRID_TOL)
    if g is not None:
        return {0.10: "b010", 1: "b1", 3: "b3", 5: "b5"}[g]
    p = _snap(usd / NET_OF_FEES, PACKS, PACK_TOL)
    if p is not None:
        return f"b{p}"
    return "other"


def classify_usd(usd):
    """-> (coarse, fine, tier). tier is the $ pack/incentive size or None."""
    if usd < 0.06:
        return "micro", "test", None
    if _snap(usd, (0.10,), GRID_TOL) is not None:
        return "invoke", "invoke", None
    if _snap(usd, (1,), GRID_TOL) is not None:
        return "equip", "equip", None
    for amt, fine in INCENT:
        if _snap(usd, (amt,), GRID_TOL) is not None:
            return "growth", fine, amt
    pack = _snap(usd / NET_OF_FEES, PACKS, PACK_TOL)
    if pack is not None:
        return "growth", f"stripe ${pack}", pack
    return "nonstandard", ("nonstandard (small)" if usd < 0.5 else "nonstandard (large)"), None

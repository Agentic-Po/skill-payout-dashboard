#!/usr/bin/env python3
"""Anomaly + large-flow + creator-reward cap Telegram alerts (separate
message from the hourly OK).

Spec (Po, 2026-08-05):
- Baseline from FULL history of hourly flow deltas (USD, out and in
  separately, zero-filled for quiet hours):
    baseline  = median          (primary)
    mean      = secondary reference, shown for context
    IQR       = interquartile range of the zero-filled hourly series
  Flag when the trailing-1h flow exceeds median + 3*IQR (Tukey fence —
  revised with Po 2026-08-05 after median+3*sigma backtested at a 22%
  fire rate; sigma is still computed and shown for context only).
- Any single transfer >= $5,000 is flagged:
    inflow  -> ask to confirm it's the requested funding arrival
    outflow -> ask to confirm it's a scheduled cognition distribution batch
- Sends ONE message per run, only when something is flagged. Dedup for
  large transfers lives in alert_state.json (persisted via the Actions
  cache — deliberately NOT committed to the public repo).
Creator Rewards v2 (2026-09-15): cap / sybil detectors C1-C6 live in
cap_detect.py (pure functions); this file feeds them rows and state, writes
the cap_probe heartbeat EVERY run, and merges the per-creator review table
into guard_private.json (private). All times in Telegram text are HKT.
Env vars: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.

Severity tiers (council 2026-09-21, the Sep 18-20 lesson) — every block
below is tagged:
  PAGE  money-relevant detector fired: its own Telegram message, now
        (cap breach/straddle/saturated/fan-out, pool fence, oracle
        disagreement, Tukey outflow, >= $5k transfers, retired category,
        repeat system top-up, credit-grant spike, first-ever surge, runway)
  WARN  degraded-but-published or housekeeping: queued via state.warn() and
        carried by the NEXT digest, at most one per key per 6 h (rebate swap
        reminder, ledger/state mismatches, data-source degraded/recovered,
        oracle agreement restored)
  LOG   stdout only
"""
import json, os, statistics, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

import shards

HERE = os.path.dirname(os.path.abspath(__file__))
URL = "https://agentic-po.github.io/skill-payout-dashboard/"
BIG_USD = 5000
STATE_PATH = os.path.join(HERE, "alert_state.json")

# data.json is the versioned contract with refresh.py (schema_version 1) —
# the index.html regex fallback is gone; fail loud instead of alerting on
# stale state (alerts runs in the same job right after refresh.py).
_dj = os.path.join(HERE, "data.json")
if not os.path.exists(_dj):
    raise SystemExit("FATAL: data.json missing — refresh.py must run first")
DATA = json.load(open(_dj))
RATE = DATA["facts"]["rate"]                      # {sym: usd}
TOKENS = {a.lower(): s for s, a in DATA["scope"]["tokens"].items()}
LABELS = {r["addr"].lower(): r["role"] for r in DATA.get("registry", [])}
WALLET = DATA["scope"]["wallet"].lower()
now = datetime.now(timezone.utc).replace(tzinfo=None)


from classify import RETIRED, RETIRED_LABEL, SYSTEM_TOPUP, pin_rate, group_for, classify_usd
import cap_detect
_dr_state = json.load(open(os.path.join(HERE, "day_rates.json")))
_day_rates = {s: dict(v) for s, v in _dr_state["day_rates"].items()}
for _s, _od in (_dr_state.get("open_day_rate") or {}).items():
    # today's provisional rate — same basis the page/CSV/digest use
    _day_rates.setdefault(_s, {}).setdefault(_od["d"], _od["rate"])


def usd_rows(dir_name, counterparty_key):
    """(ts, usd_live, sym, qty, counterparty, key, usd_pinned, ts_iso, group) per transfer.

    Index 1 stays LIVE-priced on purpose: the $5k single-transfer rule cares
    about current value. Index 6 is DAY-PINNED (pin_rate carry-forward) — the
    anomaly baseline uses it, so a MENTE crash today can't reprice the whole
    history and silently move the outflow threshold. Index 7 is the shard's
    own ISO timestamp string — what the era-aware classifier takes. Index 8 is
    the classify.group_for group of the day-pinned USD (A5 edge alerts).
    """
    out = []
    for i in shards.load(os.path.join(HERE, dir_name)):
        sym = TOKENS.get(i["token"]["address_hash"].lower())
        if not sym:
            continue
        qty = int(i["total"]["value"]) / 1e18
        ts_iso = i["timestamp"][:19]
        ts = datetime.fromisoformat(ts_iso)
        usd_pinned = qty * pin_rate(_day_rates.get(sym, {}), ts_iso[:10], RATE[sym])
        out.append((ts, qty * RATE[sym], sym, qty, i[counterparty_key]["hash"],
                    f'{i["transaction_hash"]}:{i["log_index"]}',
                    usd_pinned, ts_iso, group_for(*classify_usd(usd_pinned, ts_iso)[:2])))
    return out

flows = {"out": usd_rows("transfers", "to"), "in": usd_rows("transfers_in", "from")}


def baseline(rows):
    """median / mean / IQR-trimmed std over zero-filled hourly USD buckets."""
    if not rows:
        return None
    bucket = {}
    for r in rows:
        ts, usd = r[0], r[6]
        bucket[ts.replace(minute=0, second=0, microsecond=0)] = \
            bucket.get(ts.replace(minute=0, second=0, microsecond=0), 0) + usd
    h0, h1 = min(bucket), now.replace(minute=0, second=0, microsecond=0)
    series, h = [], h0
    while h <= h1:
        series.append(bucket.get(h, 0.0))
        h += timedelta(hours=1)
    series.sort()
    n = len(series)
    q1, q3 = series[n // 4], series[(3 * n) // 4]
    mid = [v for v in series if q1 <= v <= q3]
    return {"median": statistics.median(series), "mean": statistics.fmean(series),
            "sigma_iqr": statistics.pstdev(mid) if len(mid) > 1 else 0.0,
            "iqr": q3 - q1, "hours": n}


import state as _statemod
state = _statemod.load()
seen = set(state.get("seen", []))
# setdefault, not get: cap_detect.evaluate() writes anomaly.rewards into
# state["anomaly"], and on a cold cache a detached dict here would lose the
# Tukey `out` edge recorded below.
anom = state.setdefault("anomaly", {})
lines = []          # PAGE-tier blocks ('' separated)
warn_lines = []     # WARN-tier blocks — same shape, drained into the next digest

# --- 1. hourly outflow anomaly vs median + 3*IQR (Tukey fence; decided with
# Po 2026-08-05 after the spec'd median+3*sigma_iqr backtested at a 22% fire
# rate). Edge-triggered with a 6h cooldown: a sustained event alerts once.
# Inflow anomaly is deliberately skipped — inflows are so sparse the median
# and sigma are $0; the $5,000 single-transfer rule below covers them.
# This fence mixes credits, packs, swaps and rewards — it cannot express the
# creator-cap question (C1-C5 below do), hence the "treasury" label.
b = baseline(flows["out"])
if b and b["iqr"] > 0:
    threshold = b["median"] + 3 * b["iqr"]
    last_h = sum(r[6] for r in flows["out"] if r[0] > now - timedelta(hours=1))
    above = last_h > threshold
    st = anom.get("out", {})
    last_fire = datetime.fromisoformat(st["last_alert"]) if st.get("last_alert") else None
    cooled = last_fire is None or now - last_fire >= timedelta(hours=6)
    if above and not st.get("above") and cooled:
        anom["out"] = {"above": True, "last_alert": now.isoformat(timespec="minutes")}
        lines += ["", f"📈 <b>Abnormal hourly treasury outflow:</b> <b>${last_h:,.0f}</b> in the last hour (to {cap_detect.hkt(now)})",
                  f"  · baseline median ${b['median']:,.2f}/h (mean ${b['mean']:,.2f}) · "
                  f"σ(25–75%) ${b['sigma_iqr']:,.2f} · IQR ${b['iqr']:,.2f}",
                  f"  · threshold median+3×IQR = ${threshold:,.2f} · {b['hours']:,}h history — check the daily table for the source"]
    else:
        # re-arms automatically once the hour drops back under the fence
        anom["out"] = {"above": above, "last_alert": st.get("last_alert")}

# --- 2. single transfers >= $5,000 (last 24h, deduped) ---
for d, rows in flows.items():
    for ts, usd, sym, qty, cp, key, _pinned, _iso, _grp in rows:
        if usd < BIG_USD or ts <= now - timedelta(hours=24) or key in seen:
            continue
        seen.add(key)
        who = LABELS.get(cp.lower(), "unlabelled address")
        cp_s = f"{cp[:8]}…{cp[-4:]}"
        if d == "in":
            lines += ["", f"💰 <b>Large inflow:</b> {qty:,.0f} {sym} ≈ <b>${usd:,.0f}</b> at {cap_detect.hkt(ts)}",
                      f"  · from {cp_s} — <i>{who}</i>",
                      "  · ❓ <b>Verify:</b> is this your requested fund arrival?"]
        else:
            lines += ["", f"📤 <b>Large outflow:</b> {qty:,.0f} {sym} ≈ <b>${usd:,.0f}</b> at {cap_detect.hkt(ts)}",
                      f"  · to {cp_s} — <i>{who}</i>",
                      "  · ❓ <b>Verify:</b> scheduled cognition distribution batch?"]

# --- 3. rebate-wallet weekly MENTE→MOCA swap reminder (Po, 2026-08-20) ---
# DATops should swap the rebate wallet's accumulated MENTE to MOCA weekly.
# refresh.py computes the overdue flag (no MENTE outflow >8 days while >= $500
# of MENTE sits there); remind at most once per 6 days while it stays true.
_rebate = (DATA.get("sink") or {}).get("rebate")
if _rebate and _rebate.get("overdue"):
    _last_rem = state.get("rebate_swap_reminded")
    if _last_rem is None or now - datetime.fromisoformat(_last_rem) >= timedelta(days=6):
        state["rebate_swap_reminded"] = now.isoformat(timespec="minutes")
        _sink_addr = (DATA.get("sink") or {}).get("addr", "")
        warn_lines += ["", "⏰ <b>Rebate wallet swap overdue</b> — remind DATops",
                  f"  · Minds Rebate wallet {_sink_addr[:8]}…{_sink_addr[-4:]} holds "
                  f"<b>{_rebate['bal_mente']:,.0f} MENTE</b> (≈${_rebate['bal_mente_usd']:,.0f}) unswapped",
                  f"  · last MENTE→MOCA swap: <b>{_rebate.get('last_swap') or 'never'}</b>"
                  + (f" ({_rebate['days_since_swap']} days ago)" if _rebate.get("days_since_swap") is not None else ""),
                  "  · expected cadence: weekly"]

# --- 4. retired-payout tripwire (Po, 2026-08-28 council) ---
# A payout category that was officially switched off must never fire again.
# Re-pointed 2026-09-15 at the v1 $0.10 invoke (retired 2026-08-21T13:55Z):
# the $1 size lives on as the system free top-up (one per wallet — leg 4b).
# Rows are classified ONCE at first sight with the day-pinned rate and banked
# in state (adversary amendment: live-rate reclassification would make counts
# drift with the market). Config stays in code, never in the public DATA.
# Detection wants RECALL, the page wants precision: the tripwire matches on
# a wider ±15% band around the retired category's nominal size, because a
# few percent of oracle-vs-execution rate drift must not hide a leak.
retired_seen = state.setdefault("retired_seen", {})
# migration: drop banked entries whose category is no longer retired (the
# 71+ old `equip` entries from the $1 era are system free top-ups now)
retired_seen = {k: v for k, v in retired_seen.items() if v.get("cat") in RETIRED}
_new_retired = []
_CANDIDATE_CUT = now - timedelta(days=30)
for ts, usd_live, sym, qty, cp, key, usd_pinned, ts_iso, _grp in flows["out"]:
    # candidate window: rows older than 30d never (re-)alert — running totals
    # come from the chain-recomputed ledger, so old state entries are trimmed
    # without losing the audit trail
    # date-based on BOTH sides (banking and trim) so a boundary-day row can
    # never be trimmed and re-banked in a loop (QA finding, loop 3)
    if ts.strftime("%Y-%m-%d") <= _CANDIDATE_CUT.strftime("%Y-%m-%d"):
        continue
    for cat, rule in RETIRED.items():
        # ROW timestamp vs the same instant the classifier uses
        if ts_iso <= rule["cutoff"] or key in retired_seen:
            continue
        if abs(usd_pinned - rule["point"]) / rule["point"] <= rule["tol"]:
            retired_seen[key] = {"usd": round(usd_pinned, 2), "d": ts.strftime("%Y-%m-%d"),
                                 "cat": cat, "to": cp}
            _new_retired.append((cat, usd_pinned, cp, key, ts))
# trim dedup state to the candidate window; reconcile vs the chain ledger
retired_seen = {k: v for k, v in retired_seen.items()
                if v.get("d", "0000-00-00") > _CANDIDATE_CUT.strftime("%Y-%m-%d")}
state["retired_seen"] = retired_seen
_gp = os.path.join(HERE, "guard_private.json")
_guard = json.load(open(_gp)) if os.path.exists(_gp) else {}
_ledger = _guard.get("retired_ledger") or {}
if _new_retired:
    _tot_n = sum(L.get("n", 0) for L in _ledger.values()) or len(retired_seen)
    _tot_usd = sum(L.get("usd", 0) for L in _ledger.values()) or sum(v["usd"] for v in retired_seen.values())
    _cats = ", ".join(sorted({RETIRED_LABEL.get(c, c) for c, *_ in _new_retired}))
    lines += ["", f"🧟 <b>Retired payout category fired:</b> {len(_new_retired)} new <i>{_cats}</i>-sized transfer(s), latest {cap_detect.hkt(max(x[4] for x in _new_retired))}",
              *[f"  · ${u:,.2f} → {cp[:8]}…{cp[-4:]} · <code>{k.split(':')[0]}</code>"
                for _c, u, cp, k, _t in _new_retired[:5]],
              f"  · running total since retirement (chain-recomputed): <b>{_tot_n} transfers ≈ ${_tot_usd:,.2f}</b> — v1 invoke reward re-enabled? (retired 21 Aug); check the reward job config"]
if _ledger and not _new_retired:
    _led_recent = sum(1 for L in _ledger.values() for e in L.get("entries", [])
                      if e["ts"][:10] > _CANDIDATE_CUT.strftime("%Y-%m-%d"))
    if _led_recent != len(retired_seen):
        warn_lines += ["", f"⚠️ <b>Retired-ledger mismatch:</b> chain ledger has {_led_recent} straggler(s) in the last 30d, tripwire state has {len(retired_seen)} — check guard_private.json"]

# --- 4b. system free top-up (≈$1): one per wallet (private invariant) ---
# The system free top-up is legitimate but shape-constrained. Count per wallet
# across the FULL chain-recomputed ledger; a wallet's 2nd top-up fires once.
# State key migrated 2026-09-15 (legacy_seen -> system_topup_seen): the
# banked dedup entries are carried over so the rename cannot re-alert.
if "legacy_seen" in state and "system_topup_seen" not in state:
    state["system_topup_seen"] = state.pop("legacy_seen")
state.pop("legacy_seen", None)
legacy_seen = state.setdefault("system_topup_seen", {})
_leg_ledger = _guard.get("system_topup_ledger") or {}
_new_legacy = []
for cat, rule in SYSTEM_TOPUP.items():
    _entries = (_leg_ledger.get(cat) or {}).get("entries") or []
    _per = {}
    for e in sorted(_entries, key=lambda e: e["ts"]):
        _per.setdefault(e["to"].lower(), []).append(e)
    for addr, es in _per.items():
        for nth, e in enumerate(es, 1):
            if nth <= rule["max_per_wallet"]:
                continue
            key = f'{e["tx"]}:{e.get("li", "")}'
            if key in legacy_seen or e["ts"][:10] <= _CANDIDATE_CUT.strftime("%Y-%m-%d"):
                continue
            legacy_seen[key] = {"usd": e["usd"], "d": e["ts"][:10], "cat": cat, "to": e["to"], "nth": nth}
            _new_legacy.append((addr, nth, e))
legacy_seen = {k: v for k, v in legacy_seen.items()
               if v.get("d", "0000-00-00") > _CANDIDATE_CUT.strftime("%Y-%m-%d")}
state["system_topup_seen"] = legacy_seen
_ord = {2: "2nd", 3: "3rd"}
for addr, nth, e in _new_legacy[:5]:
    lines += ["", f"🧟 <b>Repeat system free top-up to one wallet:</b> {addr[:6]}…{addr[-4:]} received its "
                  f"{_ord.get(nth, f'{nth}th')} $1 system free top-up on "
                  f"{cap_detect.hkt(datetime.fromisoformat(e['ts']))} (rule: one per wallet) — "
                  f"top-up job config leak? check the platform's free-credit rule"]
_leg_rep = {a for L in _leg_ledger.values() for a in L.get("repeat_wallets", [])}
if _leg_ledger and not _new_legacy:
    _seen_rep = {v["to"].lower() for v in legacy_seen.values()}
    if _leg_rep - _seen_rep:
        warn_lines += ["", f"⚠️ <b>System-top-up ledger mismatch:</b> chain ledger lists {len(_leg_rep)} repeat wallet(s), "
                      f"tripwire state knows {len(_seen_rep)} — check guard_private.json"]

# --- 4c. A5 edge alerts (2026-09-15): the one treasury event of the day,
# edge-triggered with a 24 h cooldown, thresholds chosen against a replay of
# the last 14 days of real shards (1-15 Sep) so routine batches stay silent:
#   credit-grant spike   trailing-24h credit grants (credit_grants +
#                        system_topups via group_for) >= $20,000 AND >= 3x
#                        the prior 24 h. Replay max: $13,483 / 33x (11 Sep
#                        batch); three batches of 10-33x in the window are
#                        NORMAL, so a ratio rule alone can never be quiet —
#                        the $ floor is what makes this a runaway-job alarm,
#                        and routine batches show in the daily digest instead.
#   first-ever surge     >= 10 wallets received their first-ever skill reward
#                        in the trailing 24 h AND they are > 50% of the
#                        wallets paid in that window. Replay max: 4 of 52.
CREDIT_SPIKE_USD = 20000
CREDIT_SPIKE_RATIO = 3.0
NEW_WALLET_MIN = 10
NEW_WALLET_SHARE = 0.5
EDGE_COOLDOWN_H = 24
_edge = state.setdefault("edge", {})
_c24, _c48 = now - timedelta(hours=24), now - timedelta(hours=48)
_cg = lambda lo, hi: sum(r[6] for r in flows["out"] if lo < r[0] <= hi and r[8] in ("credit_grants", "system_topups"))
_cg24, _cgprev = _cg(_c24, now), _cg(_c48, _c24)
_cgn24 = sum(1 for r in flows["out"] if _c24 < r[0] <= now and r[8] in ("credit_grants", "system_topups"))
_spike = _cg24 >= CREDIT_SPIKE_USD and (_cgprev <= 0 or _cg24 / _cgprev >= CREDIT_SPIKE_RATIO)
_st = _edge.get("credit_spike", {})
_last = datetime.fromisoformat(_st["last_alert"]) if _st.get("last_alert") else None
if _spike and not _st.get("above") and (_last is None or now - _last >= timedelta(hours=EDGE_COOLDOWN_H)):
    _edge["credit_spike"] = {"above": True, "last_alert": now.isoformat(timespec="minutes")}
    _ratio = f"{_cg24 / _cgprev:,.0f}x" if _cgprev > 0 else "∞"
    lines += ["", f"⚠️ <b>Credit grants {_ratio} vs prior 24h:</b> ${_cg24:,.0f} ({_cgn24:,} grants) vs ${_cgprev:,.0f} — "
                  f"above the ${CREDIT_SPIKE_USD:,} runaway floor (window to {cap_detect.hkt(now)}); "
                  f"check the credit job is a scheduled campaign, not a loop"]
else:
    _edge["credit_spike"] = {"above": _spike, "last_alert": _st.get("last_alert")}
_first = {}
for r in sorted((r for r in flows["out"] if r[8] == "skill_rewards"), key=lambda r: r[0]):
    _first.setdefault(r[4].lower(), r[0])
_paid24 = {r[4].lower() for r in flows["out"] if r[0] > _c24 and r[8] == "skill_rewards"}
_new24 = sum(1 for a in _paid24 if _first.get(a, now) > _c24)
_surge = _new24 >= NEW_WALLET_MIN and _paid24 and _new24 / len(_paid24) > NEW_WALLET_SHARE
_st = _edge.get("new_wallets", {})
_last = datetime.fromisoformat(_st["last_alert"]) if _st.get("last_alert") else None
if _surge and not _st.get("above") and (_last is None or now - _last >= timedelta(hours=EDGE_COOLDOWN_H)):
    _edge["new_wallets"] = {"above": True, "last_alert": now.isoformat(timespec="minutes")}
    lines += ["", f"⚠️ <b>First-ever creator wallets {_new24} of {len(_paid24)} paid (24h):</b> "
                  f"{_new24 / len(_paid24):.0%} of the wallets paid a skill reward in the window to {cap_detect.hkt(now)} "
                  f"had never earned before — the 21-Aug farm pattern started this way; review the set in guard_private.json"]
else:
    _edge["new_wallets"] = {"above": bool(_surge), "last_alert": _st.get("last_alert")}
print(f"edge: credit grants 24h ${_cg24:,.0f} vs prior ${_cgprev:,.0f} · first-ever wallets {_new24}/{len(_paid24)}")

# --- 5. data-source degradation edge alert (council item 4b) ---
# complete=False means the run rendered from a stale/partial cache. The page
# shows it quietly; Telegram gets ONE line on the flip and one on recovery.
_src_ok = bool(DATA["scope"].get("complete", True))
_was_ok = state.get("source_ok", True)
if _was_ok and not _src_ok:
    warn_lines += ["", "🟠 <b>Data source degraded:</b> this refresh rendered with incomplete chain data (cache fallback) — figures may lag until the source recovers"]
elif _src_ok and not _was_ok:
    warn_lines += ["", "🟢 <b>Data source recovered:</b> chain fetch complete again"]
state["source_ok"] = _src_ok

# --- 5b. treasury runway edge alert (item 4, 2026-09-21: the council's
# "funding cliff" finding). facts.float.days_7d_pace below 7, then below 3;
# per-threshold edge, 24 h cooldown, re-armed on recovery. ~9.7 d on the
# day this shipped -> silent today. PAGE tier: to the funding owner at once.
_FL = DATA["facts"].get("float") or {}
_rw_secs, state = cap_detect.runway_check(_FL.get("days_7d_pace"), _FL.get("bal_usd") or 0,
                                          _FL.get("driver_24h"), state, now)
print(f"runway: {_FL.get('days_7d_pace')} d at the 7d pace · thresholds {[t for t, _ in cap_detect.RUNWAY_THRESHOLDS]}"
      + (" · FIRED" if _rw_secs else ""))

# --- 6. creator-reward cap / sybil detectors C1-C6 (cap_detect.py) ---
# Rows: outflow rows since the v2 resume, day-pinned USD, era-aware fine
# class in {equip, invoke}. Units are the counter; USD is display only.
# wallets LOWERCASED: rows arrive EIP-55 from Blockscout and lowercase from
# the eth_getLogs legs, and a per-wallet counter split by casing would
# under-count exactly the wallet that matters (refresh.py canonicalises the
# same way via canon())
_rrows = cap_detect.reward_rows((r[0], r[6], r[4].lower(), r[7]) for r in flows["out"])
_sw = cap_detect.sweep(_rrows, now)
cap_sections, state = cap_detect.evaluate(_sw, state, now)
_or_secs, state = cap_detect.oracle_check(_guard.get("grid_agreement"), state, now,
                                          now.strftime("%Y-%m-%d"))
cap_sections += _or_secs + _rw_secs
# the oracle RECOVERY line is informational -> WARN tier (next digest); the
# disagreement itself stays PAGE (a wrong day rate doubles every USD figure)
_warn_secs = [sec for sec in cap_sections if any("Oracle agreement restored" in ln for ln in sec)]
cap_sections = [sec for sec in cap_sections if sec not in _warn_secs]
# Heartbeat: written NOW, unconditionally — a failed Telegram send below must
# not erase the proof that the detector ran (alive_check.py's dead-man).
cap_probe = cap_detect.probe(_sw, now)
state["cap_probe"] = cap_probe
_statemod.update({"cap_probe": cap_probe})
# per-creator review table -> the PRIVATE file only (merge, refresh.py owns
# the rest of it and rewrites it every run)
if os.path.exists(_gp):
    _guard["cap_table"] = cap_detect.cap_table(_sw)
    _guard["fanout_hours"] = {h: v for h, v in _sw["hours"].items() if v["ge20"] >= 5 or v["ge40"] >= 2}
    _tmp = _gp + ".tmp"
    json.dump(_guard, open(_tmp, "w"))
    os.replace(_tmp, _gp)
print(f"cap probe: rows {cap_probe['rows']} · creators 1h {cap_probe['creators_1h']} · "
      f"top {cap_probe['top_units_1h']} units (${cap_probe['top_usd_1h']}) · at cap {cap_probe['n_at_cap']}")

# Persist state only AFTER a successful send (QA finding, 2026-08-29):
# persisting first meant a failed Telegram call permanently suppressed every
# alert of the run. Now a failed send leaves state untouched, so the next run
# re-alerts — the worst case is a duplicate, never silence.
def persist_state():
    # keep 'seen' bounded: only keys that can still re-trigger (last 48h)
    recent = {r[5] for rows_ in flows.values() for r in rows_
              if r[0] > now - timedelta(hours=48)}
    st = {**state, "seen": sorted(seen & recent), "anomaly": anom}
    # send_health is owned by state.record_send (item 4): the copy loaded at
    # the top of this run is stale by send time — writing it back here would
    # clobber the outcome just recorded. warn_queue likewise (state.warn).
    st.pop("send_health", None)
    st.pop("warn_queue", None)
    _statemod.update(st, drop=("legacy_seen",))


def _sections(ls):
    """alerts blocks are '' separated — regroup into sections for compose()."""
    out, cur = [], []
    for ln in ls:
        if ln == "" and cur:
            out.append(cur)
            cur = []
        cur.append(ln)
    if cur:
        out.append(cur)
    return out


# ---- WARN tier: never its own message. Queued (one per key per 6 h) and
# carried by the next hourly/daily digest (notify.py drains the queue).
_WARN_KEY = (("Rebate wallet swap overdue", "rebate_swap"), ("Retired-ledger mismatch", "retired_mismatch"),
             ("System-top-up ledger mismatch", "topup_mismatch"), ("Data source degraded", "source"),
             ("Data source recovered", "source"), ("Oracle agreement restored", "oracle_restored"))
for sec in _warn_secs + _sections(warn_lines):
    text = " ".join(ln.strip() for ln in sec if ln.strip())
    key = next((k for needle, k in _WARN_KEY if needle in text), "misc")
    if _statemod.warn("alerts:" + key, text, now):
        print(f"WARN queued [{key}] for the next digest")
    else:
        print(f"WARN [{key}] inside its 6 h cooldown — not re-queued")

all_sections = cap_sections + _sections(lines)
if all_sections:
    msg = cap_detect.compose("🚨 <b>Flow alert</b> — <i>Skill Payout Dashboard</i> · " + cap_detect.hkt(now),
                             all_sections)
    body = urllib.parse.urlencode({
        "chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": msg, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
        "reply_markup": json.dumps({"inline_keyboard": [[{"text": "📊 Open dashboard", "url": URL}]]}),
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage", data=body)
    # Liveness bookkeeping (Cycle-3 Loop 2, item 4): record the ATTEMPT's
    # outcome either way; 3 consecutive failures turn refresh.yml red via
    # alive_check.py. The send itself stays continue-on-error in the workflow
    # (redline) — record-then-reraise preserves that behaviour exactly.
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print("alert sent:", r.status)
    except Exception:
        _statemod.record_send("alerts", False, now)
        raise
    _statemod.record_send("alerts", True, now)
    persist_state()
else:
    persist_state()
    print("no alerts")

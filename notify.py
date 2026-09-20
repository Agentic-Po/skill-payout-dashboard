#!/usr/bin/env python3
"""Send a rich-format (HTML) Telegram status update with deltas.

Usage: notify.py hourly|daily|weekly [--dry-run]
Deltas are computed directly from the transfers/ shard timestamps, so they are
exact from the very first message — no snapshot warm-up needed.
Every timestamp in the message is HKT (Po's clock); UTC stays in logs and
public artifacts. --dry-run prints the message and exits without sending or
touching alert_state.json.
Env vars: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.
"""
import json, os, sys, urllib.request, urllib.parse
import shards
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
URL = "https://agentic-po.github.io/skill-payout-dashboard/"
_args = [a for a in sys.argv[1:] if not a.startswith("--")]
mode = _args[0] if _args else "hourly"
DRY_RUN = "--dry-run" in sys.argv[1:]

hist = json.load(open(os.path.join(HERE, "stats_history.json")))
now = datetime.now(timezone.utc).replace(tzinfo=None)
HKT = timedelta(hours=8)


def hkt(dt, fmt="%d %b %H:%M"):
    """HKT rendering for every timestamp that reaches Telegram."""
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.rstrip("Z")[:19])
    return (dt + HKT).strftime(fmt) + " HKT"


def hkt_day(day):
    """A UTC day label ('2026-09-14') -> '14 Sep' (its noon is the same HKT date)."""
    return (datetime.fromisoformat(day) + timedelta(hours=12) + HKT).strftime("%d %b")


# data.json is the versioned contract with refresh.py — no HTML scraping.
# Fail LOUD on absence or staleness: a silent fallback to stale numbers is
# how the wallet-balance line went dark for days in July.
_dj = os.path.join(HERE, "data.json")
if not os.path.exists(_dj):
    raise SystemExit("FATAL: data.json missing — refresh.py must run first")
_D = json.load(open(_dj))
_gen = _D.get("scope", {}).get("generated_iso")
if not _gen:
    raise SystemExit("FATAL: data.json has no generated_iso — pre-contract file, refusing to send")
# TWO freshness legs, because they answer different questions and the QA pass
# (2026-08-30) caught the swap that dropped one of them:
#   1. rows.require_fresh on catalog.json's `data` entry — the PIPELINE is
#      alive (catalog.json is rebuilt at the end of every refresh; a missing
#      catalog is itself a broken pipeline and refuses).
#   2. data.json's own scope.generated_iso — the FIGURES about to be sent are
#      fresh. Leg 1 alone is not enough: `python3 catalog.py` run by hand, or
#      a partial refresh that rebuilds only the catalog, resets leg 1's clock
#      while data.json stays arbitrarily stale (17 min of decoupling was
#      already visible in the committed tree).
import rows as _rows
if not DRY_RUN:
    try:
        _rows.require_fresh(os.path.join(HERE, "catalog.json"), "data", 2, field="generated_iso")
    except _rows.StaleData as e:
        raise SystemExit(f"FATAL: {e} — refusing to send outdated figures")
_gen_age_h = (now - datetime.fromisoformat(_gen.rstrip("Z")[:19])).total_seconds() / 3600
if _gen_age_h > 2 and not DRY_RUN:
    raise SystemExit(f"FATAL: data.json is {_gen_age_h:.1f}h old (generated_iso {_gen}, "
                     f"limit 2h) — refusing to send outdated figures")
F = _D["facts"]
G = _D["infer"]["guard"]
FL = F.get("float") or {}
TOKENS = {a.lower(): s for s, a in _D["scope"]["tokens"].items()}   # addr -> sym
RATE = F["rate"].get("MOCA") or hist[-1]["rate"]

# Payout taxonomy lives in classify.py — the ONE classifier shared with the
# page (refresh.py) and alerts.py. Rows are priced at the DAY-PINNED rate per
# token (day_rates.json, incl. today's provisional open_day_rate) so history
# can't reprice with the market; the live per-token rate is the last resort.
# The classifier is ERA-AWARE: every call passes the row's own timestamp.
# Every rollup below goes through classify.group_for — the ONE grouping layer
# (2026-09-15): no category is hand-listed here, and test_parity recomputes
# the same sums the page publishes.
from classify import (classify_usd, INCENT, pin_rate, era_for, RESUMED_UTC, SYSTEM_TOPUP, UNITS,
                      group_for, GROUP_KEYS, GROUP_LABEL)
_dr_state = json.load(open(os.path.join(HERE, "day_rates.json")))
_DAY_RATES = {sym: dict(_dr_state["day_rates"].get(sym, {})) for sym in TOKENS.values()}
for sym, od in (_dr_state.get("open_day_rate") or {}).items():
    _DAY_RATES.setdefault(sym, {}).setdefault(od["d"], od["rate"])

def classify(ts, sym, v):
    """-> (coarse, usd, tier, fine, group). ts is the row's ISO timestamp
    (day-pins the rate AND selects the reward era)."""
    _fb = F["rate"].get(sym)
    if not _fb and not _DAY_RATES.get(sym):
        raise SystemExit(f"FATAL: no rate available for {sym} — refusing to price rows at $0")
    usd = v * pin_rate(_DAY_RATES.get(sym, {}), ts[:10], _fb or 0)
    coarse, fine, tier = classify_usd(usd, ts)
    return coarse, usd, tier, fine, group_for(coarse, fine)

# ALL tracked tokens (council loop 3: the digest previously classified only
# MOCA, silently excluding the entire MENTE era from the all-time figures
# while the page counted both — the one-figure-everywhere failure).
rows = []          # (ts, cat, qty, wallet, tier, usd_day_pinned, sym, fine, group)
for i in shards.load(os.path.join(HERE, "transfers")):
    sym = TOKENS.get(i["token"]["address_hash"].lower())
    if not sym:
        continue
    v = int(i["total"]["value"]) / 1e18
    ts_iso = i["timestamp"][:19]
    cat, usd, tier, fine, grp = classify(ts_iso, sym, v)
    # wallet lowercased: Blockscout rows are EIP-55, eth_getLogs rows are
    # lowercase, and a distinct-wallet count split by casing overstated
    # "creator wallets paid" vs the page (56 vs 47 on 2026-09-15)
    rows.append((datetime.fromisoformat(ts_iso), cat, v, i["to"]["hash"].lower(), tier, usd, sym, fine, grp))
RESUMED = datetime.fromisoformat(RESUMED_UTC)
TOPUP_FINE = SYSTEM_TOPUP["topup1"]["fine"]
# first-ever skill reward per wallet, all history (for "new creator wallets")
_first_reward = {}
for r in sorted((r for r in rows if r[8] == "skill_rewards"), key=lambda r: r[0]):
    _first_reward.setdefault(r[3], r[0])

def win(hours=None):
    """Activity inside the trailing window (None = all history).

    Group sums come from classify.group_for; creator wallets are DISTINCT
    recipient wallets of a skill-reward-sized row; tiers_* map $ size -> count.
    """
    lo = None if hours is None else now - timedelta(hours=hours)
    rs = rows if lo is None else [r for r in rows if r[0] > lo]
    def tiers(grp):
        out = {}
        for r in rs:
            if r[8] == grp and r[4]:
                out[r[4]] = out.get(r[4], 0) + 1
        return dict(sorted(out.items()))
    g_usd = {g: sum(r[5] for r in rs if r[8] == g) for g in GROUP_KEYS}
    g_n = {g: sum(1 for r in rs if r[8] == g) for g in GROUP_KEYS}
    cw = {r[3] for r in rs if r[8] == "skill_rewards"}
    return {
        "invoke": sum(1 for r in rs if r[1] == "invoke"),
        "equip": sum(1 for r in rs if r[1] == "equip"),
        "creators": len(cw),
        "new_creators": sum(1 for a in cw if lo is not None and _first_reward.get(a, now) > lo),
        "g_usd": g_usd, "g_n": g_n,
        "out_usd": sum(r[5] for r in rs),
        "qty_ce": _qty(rs, "skill_rewards"), "qty_credit": _qty(rs, ("credit_grants", "system_topups")),
        "qty_topup": _qty(rs, "topups_delivered"),
        "tiers_credit": {**tiers("credit_grants"), **tiers("system_topups")},
        "tiers_topup": tiers("topups_delivered"),
        # Creator Rewards v2 (fine == equip/invoke on the v2 grid, since the resume)
        "v2_equip": sum(1 for r in rs if r[7] == "equip" and r[0] >= RESUMED),
        "v2_invoke": sum(1 for r in rs if r[7] == "invoke" and r[0] >= RESUMED),
        "v2_usd_equip": sum(r[5] for r in rs if r[7] == "equip" and r[0] >= RESUMED),
        "v2_usd_invoke": sum(r[5] for r in rs if r[7] == "invoke" and r[0] >= RESUMED),
        "v2_creators": len({r[3] for r in rs if r[7] in UNITS and r[0] >= RESUMED}),
        "system_topup_n": g_n["system_topups"],
    }

def _qty(rs_, groups):
    groups = (groups,) if isinstance(groups, str) else groups
    q = {}
    for r in rs_:
        if r[8] in groups:
            q[r[6]] = q.get(r[6], 0) + r[2]
    return q

def mix(tiers, labels=None):
    """'23 x $10 - 24 x $20' — biggest first, blank when empty."""
    if not tiers: return ""
    top = sorted(tiers.items(), key=lambda kv: -kv[1])
    return " · ".join(f"{n:,} × {labels[p] if labels else '$%d' % p}" for p, n in top)

INCENT_LABEL = dict(INCENT)

w1, w24, w7d, cum = win(1), win(24), win(24 * 7), win()
# the window the body reports on: 24h hourly/daily, 7d weekly
W, WLAB = (w7d, "7d") if mode == "weekly" else (w24, "24h")

# reward-size labels follow the CURRENT era (classify.ERAS) — the next price
# change is a table edit there, not here
_ERA = era_for(now.isoformat(timespec="minutes"))
def _sz(name):
    p = _ERA["rewards"].get(name)
    return f" (${p:g})" if p else ""

def usd_line(label, key, w, note=""):
    # $ figure is the sum of day-pinned per-row USD — same basis as the page.
    # Raw amounts render per token (MENTE-era rows are now included).
    grp = {"qty_ce": "skill_rewards", "qty_topup": "topups_delivered"}.get(key)
    u = w["g_usd"][grp] if grp else w["g_usd"]["credit_grants"] + w["g_usd"]["system_topups"]
    qty = " + ".join(f"{q:,.0f} {s}" for s, q in sorted(w[key].items(), key=lambda kv: -kv[1]) if q) or "0"
    return f"<b>{label}:</b> {qty} ≈ <b>${u:,.2f}</b>{note}"

head = {"hourly": "🟢 <b>Hourly refresh OK</b>",
        "daily": "📊 <b>Daily summary</b>",
        "weekly": "🗓 <b>Weekly summary</b>"}[mode]

import state as _state
_ST = _state.load()
# C4 (2026-09-15): monitoring-status counts come from alert_state.json
# (PRIVATE, written by refresh.py), never from data.json
_PM = _ST.get("pattern_monitor") or {}

# ---- float line (X5/X8): ONE runway on TOTAL outflow (facts.float), the
# driver named. Emoji is the severity; the line is always present.
def float_line():
    d7, d24 = FL.get("days_7d_pace"), FL.get("days_24h_pace")
    worst = min(x for x in (d7, d24) if x is not None) if (d7 or d24) else None
    icon = "🔴" if worst is not None and worst < 7 else ("🟠" if worst is not None and worst < 14 else "🟢")
    drv = FL.get("driver_24h") or {}
    return (f"{icon} <b>Float</b> ~{d24 if d24 is not None else '?'}d (24h pace) · {d7 if d7 is not None else '?'}d (7d pace) · "
            f"${FL.get('bal_usd', G.get('bal_usd', 0)):,.0f} · out ${FL.get('out_24h_usd', 0):,.0f}/24h"
            + (f", {drv['share_pct']:g}% {drv['label'].lower()}" if drv else ""))

health = []
w24_f = F["windows"][0]
bal_m, bal_e = F["balance"].get("MOCA"), F["balance"].get("MENTE")
# Wallet line is a hard contract of every message: if the live fetch failed
# (balance null), fall back to the last non-null snapshot in stats_history
# and mark it stale — never silently drop the line.
stale_ts = None
if bal_m is None or bal_e is None:
    for snap in reversed(hist):
        if snap.get("balance") is not None:
            bal_m = bal_m if bal_m is not None else snap["balance"]
            bal_e = bal_e if bal_e is not None else snap.get("mente_balance")
            stale_ts = snap["ts"]
            break
r_m, r_e = F["rate"].get("MOCA") or RATE, F["rate"].get("MENTE") or 0
usd_m = (bal_m or 0) * r_m
usd_e = (bal_e or 0) * r_e
stale = f" ⚠️ <i>(live fetch failed — last known {hkt(stale_ts)})</i>" if stale_ts else ""
out_1h = sum(r[5] for r in rows if r[0] > now - timedelta(hours=1))
if mode != "hourly":
    health.append("")
    health.append("🔧 <b>Ops health</b>")
    health.append(f"<b>Wallet balance:</b> <b>${usd_m + usd_e:,.0f}</b> total{stale}")
    health.append(f"  · MOCA: {bal_m or 0:,.0f} ≈ <b>${usd_m:,.2f}</b>")
    health.append(f"  · MENTE: {bal_e or 0:,.0f} ≈ <b>${usd_e:,.2f}</b>")
    # Economy vs ops split: economy = every group but ops (closes on the
    # message itself); ops = the residual, never printed negative.
    # both terms from data.json's OWN 24h window (facts_window.groups) so the
    # line closes on itself even when this digest runs minutes after the build
    _ops24 = (w24_f.get("groups") or {}).get("ops", {}).get("usd", 0.0)
    _eco24 = w24_f["out_usd"] - _ops24
    _ops_s = (f"ops out ${_ops24:,.0f} <i>(swaps/treasury)</i>" if _ops24 >= 0
              else f"ops out $0 <i>(⚠ basis mismatch ${_ops24:,.0f})</i>")
    health.append(f"<b>Flows:</b> out ${w24_f['out_usd']:,.0f} 24h = economy ${_eco24:,.0f} + {_ops_s} · in ${w24_f['in_usd']:,.0f} 24h · net {'+' if w24_f['net_usd']>=0 else ''}${w24_f['net_usd']:,.0f}")
    # A5: last external funding (facts.in_ledger, recycled leg excluded)
    _ext = [l for l in (F.get("in_ledger") or []) if (l.get("label") or "") != "Cognition Credits collector — also the original SWARM-era treasury+collector hub (pre-Apr 2026)"]
    if _ext:
        _lf = _ext[0]
        _age = (now.date() - datetime.fromisoformat(_lf["day"]).date()).days
        health.append(f"<b>Last funding:</b> {hkt_day(_lf['day'])} · ${_lf['usd']:,.0f} ({_lf['tok']}) · {_age}d ago")
    if _PM:
        health.append(f"<b>Pattern monitor (private):</b> {_PM.get('flagged_n', '?')} of {_PM.get('monitored_n', '?')} wallets flagged · "
                      f"<b>at risk:</b> ${_PM.get('at_risk_usd', 0):,.2f} of ${_PM.get('ce_total_usd', 0):,.2f} <i>(heuristic, unconfirmed; as of {hkt(_PM['ts']) if _PM.get('ts') else '?'})</i>")
    else:
        health.append("<b>Pattern monitor (private):</b> <i>no state banked yet (cold cache)</i>")
    # Alert-delivery liveness (Cycle-3 Loop 2, item 4): counts come from
    # alert_state.json's send_health (Actions cache) — digest-only, never a
    # public artifact. A stretch of failures also turns refresh.yml red via
    # alive_check.py; this line keeps the trend visible even below threshold.
    _sh = _ST.get("send_health") or {}
    _cut24 = (now - timedelta(hours=24)).isoformat(timespec="minutes")
    _n_sent = sum(1 for c in _sh.values() for t in c.get("sent", []) if t > _cut24)
    _n_fail = sum(1 for c in _sh.values() for t in c.get("failed", []) if t > _cut24)
    health.append(f"<b>alerts:</b> {_n_sent} sent / {_n_fail} failed (24h)")
    # Item 3 (2026-09-21): ONE private line from sanity.py's anomaly pass
    # (guard_private.json, Actions cache) — per-wallet grant counts never
    # reach a public artifact; the chain shows wallets, not accounts.
    if mode == "daily":
        _gp = os.path.join(HERE, "guard_private.json")
        try:
            _rg = (json.load(open(_gp)).get("repeat_grants") or {}) if os.path.exists(_gp) else {}
        except ValueError:
            _rg = {}
        if _rg:
            health.append(f"<b>Repeat grants (private):</b> {_rg.get('repeat_wallets', 0):,} wallets took >1 "
                          f"(${_rg.get('repeat_usd', 0):,.0f}, {_rg.get('repeat_share_pct', 0):g}% of grant spend); "
                          f"heaviest {_rg.get('heaviest_grants', 0):,} grants")
        else:
            health.append("<b>Repeat grants (private):</b> <i>not banked yet (sanity.py has not run on this cache)</i>")
        # loop 2, item 3: the slow-bleed measurement (fences.grant_bleed, banked
        # by sanity.py) — ONE private line, never a page, never a public field
        try:
            _gb = (json.load(open(_gp)).get("grant_bleed") or {}) if os.path.exists(_gp) else {}
        except ValueError:
            _gb = {}
        if _gb:
            import fences as _fences
            health.append(_fences.bleed_line(_gb))
if mode == "weekly":
    health.append("")
    _ss = _D.get("stripe_snap") or {}
    _ver = (f" Verified Stripe net: ${_ss.get('net_usd', 0):,.0f} ({_ss['period'][0]}→{_ss['period'][1]}, one-time snapshot)."
            if _ss.get("net_usd") and _ss.get("period") else "")
    _flag_s = f"{_PM['flagged_n']} wallet(s) flagged for private review" if _PM else "pattern monitor state not banked"
    health.append(f"<i>Paste-ready:</i> This week: {w7d['invoke']:,} invokes and {w7d['equip']:,} equips across {w7d['creators']:,} creator wallets, ${w7d['g_usd']['skill_rewards']:,.2f} of skill rewards paid — {_flag_s}. ${w7d['g_usd']['topups_delivered']:,.2f} of flows were Stripe-pack-sized deliveries (size-inferred; may include coupon-delivered credits — not verified revenue).{_ver}")
    # --- ops footer (Cycle-3 Loop 3, item 3) ---
    # DIGEST-ONLY, no new public fields. Cadence comes from data already
    # public (stats_history.json timestamps — one snapshot per successful
    # refresh, expected 4/hour); run duration comes from alert_state.json's
    # refresh_runs (Actions cache, PRIVATE — recorded by refresh.py since
    # this loop; "(collecting)" until it has data / a full 7 days).
    # refresh.yml's GITHUB_TOKEN lacks actions:read by design (amended
    # verdict 14) — no gh API call is made here.
    _c7d_iso = (now - timedelta(days=7)).isoformat(timespec="minutes")
    _n_runs = sum(1 for _h in hist if _h.get("ts", "") > _c7d_iso)
    _exp_runs = 4 * 24 * 7                      # refresh.yml fires 4x/hour
    _uptime = min(100.0, _n_runs / _exp_runs * 100)
    _rr = [r for r in (_ST.get("refresh_runs") or [])
           if isinstance(r, dict) and r.get("ts", "") > _c7d_iso and r.get("dur_s") is not None]
    if _rr:
        _durs = sorted(r["dur_s"] for r in _rr)
        _span_d = (now - datetime.fromisoformat(_rr[0]["ts"])).total_seconds() / 86400
        _dur_s = f"last {_rr[-1]['dur_s']:.0f}s · median {_durs[len(_durs) // 2]:.0f}s over {len(_rr)} run(s)"
        if _span_d < 6.5:
            _dur_s += f" <i>(collecting — {_span_d:.1f}d of 7d banked)</i>"
    else:
        _dur_s = "<i>(collecting)</i>"
    health.append(f"🛠 <b>Refresh ops (7d):</b> {_n_runs}/{_exp_runs} scheduled runs published → "
                  f"uptime {_uptime:.1f}% · <b>run duration:</b> {_dur_s}")
# ---- alert lines: only when firing (every mode) ----
alerts = []
for sym, r in (F.get("recon") or {}).items():
    if r and r.get("warn"):
        alerts.append(f"⚠️ <b>Reconciliation drift ({sym}):</b> Δ moved {r['drift']:+,.1f} since last run — possible missed transfers")
if stale_ts:
    alerts.append(f"⚠️ <b>Balance:</b> live fetch failed — last known {hkt(stale_ts)}")
if not _D["scope"].get("complete", True):
    alerts.append("⚠️ <b>Chain fetch incomplete</b> this run — figures may lag")
if FL.get("out_prev24_usd", 0) > 0 and FL.get("out_24h_usd", 0) / FL["out_prev24_usd"] > 2 and mode != "hourly":
    alerts.append(f"⚠️ <b>Outflow accelerating:</b> ${FL['out_24h_usd']:,.0f}/24h vs ${FL['out_prev24_usd']:,.0f} prior")
# cap probe (PRIVATE heartbeat from alerts.py) — per WALLET, a lower bound
cp = _ST.get("cap_probe") or {}
cp_fresh = bool(cp.get("ts")) and (now - datetime.fromisoformat(cp["ts"])).total_seconds() <= 2 * 3600
if not cp_fresh:
    alerts.append(f"⚠️ <b>Cap probe missing</b> — detector did not run (last {hkt(cp['ts']) if cp.get('ts') else 'never'})")
# ---- WARN tier (severity tiering, 2026-09-21): degraded-but-published
# notices queued by alerts.py / sanity.py / refresh.py ride alert_state.json
# and are carried HERE, in the next digest, never as their own message.
# Marked sent only after a successful send of a message that actually
# carried them (the hourly budget may push some to the daily).
_warns = _state.pending_warns(now)
_warn_text = {f"🟠 {w['text']}": w["key"] for w in _warns}
alerts += list(_warn_text)
health += [""] + alerts if alerts else []
_carried_warn_keys = [k for k in _warn_text.values()]


# ---- Creator Rewards v2 block (daily/weekly only since 2026-09-15) ----
# Counts/USD come from the same era-aware rows as every other line above.
# Cap headroom comes from alert_state.json's cap_probe heartbeat (PRIVATE —
# Actions cache, written by alerts.py every run). Po's private channel, so
# the cap figure and per-hour headroom ARE printed here; the public page and
# every public artifact carry no cap data at all. Per WALLET: the chain sees
# wallets, not accounts — every figure here is a lower bound per creator.
def v2_block(w, lab):
    out = ["", f"🎯 <b>Creator rewards v2 ({lab})</b>",
           f"  · <b>equips:</b> {w['v2_equip']:,} ≈ ${w['v2_usd_equip']:,.2f} · "
           f"<b>invokes:</b> {w['v2_invoke']:,} ≈ ${w['v2_usd_invoke']:,.2f} · "
           f"<b>creator wallets paid:</b> {w['v2_creators']:,} ({w['new_creators']:,} first-ever)"]
    if cp_fresh:
        cap = cp.get("cap_usd") or 0
        out.append(f"  · <b>Cap (60m, per wallet):</b> top wallet ${cp.get('top_usd_1h', 0):,.2f} / ${cap:,.2f} · "
                   f"{cp.get('creators_1h', 0)} wallets paid · {cp.get('n_at_cap', 0)} ≥90%")
        if mode == "daily":
            out.append(f"  · <b>Peak wallet-hour (24h):</b> ${cp.get('top_clock_usd_24h', 0):,.2f} / ${cap:,.2f} cap · "
                       f"{cp.get('n_at_90pct_24h', 0)} wallet(s) with a ≥90% hour")
        if mode == "weekly":
            _c7 = (now - timedelta(days=7)).isoformat(timespec="minutes")
            _breaches = sum(1 for v in (_ST.get("cap_hits") or {}).values() if v > _c7)
            _fan = sum(1 for v in ((_ST.get("cap_state") or {}).get("fanout") or {}).values() if v > _c7)
            out.append(f"  · <b>Cap (7d):</b> max wallet-hour ${cp.get('top_clock_usd_7d', 0):,.2f} / ${cap:,.2f} · "
                       f"{_breaches} breach(es) · {_fan} fan-out hour(s)")
    # concentration: the SAME public aggregate the page's creator card shows
    _cw = ((F.get("creator_wallets") or {}).get("windows") or {})
    _k = "7d" if mode == "weekly" else "24h"
    _c = _cw.get(_k) or {}
    if _c.get("wallets"):
        _med = f" · median ${_c['median_usd']:,.4f}" if _c.get("median_usd") is not None else ""
        out.append(f"  · <b>Concentration ({_k}):</b> top-1 {_c.get('top1_share_pct')}% · top-5 {_c.get('top5_share_pct')}% · "
                   f"top-10 {_c.get('top10_share_pct')}% of ${_c['usd']:,.2f} across {_c['wallets']} wallets{_med}")
    _sr = _cw.get("since_resume") or {}
    if _sr.get("wallets"):
        out.append(f"  · <b>Since resume ({hkt_day(RESUMED_UTC[:10])}):</b> ${_sr['usd']:,.2f} to {_sr['wallets']} creator wallets · "
                   f"{_sr['equips']:,} equips · {_sr['invokes']:,} invokes")
    # system free top-ups: the window count from the rows, the running total
    # from the SAME public aggregate the page shows (infer.system_topup_public)
    lp = next(iter(_D["infer"].get("system_topup_public") or []), None)
    if lp:
        since = hkt_day(lp["since"]) if lp.get("since") else "?"
        out.append(f"  · <b>System free top-ups ($1):</b> {w['system_topup_n']:,} ({lab}) · {lp['n']:,} since {since}")
    # pricing provenance (day_rates.json): last closed day + the open day
    dr = _dr_state["day_rates"].get("MOCA") or {}
    src = (_dr_state.get("day_rate_src") or {}).get("MOCA") or {}
    od = (_dr_state.get("open_day_rate") or {}).get("MOCA")
    parts = []
    if dr:
        last_d = max(dr)
        parts.append(f"MOCA day rate {dr[last_d]:.6f} ({hkt_day(last_d)}, {src.get(last_d, '?')})")
    parts.append(f"open day {od['rate']:.6f} (market-open)" if od else "open day carry-forward")
    c30 = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    cf = sum(1 for d, v in src.items() if c30 <= d < now.strftime("%Y-%m-%d") and v == "carry-forward")
    parts.append(f"{cf} carry-forward day(s) in 30d")
    out.append("  · <b>Pricing:</b> " + " · ".join(parts))
    return out


if mode == "hourly":
    # ---- X5: the phone-first hourly (≤ 700 chars, worst thing first) ----
    _g1, _n1 = w1["g_usd"], w1["g_n"]
    _g24, _n24 = w24["g_usd"], w24["g_n"]
    body_lines = [f"{head} — <i>Skill Payout Dashboard</i> · {hkt(now, '%d %b %Y %H:%M')}",
                  float_line(),
                  (f"📈 <b>1h</b> · rewards ${_g1['skill_rewards']:,.2f} ({w1['equip']:,} equips · {w1['invoke']:,} invokes → {w1['creators']:,} creator wallets) · "
                   f"credit grants {_n1['credit_grants'] + _n1['system_topups']:,} ≈ ${_g1['credit_grants'] + _g1['system_topups']:,.0f} · out ${out_1h:,.0f}"),
                  (f"📊 <b>24h</b> · rewards ${_g24['skill_rewards']:,.2f} → {w24['creators']:,} creator wallets · "
                   f"credit grants ${_g24['credit_grants'] + _g24['system_topups']:,.0f} ({_n24['credit_grants']:,} + {_n24['system_topups']:,} system free top-ups) · "
                   f"top-ups delivered {_n24['topups_delivered']:,} ≈ ${_g24['topups_delivered']:,.0f}")]
    if cp_fresh:
        body_lines.append(f"🎯 <b>Cap</b> (60m, per wallet) · top wallet ${cp.get('top_usd_1h', 0):,.2f} / ${cp.get('cap_usd', 0):,.2f} · {cp.get('n_at_cap', 0)} ≥90%")
    # budget: the four fixed lines are ~430 chars; alert lines are appended
    # while they fit, the rest collapse to a count — a bad day must never turn
    # into a silent (asserted-out) hourly
    HOURLY_MAX = 700
    kept = list(alerts)
    while kept and len("\n".join(body_lines + kept)) > HOURLY_MAX:
        kept.pop()
    if len(kept) < len(alerts):
        kept.append(f"… +{len(alerts) - len(kept)} more warning(s) in the daily")
        while len(kept) > 1 and len("\n".join(body_lines + kept)) > HOURLY_MAX:
            kept.pop(-2)
    msg = "\n".join(body_lines + kept)
    assert len(msg) <= HOURLY_MAX, f"hourly digest is {len(msg)} chars — the phone-first budget is {HOURLY_MAX}"
    _carried_warn_keys = [_warn_text[ln] for ln in kept if ln in _warn_text]
else:
    body_lines = [f"{head} — <i>Skill Payout Dashboard</i> · {hkt(now, '%d %b %Y %H:%M')}", "", float_line(), "",
                  "📈 <b>Economy</b>",
                  f"<b>Last {WLAB}</b> · out ${W['out_usd']:,.2f} = " + " · ".join(
                      f"{GROUP_LABEL[g].lower()} ${W['g_usd'][g]:,.2f}" for g in GROUP_KEYS if W["g_n"][g]),
                  f"  · skill rewards: {W['invoke']:,} invokes{_sz('invoke')} · {W['equip']:,} equips{_sz('equip')} → {W['creators']:,} creator wallets ({W['new_creators']:,} first-ever)",
                  "  · " + usd_line("paid to creator wallets", "qty_ce", W),
                  "  · " + usd_line("credit grants", "qty_credit", W,
                                    f" <i>({mix(W['tiers_credit'], INCENT_LABEL)})</i>" if W["tiers_credit"] else ""),
                  "  · " + usd_line("top-ups delivered", "qty_topup", W,
                                    f" <i>({W['g_n']['topups_delivered']:,} paid: {mix(W['tiers_topup'])})</i>" if W["tiers_topup"]
                                    else " <i>(Stripe-pack-sized, size-inferred)</i>")]
    if W["g_n"]["ops"]:
        body_lines.append(f"  · <i>excluded {W['g_n']['ops']:,} non-standard transfer(s) ≈ ${W['g_usd']['ops']:,.0f} "
                          f"— swaps/treasury moves, not user top-ups</i>")
    body_lines += v2_block(W, WLAB)
    _restated = _ST.get("mente_restated_v1")
    if not _restated:
        body_lines += ["", "ℹ️ <i>All-time figures restated: the digest now includes the MENTE era "
                       "(the page always did) — cumulative lines move up once; windows are unaffected.</i>"]
    body_lines += ["", "<b>All time</b>",
                   f"  · {cum['invoke']:,} invokes · {cum['equip']:,} equips · {cum['creators']:,} creator wallets paid",
                   "  · " + usd_line("paid to creator wallets", "qty_ce", cum)]
    msg = "\n".join(body_lines + health)
    assert len(msg) < 4000, f"digest message is {len(msg)} chars — Telegram's limit is 4096"
_restated = _ST.get("mente_restated_v1")

if DRY_RUN:
    print(msg)
    print(f"--- dry run: {len(msg)} chars, nothing sent ---")
    raise SystemExit(0)

# Rate-limit the hourly digest: the cron now fires 4x/hour (scheduler
# starvation workaround), but Po wants at most ~one digest per hour. State
# rides in alert_state.json (Actions cache). Daily/weekly always send.
if mode == "hourly":
    _last = _ST.get("last_hourly_digest")
    if _last and (now - datetime.fromisoformat(_last)).total_seconds() < 50 * 60:
        print(f"hourly digest sent {_last} — under 50 min ago, skipping")
        raise SystemExit(0)

body = urllib.parse.urlencode({
    "chat_id": os.environ["TELEGRAM_CHAT_ID"],
    "text": msg,
    "parse_mode": "HTML",
    "disable_web_page_preview": "true",
    "reply_markup": json.dumps({"inline_keyboard": [[{"text": "📊 Open dashboard", "url": URL}]]}),
}).encode()
req = urllib.request.Request(
    f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
    data=body)
# Liveness bookkeeping (item 4): record the ATTEMPT's outcome either way.
# On failure record-then-reraise — the workflow step is continue-on-error,
# so the raise can't kill the refresh, but alive_check.py turns 3 consecutive
# failures into a red run.
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        print("telegram:", r.status)
except Exception:
    _state.record_send("digest", False, now)
    raise
_state.record_send("digest", True, now)
# stamp AFTER the successful send — stamping first would let one failed send
# silence the digest for 50 min (same class as the alerts.py QA finding)
if _carried_warn_keys:
    _state.mark_warns_sent(_carried_warn_keys, now)
    print(f"WARN tier: {len(_carried_warn_keys)} queued notice(s) carried by this digest")
if mode == "hourly":
    _state.update({"last_hourly_digest": now.isoformat(timespec="minutes")})
elif not _restated:
    _state.update({"mente_restated_v1": now.isoformat(timespec="minutes")})

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
TOKENS = {a.lower(): s for s, a in _D["scope"]["tokens"].items()}   # addr -> sym
RATE = F["rate"].get("MOCA") or hist[-1]["rate"]

# Payout taxonomy lives in classify.py — the ONE classifier shared with the
# page (refresh.py) and alerts.py. Rows are priced at the DAY-PINNED rate per
# token (day_rates.json, incl. today's provisional open_day_rate) so history
# can't reprice with the market; the live per-token rate is the last resort.
# The classifier is ERA-AWARE: every call passes the row's own timestamp.
from classify import classify_usd, INCENT, pin_rate, era_for, RESUMED_UTC, LEGACY, UNITS
_dr_state = json.load(open(os.path.join(HERE, "day_rates.json")))
_DAY_RATES = {sym: dict(_dr_state["day_rates"].get(sym, {})) for sym in TOKENS.values()}
for sym, od in (_dr_state.get("open_day_rate") or {}).items():
    _DAY_RATES.setdefault(sym, {}).setdefault(od["d"], od["rate"])

def classify(ts, sym, v):
    """-> (class, usd, tier, fine) in the digest's local vocabulary. ts is the
    row's ISO timestamp (day-pins the rate AND selects the reward era)."""
    _fb = F["rate"].get(sym)
    if not _fb and not _DAY_RATES.get(sym):
        raise SystemExit(f"FATAL: no rate available for {sym} — refusing to price rows at $0")
    usd = v * pin_rate(_DAY_RATES.get(sym, {}), ts[:10], _fb or 0)
    coarse, fine, tier = classify_usd(usd, ts)
    if coarse == "growth":
        return ("topup" if fine.startswith("stripe") else "incentive"), usd, tier, fine
    if coarse == "nonstandard":
        return "other", usd, None, fine
    return coarse, usd, None, fine

# ALL tracked tokens (council loop 3: the digest previously classified only
# MOCA, silently excluding the entire MENTE era from the all-time figures
# while the page counted both — the one-figure-everywhere failure).
rows = []          # (ts, cls, qty, wallet, tier, usd_day_pinned, sym, fine)
for i in shards.load(os.path.join(HERE, "transfers")):
    sym = TOKENS.get(i["token"]["address_hash"].lower())
    if not sym:
        continue
    v = int(i["total"]["value"]) / 1e18
    ts_iso = i["timestamp"][:19]
    cls, usd, tier, fine = classify(ts_iso, sym, v)
    # wallet lowercased: Blockscout rows are EIP-55, eth_getLogs rows are
    # lowercase, and a distinct-wallet count split by casing overstated
    # "creators paid" vs the page (56 vs 47 on 2026-09-15)
    rows.append((datetime.fromisoformat(ts_iso), cls, v, i["to"]["hash"].lower(), tier, usd, sym, fine))

def win(hours=None):
    """Activity inside the trailing window (None = all history).

    Counts are payout transfers; creators are DISTINCT recipient wallets paid
    an invoke- or equip-sized amount; moca_* are summed payout amounts.
    tiers_* map $ size -> count, so the mix is visible, not just the total.
    """
    rs = rows if hours is None else [r for r in rows if r[0] > now - timedelta(hours=hours)]
    def _qty(rs_, groups):
        q = {}
        for r in rs_:
            if r[1] in groups:
                q[r[6]] = q.get(r[6], 0) + r[2]
        return q
    def tiers(cls):
        out = {}
        for r in rs:
            if r[1] == cls and r[4]:
                out[r[4]] = out.get(r[4], 0) + 1
        return dict(sorted(out.items()))
    return {
        "invoke": sum(1 for r in rs if r[1] == "invoke"),
        "equip": sum(1 for r in rs if r[1] == "equip"),
        "incentive": sum(1 for r in rs if r[1] == "incentive"),
        "topup_n": sum(1 for r in rs if r[1] == "topup"),
        "creators": len({r[3] for r in rs if r[1] in ("invoke", "equip")}),
        "qty_ce": _qty(rs, ("invoke", "equip")),
        "qty_incent": _qty(rs, ("incentive",)),
        "qty_topup": _qty(rs, ("topup",)),
        "usd_ce": sum(r[5] for r in rs if r[1] in ("invoke", "equip")),
        "usd_incent": sum(r[5] for r in rs if r[1] == "incentive"),
        "usd_topup": sum(r[5] for r in rs if r[1] == "topup"),
        "tiers_incent": tiers("incentive"),
        "tiers_topup": tiers("topup"),
        "other_n": sum(1 for r in rs if r[1] == "other"),
        "other_usd": sum(r[5] for r in rs if r[1] == "other"),
        # Creator Rewards v2 (fine == equip/invoke on the v2 grid, since the resume)
        "v2_equip": sum(1 for r in rs if r[7] == "equip" and r[0] >= RESUMED),
        "v2_invoke": sum(1 for r in rs if r[7] == "invoke" and r[0] >= RESUMED),
        "v2_usd_equip": sum(r[5] for r in rs if r[7] == "equip" and r[0] >= RESUMED),
        "v2_usd_invoke": sum(r[5] for r in rs if r[7] == "invoke" and r[0] >= RESUMED),
        "v2_creators": len({r[3] for r in rs if r[7] in UNITS and r[0] >= RESUMED}),
        "legacy_n": sum(1 for r in rs if r[7] == LEGACY_FINE),
    }

RESUMED = datetime.fromisoformat(RESUMED_UTC)
LEGACY_FINE = LEGACY["topup1"]["fine"]

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
COUNTS = [("invoke", f"skill invokes{_sz('invoke')}"), ("equip", f"skill equips{_sz('equip')}"),
          ("creators", "creator wallets paid"), ("incentive", "growth incentives")]

def counts_line(w):
    """'7 skill invokes / 2 growth incentives' — zero terms omitted entirely."""
    return " · ".join(f"{w[k]:,} {lab}" for k, lab in COUNTS if w[k])

def usd_line(label, key, w, note=""):
    # $ figure is the sum of day-pinned per-row USD — same basis as the page.
    # Raw amounts render per token (MENTE-era rows are now included).
    u = w[key.replace("qty_", "usd_")]
    qty = " + ".join(f"{q:,.0f} {s}" for s, q in sorted(w[key].items(), key=lambda kv: -kv[1]) if q) or "0"
    return f"<b>{label}:</b> {qty} ≈ <b>${u:,.2f}</b>{note}"

head = {"hourly": "🟢 <b>Hourly refresh OK</b>",
        "daily": "📊 <b>Daily summary</b>",
        "weekly": "🗓 <b>Weekly summary</b>"}[mode]

import state as _state
_ST = _state.load()

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
health.append("")
health.append("🔧 <b>Ops health</b>")
health.append(f"<b>Wallet balance:</b> <b>${usd_m + usd_e:,.0f}</b> total{stale}")
health.append(f"  · MOCA: {bal_m or 0:,.0f} ≈ <b>${usd_m:,.2f}</b>")
health.append(f"  · MENTE: {bal_e or 0:,.0f} ≈ <b>${usd_e:,.2f}</b>")
out_1h = sum(r[5] for r in rows if r[0] > now - timedelta(hours=1))
_f1h = f"out ${out_1h:,.0f} 1h · $" if mode == "hourly" else "out $"
# Economy vs ops split (council loop 3): the payout lines above exclude
# swaps/treasury logistics, so a raw Flows total never visibly reconciled
# with them. economy = classified payout USD in the window; ops = the
# RESIDUAL (guaranteed to close on the message itself). A negative residual
# is a basis mismatch and is flagged, never printed as a negative dollar.
_eco24 = sum(r[5] for r in rows if r[0] > now - timedelta(hours=24) and r[1] != "other")
_ops24 = w24_f["out_usd"] - _eco24
_ops_s = (f"ops out ${_ops24:,.0f} <i>(swaps/treasury)</i>" if _ops24 >= 0
          else f"ops out $0 <i>(⚠ basis mismatch ${_ops24:,.0f})</i>")
health.append(f"<b>Flows:</b> {_f1h}{w24_f['out_usd']:,.0f} 24h = economy ${_eco24:,.0f} + {_ops_s} · in ${w24_f['in_usd']:,.0f} 24h · net {'+' if w24_f['net_usd']>=0 else ''}${w24_f['net_usd']:,.0f}")
if mode in ("daily", "weekly"):
    health.append(f"<b>Pattern monitor:</b> {G['flagged_n']} of {G['monitored_n']} flagged · <b>at risk:</b> ${G['at_risk_usd']:,.2f} of ${G['ce_total_usd']:,.2f} <i>(heuristic, unconfirmed)</i>")
    if G.get("runway7") is not None:
        health.append(f"<b>Payout float:</b> ~{G['runway7']} days (7d-avg burn) · {G.get('runway24') or '?'}d at 24h pace — top-up cadence, not solvency")
    # Alert-delivery liveness (Cycle-3 Loop 2, item 4): counts come from
    # alert_state.json's send_health (Actions cache) — digest-only, never a
    # public artifact. A stretch of failures also turns refresh.yml red via
    # alive_check.py; this line keeps the trend visible even below threshold.
    _sh = _ST.get("send_health") or {}
    _cut24 = (now - timedelta(hours=24)).isoformat(timespec="minutes")
    _n_sent = sum(1 for c in _sh.values() for t in c.get("sent", []) if t > _cut24)
    _n_fail = sum(1 for c in _sh.values() for t in c.get("failed", []) if t > _cut24)
    health.append(f"<b>alerts:</b> {_n_sent} sent / {_n_fail} failed (24h)")
if mode == "weekly":
    health.append("")
    _ss = _D.get("stripe_snap") or {}
    _ver = (f" Verified Stripe net: ${_ss.get('net_usd', 0):,.0f} ({_ss['period'][0]}→{_ss['period'][1]}, one-time snapshot)."
            if _ss.get("net_usd") and _ss.get("period") else "")
    health.append(f"<i>Paste-ready:</i> This week: {w7d['invoke']:,} invokes across {w7d['creators']:,} creator wallets, ${w7d['usd_ce']:,.2f} paid to creators — {G['flagged_n']} account(s) flagged for review. ${w7d['usd_topup']:,.2f} of flows were Stripe-pack-sized deliveries (size-inferred; may include coupon-delivered credits — not verified revenue).{_ver}")
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
for sym, r in (F.get("recon") or {}).items():
    if r and r.get("warn"):
        health.append(f"⚠️ <b>Reconciliation drift ({sym}):</b> Δ moved {r['drift']:+,.1f} since last run — possible missed transfers")
if G.get("burn_prev", 0) > 0 and G.get("burn24", 0) / G["burn_prev"] > 2 and mode != "hourly":
    health.append(f"⚠️ <b>Burn accelerating:</b> ${G['burn24']}/24h vs ${G['burn_prev']} prior")
rw = min(G.get("runway7") or 99, G.get("runway24") or 99)
if rw < 7:
    prev_rw = hist[-2].get("runway7", hist[-2].get("runway_adj")) if len(hist) >= 2 else None
    crossed = prev_rw is None or prev_rw >= 7 or (rw < 1 <= prev_rw) or (rw < 0.5 <= prev_rw)
    if mode != "hourly" or crossed or now.hour % 6 == 0:
        health.append(f"🔴 <b>Low float:</b> ${G['burn24']}/24h burn → ~{rw}d left")


# ---- Creator Rewards v2 block (2026-09-15) ----
# Counts/USD come from the same era-aware rows as every other line above.
# Cap headroom comes from alert_state.json's cap_probe heartbeat (PRIVATE —
# Actions cache, written by alerts.py every run). Po's private channel, so
# the cap figure and per-hour headroom ARE printed here; the public page and
# every public artifact carry no cap data at all.
def v2_block(w, lab):
    out = ["", f"🎯 <b>Creator rewards v2 ({lab})</b>",
           f"  · <b>equips:</b> {w['v2_equip']:,} ≈ ${w['v2_usd_equip']:,.2f} · "
           f"<b>invokes:</b> {w['v2_invoke']:,} ≈ ${w['v2_usd_invoke']:,.2f} · "
           f"<b>creators paid:</b> {w['v2_creators']:,}"]
    cp = _ST.get("cap_probe") or {}
    fresh = bool(cp.get("ts")) and (now - datetime.fromisoformat(cp["ts"])).total_seconds() <= 2 * 3600
    if fresh:
        cap = cp.get("cap_usd") or 0
        out.append(f"  · <b>Cap headroom (60m):</b> top creator ${cp.get('top_usd_1h', 0):,.2f} / ${cap:,.2f} cap · "
                   f"{cp.get('creators_1h', 0)} creators paid · {cp.get('n_at_cap', 0)} ≥90%")
        if mode == "daily":
            out.append(f"  · <b>Peak creator-hour (24h):</b> ${cp.get('top_clock_usd_24h', 0):,.2f} / ${cap:,.2f} cap · "
                       f"{cp.get('n_at_90pct_24h', 0)} creator(s) with a ≥90% hour")
        if mode == "weekly":
            _c7 = (now - timedelta(days=7)).isoformat(timespec="minutes")
            _breaches = sum(1 for v in (_ST.get("cap_hits") or {}).values() if v > _c7)
            _fan = sum(1 for v in ((_ST.get("cap_state") or {}).get("fanout") or {}).values() if v > _c7)
            share = cp.get("top_creator_share_7d")
            out.append(f"  · <b>Cap (7d):</b> max creator-hour ${cp.get('top_clock_usd_7d', 0):,.2f} / ${cap:,.2f} · "
                       f"{_breaches} breach(es) · {_fan} fan-out hour(s)"
                       + (f" · top creator share {share}% (24h)" if share is not None else ""))
    else:
        out.append(f"  · ⚠️ cap probe missing — detector did not run "
                   f"(last {hkt(cp['ts']) if cp.get('ts') else 'never'})")
    # legacy $1 top-ups: the window count from the rows, the running total
    # from the SAME public aggregate the page shows (infer.legacy_public)
    lp = next(iter(_D["infer"].get("legacy_public") or []), None)
    if lp:
        since = hkt_day(lp["since"]) if lp.get("since") else "?"
        out.append(f"  · <b>Legacy $1 top-ups:</b> {w['legacy_n']:,} ({lab}) · {lp['n']:,} since {since}")
    # pricing provenance (day_rates.json): last closed day + the open day
    dr = _dr_state["day_rates"].get("MOCA") or {}
    src = (_dr_state.get("day_rate_src") or {}).get("MOCA") or {}
    od = (_dr_state.get("open_day_rate") or {}).get("MOCA")
    parts = []
    if dr:
        last_d = max(dr)
        parts.append(f"MOCA day rate {dr[last_d]:.6f} ({hkt_day(last_d)}, {src.get(last_d, '?')})")
    parts.append(f"open day {od['rate']:.6f} (market-open)" if od else "open day carry-forward")
    if mode != "hourly":
        c30 = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        cf = sum(1 for d, v in src.items() if c30 <= d < now.strftime("%Y-%m-%d") and v == "carry-forward")
        parts.append(f"{cf} carry-forward day(s) in 30d")
    out.append("  · <b>Pricing:</b> " + " · ".join(parts))
    return out


fresh = counts_line(w1)
body_lines = [f"{head} — <i>Skill Payout Dashboard</i> · {hkt(now, '%d %b %Y %H:%M')}", "", "📈 <b>Economy</b>"]
if mode == "hourly":
    body_lines += [f"<b>New this hour:</b> {fresh}" if fresh
                   else "<b>New this hour:</b> <i>nothing new</i>", ""]
body_lines += [
    f"<b>Last {WLAB}</b>",
    f"  · payouts: {counts_line(W) or '<i>none</i>'}",
    "  · " + usd_line("paid to creators", "qty_ce", W),
    "  · " + usd_line("incentive spend", "qty_incent", W,
                      f" <i>({mix(W['tiers_incent'], INCENT_LABEL)})</i>" if W["tiers_incent"] else ""),
    "  · " + usd_line("top-ups delivered", "qty_topup", W,
                      f" <i>({W['topup_n']:,} paid: {mix(W['tiers_topup'])})</i>" if W["tiers_topup"]
                      else " <i>(Stripe-pack-sized, size-inferred)</i>"),
]
if W["other_n"]:
    body_lines.append(f"  · <i>excluded {W['other_n']:,} non-standard transfer(s) ≈ ${W['other_usd']:,.0f} "
                      f"— swaps/treasury moves, not user top-ups</i>")
body_lines += v2_block(W, WLAB)
_restated = _ST.get("mente_restated_v1")
if mode in ("daily", "weekly") and not _restated:
    body_lines += ["", "ℹ️ <i>All-time figures restated: the digest now includes the MENTE era "
                   "(the page always did) — cumulative lines move up once; windows are unaffected.</i>"]
if mode in ("daily", "weekly"):
    body_lines += ["", "<b>All time</b>",
                   f"  · {cum['invoke']:,} invokes · {cum['equip']:,} equips · {cum['creators']:,} creator wallets paid",
                   "  · " + usd_line("paid to creators", "qty_ce", cum)]
msg = "\n".join(body_lines + health)
assert len(msg) < 4000, f"digest message is {len(msg)} chars — Telegram's limit is 4096"

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
if mode == "hourly":
    _state.update({"last_hourly_digest": now.isoformat(timespec="minutes")})
elif not _restated:
    _state.update({"mente_restated_v1": now.isoformat(timespec="minutes")})

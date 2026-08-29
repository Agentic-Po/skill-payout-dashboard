#!/usr/bin/env python3
"""Send a rich-format (HTML) Telegram status update with deltas.

Usage: notify.py hourly|daily|weekly
Deltas are computed directly from the transfers/ shard timestamps, so they are
exact from the very first message — no snapshot warm-up needed.
Env vars: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.
"""
import json, os, sys, urllib.request, urllib.parse
import shards
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
URL = "https://agentic-po.github.io/skill-payout-dashboard/"
mode = sys.argv[1] if len(sys.argv) > 1 else "hourly"

hist = json.load(open(os.path.join(HERE, "stats_history.json")))
now = datetime.now(timezone.utc).replace(tzinfo=None)

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
if (now - datetime.fromisoformat(_gen.replace("Z", ""))).total_seconds() > 2 * 3600:
    raise SystemExit(f"FATAL: data.json stale (generated {_gen}) — refusing to send outdated figures")
F = _D["facts"]
G = _D["infer"]["guard"]
TOKENS = {a.lower(): s for s, a in _D["scope"]["tokens"].items()}   # addr -> sym
RATE = F["rate"].get("MOCA") or hist[-1]["rate"]

# Payout taxonomy lives in classify.py — the ONE classifier shared with the
# page (refresh.py) and alerts.py. Rows are priced at the DAY-PINNED rate per
# token (day_rates.json, incl. today's provisional open_day_rate) so history
# can't reprice with the market; the live per-token rate is the last resort.
from classify import classify_usd, INCENT, pin_rate
_dr_state = json.load(open(os.path.join(HERE, "day_rates.json")))
_DAY_RATES = {sym: dict(_dr_state["day_rates"].get(sym, {})) for sym in TOKENS.values()}
for sym, od in (_dr_state.get("open_day_rate") or {}).items():
    _DAY_RATES.setdefault(sym, {}).setdefault(od["d"], od["rate"])

def classify(day, sym, v):
    """-> (class, usd, tier) in the digest's local vocabulary."""
    _fb = F["rate"].get(sym)
    if not _fb and not _DAY_RATES.get(sym):
        raise SystemExit(f"FATAL: no rate available for {sym} — refusing to price rows at $0")
    usd = v * pin_rate(_DAY_RATES.get(sym, {}), day, _fb or 0)
    coarse, fine, tier = classify_usd(usd)
    if coarse == "growth":
        return ("topup" if fine.startswith("stripe") else "incentive"), usd, tier
    if coarse == "nonstandard":
        return "other", usd, None
    return coarse, usd, None

# ALL tracked tokens (council loop 3: the digest previously classified only
# MOCA, silently excluding the entire MENTE era from the all-time figures
# while the page counted both — the one-figure-everywhere failure).
rows = []          # (ts, cls, qty, wallet, tier, usd_day_pinned, sym)
for i in shards.load(os.path.join(HERE, "transfers")):
    sym = TOKENS.get(i["token"]["address_hash"].lower())
    if not sym:
        continue
    v = int(i["total"]["value"]) / 1e18
    cls, usd, tier = classify(i["timestamp"][:10], sym, v)
    rows.append((datetime.fromisoformat(i["timestamp"][:19]), cls, v, i["to"]["hash"], tier, usd, sym))

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
    }

def mix(tiers, labels=None):
    """'23 x $10 - 24 x $20' — biggest first, blank when empty."""
    if not tiers: return ""
    top = sorted(tiers.items(), key=lambda kv: -kv[1])
    return " · ".join(f"{n:,} × {labels[p] if labels else '$%d' % p}" for p, n in top)

INCENT_LABEL = dict(INCENT)

w1, w24, w7d, cum = win(1), win(24), win(24 * 7), win()
# the window the body reports on: 24h hourly/daily, 7d weekly
W, WLAB = (w7d, "7d") if mode == "weekly" else (w24, "24h")

COUNTS = [("invoke", "skill invokes"), ("equip", "skill equips"),
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
stale = f" ⚠️ <i>(live fetch failed — last known {stale_ts} UTC)</i>" if stale_ts else ""
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
if mode == "weekly":
    health.append("")
    _ss = _D.get("stripe_snap") or {}
    _ver = (f" Verified Stripe net: ${_ss.get('net_usd', 0):,.0f} ({_ss['period'][0]}→{_ss['period'][1]}, one-time snapshot)."
            if _ss.get("net_usd") and _ss.get("period") else "")
    health.append(f"<i>Paste-ready:</i> This week: {w7d['invoke']:,} invokes across {w7d['creators']:,} creator wallets, ${w7d['usd_ce']:,.2f} paid to creators — {G['flagged_n']} account(s) flagged for review. ${w7d['usd_topup']:,.2f} of flows were Stripe-pack-sized deliveries (size-inferred; may include coupon-delivered credits — not verified revenue).{_ver}")
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

fresh = counts_line(w1)
body_lines = [f"{head} — <i>Skill Payout Dashboard</i>", "", "📈 <b>Economy</b>"]
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
import state as _state
_restated = _state.load().get("mente_restated_v1")
if mode in ("daily", "weekly") and not _restated:
    body_lines += ["", "ℹ️ <i>All-time figures restated: the digest now includes the MENTE era "
                   "(the page always did) — cumulative lines move up once; windows are unaffected.</i>"]
if mode in ("daily", "weekly"):
    body_lines += ["", "<b>All time</b>",
                   f"  · {cum['invoke']:,} invokes · {cum['equip']:,} equips · {cum['creators']:,} creator wallets paid",
                   "  · " + usd_line("paid to creators", "qty_ce", cum)]
msg = "\n".join(body_lines + health)

# Rate-limit the hourly digest: the cron now fires 4x/hour (scheduler
# starvation workaround), but Po wants at most ~one digest per hour. State
# rides in alert_state.json (Actions cache). Daily/weekly always send.
if mode == "hourly":
    _last = _state.load().get("last_hourly_digest")
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
with urllib.request.urlopen(req, timeout=30) as r:
    print("telegram:", r.status)
# stamp AFTER the successful send — stamping first would let one failed send
# silence the digest for 50 min (same class as the alerts.py QA finding)
if mode == "hourly":
    _state.update({"last_hourly_digest": now.isoformat(timespec="minutes")})
elif not _restated:
    _state.update({"mente_restated_v1": now.isoformat(timespec="minutes")})

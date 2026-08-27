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
RATE = hist[-1]["rate"]
now = datetime.now(timezone.utc).replace(tzinfo=None)

def classify(v):
    usd = v * RATE
    if usd < 0.06: return "micro"
    if usd < 0.4: return "invoke"
    if usd < 2: return "equip"
    if usd < 7: return "incentive"   # new-user credits ($3) + referrals ($5) — real spend
    return "topup"                   # Stripe top-ups — revenue-backed passthrough

rows = []
for i in shards.load(os.path.join(HERE, "transfers")):
    if i["token"]["address_hash"].lower() != "0x2b11834ed1feaed4b4b3a86a6f571315e25a884d":
        continue
    v = int(i["total"]["value"]) / 1e18
    rows.append((datetime.fromisoformat(i["timestamp"][:19]), classify(v), v, i["to"]["hash"]))

MENTE_ADDR = "0x4cd9a847f39106e19a4e41aea8a232e915c82af5"
mente_rows = []
for i in shards.load(os.path.join(HERE, "transfers")):
    if i["token"]["address_hash"].lower() == MENTE_ADDR:
        mente_rows.append((datetime.fromisoformat(i["timestamp"][:19]),
                           int(i["total"]["value"]) / 1e18))

def win(hours=None):
    """Activity inside the trailing window (None = all history).

    Counts are payout transfers; creators are DISTINCT recipient wallets paid
    an invoke- or equip-sized amount; moca_* are summed payout amounts.
    """
    rs = rows if hours is None else [r for r in rows if r[0] > now - timedelta(hours=hours)]
    return {
        "invoke": sum(1 for r in rs if r[1] == "invoke"),
        "equip": sum(1 for r in rs if r[1] == "equip"),
        "incentive": sum(1 for r in rs if r[1] == "incentive"),
        "creators": len({r[3] for r in rs if r[1] in ("invoke", "equip")}),
        "moca_ce": sum(r[2] for r in rs if r[1] in ("invoke", "equip")),
        "moca_incent": sum(r[2] for r in rs if r[1] in ("incentive", "micro")),
        "moca_topup": sum(r[2] for r in rs if r[1] == "topup"),
    }

w1, w24, w7d, cum = win(1), win(24), win(24 * 7), win()
# the window the body reports on: 24h hourly/daily, 7d weekly
W, WLAB = (w7d, "7d") if mode == "weekly" else (w24, "24h")

COUNTS = [("invoke", "skill invokes"), ("equip", "skill equips"),
          ("creators", "creator wallets paid"), ("incentive", "growth incentives")]

def counts_line(w):
    """'7 skill invokes / 2 growth incentives' — zero terms omitted entirely."""
    return " · ".join(f"{w[k]:,} {lab}" for k, lab in COUNTS if w[k])

def usd_line(label, key, w, note=""):
    return f"<b>{label}:</b> {w[key]:,.0f} MOCA ≈ <b>${w[key] * RATE:,.2f}</b>{note}"

head = {"hourly": "🟢 <b>Hourly refresh OK</b>",
        "daily": "📊 <b>Daily summary</b>",
        "weekly": "🗓 <b>Weekly summary</b>"}[mode]

# guard summary from the freshly built page
import re
_D = json.loads(re.search(r'const DATA = (\{.*\})\s*;\n', open(os.path.join(HERE, "index.html")).read()).group(1))
G = _D["infer"]["guard"]
F = _D["facts"]
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
health.append(f"<b>Wallet balance:</b> <b>${usd_m + usd_e:,.0f}</b> total{stale}")
health.append(f"  · MOCA: {bal_m or 0:,.0f} ≈ <b>${usd_m:,.2f}</b>")
health.append(f"  · MENTE: {bal_e or 0:,.0f} ≈ <b>${usd_e:,.2f}</b>")
out_1h = (sum(r[2] for r in rows if r[0] > now - timedelta(hours=1)) * RATE
          + sum(v for t, v in mente_rows if t > now - timedelta(hours=1)) * (F["rate"].get("MENTE") or 0))
_f1h = f"out ${out_1h:,.0f} 1h · $" if mode == "hourly" else "out $"
health.append(f"<b>Flows:</b> {_f1h}{w24_f['out_usd']:,.0f} 24h · in ${w24_f['in_usd']:,.0f} 24h · net {'+' if w24_f['net_usd']>=0 else ''}${w24_f['net_usd']:,.0f} 24h")
if mode in ("daily", "weekly"):
    health.append(f"<b>Pattern monitor:</b> {G['flagged_n']} of {G['monitored_n']} flagged · <b>at risk:</b> ${G['at_risk_usd']:,.2f} of ${G['ce_total_usd']:,.2f} <i>(heuristic, unconfirmed)</i>")
    if G.get("runway7") is not None:
        health.append(f"<b>Payout float:</b> ~{G['runway7']} days (7d-avg burn) · {G.get('runway24') or '?'}d at 24h pace — top-up cadence, not solvency")
if mode == "weekly":
    health.append("")
    health.append(f"<i>Paste-ready:</i> This week: {w7d['invoke']:,} invokes across {w7d['creators']:,} creator wallets, ${w7d['moca_ce']*RATE:,.2f} paid to creators — {G['flagged_n']} account(s) flagged for review, ${w7d['moca_topup']*RATE:,.2f} of flows revenue-backed (Stripe-sized).")
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
body_lines = [f"{head} — <i>Skill Payout Dashboard</i>", ""]
if mode == "hourly":
    body_lines += [f"<b>New this hour:</b> {fresh}" if fresh
                   else "<b>New this hour:</b> <i>nothing new</i>", ""]
body_lines += [
    f"<b>Last {WLAB}</b>",
    f"  · payouts: {counts_line(W) or '<i>none</i>'}",
    "  · " + usd_line("paid to creators", "moca_ce", W),
    "  · " + usd_line("incentive spend", "moca_incent", W, " <i>($3 credits + $5 referrals)</i>"),
    "  · " + usd_line("top-ups delivered", "moca_topup", W, " <i>(revenue-backed, Stripe)</i>"),
]
if mode in ("daily", "weekly"):
    body_lines += ["", "<b>All time</b>",
                   f"  · {cum['invoke']:,} invokes · {cum['equip']:,} equips · {cum['creators']:,} creator wallets paid",
                   "  · " + usd_line("paid to creators", "moca_ce", cum)]
msg = "\n".join(body_lines + health)

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

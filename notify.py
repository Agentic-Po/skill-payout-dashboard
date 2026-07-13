#!/usr/bin/env python3
"""Send a rich-format (HTML) Telegram status update with deltas.

Usage: notify.py hourly|daily|weekly
Deltas are computed directly from transfers.json timestamps, so they are
exact from the very first message — no snapshot warm-up needed.
Env vars: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.
"""
import json, os, sys, urllib.request, urllib.parse
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
for i in json.load(open(os.path.join(HERE, "transfers.json"))):
    if i["token"]["address_hash"].lower() != "0x2b11834ed1feaed4b4b3a86a6f571315e25a884d":
        continue
    v = int(i["total"]["value"]) / 1e18
    rows.append((datetime.fromisoformat(i["timestamp"][:19]), classify(v), v, i["to"]["hash"]))

def stats(cutoff):
    """Cumulative figures counting only transfers at or before `cutoff`."""
    rs = [r for r in rows if r[0] <= cutoff]
    return {
        "invoke": sum(1 for r in rs if r[1] == "invoke"),
        "equip": sum(1 for r in rs if r[1] == "equip"),
        "incentive": sum(1 for r in rs if r[1] == "incentive"),
        "creators": len({r[3] for r in rs if r[1] in ("invoke", "equip")}),
        "moca_ce": sum(r[2] for r in rs if r[1] in ("invoke", "equip")),
        "moca_incent": sum(r[2] for r in rs if r[1] in ("incentive", "micro")),
        "moca_topup": sum(r[2] for r in rs if r[1] == "topup"),
    }

cur = stats(now)
h1, h24, d7 = (stats(now - timedelta(hours=h)) for h in (1, 24, 24 * 7))

def delta(key, ref):
    d = cur[key] - ref[key]
    return f"+{d:,.0f}" if d >= 0 else f"{d:,.0f}"

def line(label, key):
    if mode == "hourly":
        return f"<b>{label}:</b> {cur[key]:,} <i>({delta(key, h1)} 1h · {delta(key, h24)} 24h)</i>"
    if mode == "daily":
        return f"<b>{label}:</b> {cur[key]:,} <i>({delta(key, h24)} 24h)</i>"
    return f"<b>{label}:</b> {cur[key]:,} <i>({delta(key, d7)} 7d)</i>"

head = {"hourly": "🟢 <b>Hourly refresh OK</b>",
        "daily": "📊 <b>Daily summary</b>",
        "weekly": "🗓 <b>Weekly summary</b>"}[mode]
def usd_line(label, key, note=""):
    u = cur[key] * RATE
    return f"<b>{label}:</b> {cur[key]:,.0f} MOCA ≈ <b>${u:,.2f}</b>{note}"

# guard summary from the freshly built page
import re
G = json.loads(re.search(r'const DATA = (\{.*?\})\s*;', open(os.path.join(HERE, "index.html")).read(), re.S).group(1))["guard"]
health = []
if G.get("balance") is not None:
    bd = G.get("bal_delta24") or 0
    sign = "+" if bd >= 0 else ""
    health.append("")
    health.append(f"<b>Wallet:</b> <b>${G['balance']*RATE:,.2f}</b> ({G['balance']:,.0f} MOCA · {sign}{bd:,.0f} MOCA/24h)")
if G.get("topup24"):
    health.append(f"⬆️ <b>Top-up received:</b> +{G['topup24']:,.0f} MOCA in the last 24h")
if mode in ("daily", "weekly"):
    health.append(f"<b>Organic payout share:</b> {G['organic_share']}% · <b>at risk:</b> ${G['at_risk_usd']} <i>(unconfirmed)</i>")
    if G.get("runway_days") is not None:
        health.append(f"<b>Payout float:</b> ~{G.get('runway_adj') or G['runway_days']} days projected ({G['balance']:,.0f} MOCA) — top-up cadence, not solvency")
if mode == "weekly":
    flagged = sum(1 for r in G["rows"] if r["status"] == "review")
    health.append("")
    health.append(f"<i>Paste-ready:</i> This week: {cur['invoke']:,} invokes across {cur['creators']} creators, ${cur['moca_ce']*RATE:,.2f} paid to creators — {G['organic_share']}% organic, {flagged} account(s) under review, ${cur['moca_topup']*RATE:,.2f} of flows revenue-backed.")
if G.get("recon_drift"):
    health.append(f"⚠️ <b>Reconciliation drift:</b> {G['recon_drift']:,.1f} MOCA unexplained vs transfer logs")
if G.get("burn_prev", 0) > 0 and G.get("burn24", 0) / G["burn_prev"] > 2 and mode != "hourly":
    health.append(f"⚠️ <b>Burn accelerating:</b> ${G['burn24']}/24h vs ${G['burn_prev']} prior")
rw = min(G.get("runway_adj") or 99, G.get("runway_days") or 99)
if rw < 7:
    # throttle for hourly: alert on threshold crossing/escalation or every 6h; daily/weekly always
    prev_rw = hist[-2].get("runway_adj") if len(hist) >= 2 else None
    crossed = prev_rw is None or prev_rw >= 7 or (rw < 1 <= prev_rw) or (rw < 0.5 <= prev_rw)
    if mode != "hourly" or crossed or now.hour % 6 == 0:
        need = f" — top up ≥{G['topup_needed']:,.0f} MOCA for 7d float" if G.get("topup_needed") else ""
        health.append(f"🔴 <b>Low float:</b> ${G['burn24']}/24h burn → ~{rw}d left{need}")

msg = "\n".join([
    f"{head} — <i>Skill Payout Dashboard</i>",
    "",
    line("Skill invokes", "invoke"),
    line("Skill equips", "equip"),
    line("Creators earning", "creators"),
    line("Growth incentives", "incentive"),
    "",
    usd_line("Creator earnings", "moca_ce"),
    usd_line("Incentive spend", "moca_incent", " <i>(credits + referrals)</i>"),
    usd_line("Top-ups delivered", "moca_topup", " <i>(revenue-backed, Stripe)</i>"),
] + health)

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

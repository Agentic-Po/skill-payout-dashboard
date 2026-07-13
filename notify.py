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
    return "growth"

rows = []
for i in json.load(open(os.path.join(HERE, "transfers.json"))):
    if i["token"]["symbol"] != "MOCA":
        continue
    v = int(i["total"]["value"]) / 1e18
    rows.append((datetime.fromisoformat(i["timestamp"][:19]), classify(v), v, i["to"]["hash"]))

def stats(cutoff):
    """Cumulative figures counting only transfers at or before `cutoff`."""
    rs = [r for r in rows if r[0] <= cutoff]
    return {
        "invoke": sum(1 for r in rs if r[1] == "invoke"),
        "equip": sum(1 for r in rs if r[1] == "equip"),
        "growth": sum(1 for r in rs if r[1] == "growth"),
        "creators": len({r[3] for r in rs if r[1] in ("invoke", "equip")}),
        "moca": sum(r[2] for r in rs),
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
usd = cur["moca"] * RATE

msg = "\n".join([
    f"{head} — <i>Skill Payout Dashboard</i>",
    "",
    line("Skill invokes", "invoke"),
    line("Skill equips", "equip"),
    line("Creators earning", "creators"),
    line("Growth payouts", "growth"),
    f"<b>Total MOCA out:</b> {cur['moca']:,.0f} ≈ <b>${usd:,.2f}</b>",
])

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

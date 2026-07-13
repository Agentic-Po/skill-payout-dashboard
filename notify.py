#!/usr/bin/env python3
"""Send a rich-format (HTML) Telegram status update with deltas.

Usage: notify.py hourly|daily|weekly
Reads stats_history.json (written by refresh.py) and env vars
TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.
"""
import json, os, sys, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
URL = "https://agentic-po.github.io/skill-payout-dashboard/"
mode = sys.argv[1] if len(sys.argv) > 1 else "hourly"

hist = json.load(open(os.path.join(HERE, "stats_history.json")))
cur = hist[-1]
now = datetime.strptime(cur["ts"], "%Y-%m-%dT%H:%M")

def snap_before(hours):
    """Latest snapshot at least `hours` old; None if history too short."""
    target = now - timedelta(hours=hours)
    older = [h for h in hist if datetime.strptime(h["ts"], "%Y-%m-%dT%H:%M") <= target]
    return older[-1] if older else None

def delta(key, ref):
    if ref is None:
        return "n/a"
    d = cur[key] - ref[key]
    return f"+{d:,.0f}" if d >= 0 else f"{d:,.0f}"

h1, h24, d7 = snap_before(1), snap_before(24), snap_before(24 * 7)
usd = cur["moca"] * cur["rate"]

def line(label, key):
    if mode == "hourly":
        return f"<b>{label}:</b> {cur[key]:,} <i>({delta(key, h1)} 1h · {delta(key, h24)} 24h)</i>"
    if mode == "daily":
        return f"<b>{label}:</b> {cur[key]:,} <i>({delta(key, h24)} 24h)</i>"
    return f"<b>{label}:</b> {cur[key]:,} <i>({delta(key, d7)} 7d)</i>"

head = {"hourly": "🟢 <b>Hourly refresh OK</b>",
        "daily": "📊 <b>Daily summary</b>",
        "weekly": "🗓 <b>Weekly summary</b>"}[mode]

msg = "\n".join([
    f"{head} — <i>Skill Payout Dashboard</i>",
    "",
    line("Skill invokes", "invoke"),
    line("Skill equips", "equip"),
    line("Creators earning", "creators"),
    line("Growth payouts", "growth"),
    f"<b>Total MOCA out:</b> {cur['moca']:,.0f} ≈ ${usd:,.2f}",
    "",
    f'<a href="{URL}">Open full dashboard →</a>',
])

body = urllib.parse.urlencode({
    "chat_id": os.environ["TELEGRAM_CHAT_ID"],
    "text": msg,
    "parse_mode": "HTML",
    "disable_web_page_preview": "true",
}).encode()
req = urllib.request.Request(
    f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
    data=body)
with urllib.request.urlopen(req, timeout=30) as r:
    print("telegram:", r.status)

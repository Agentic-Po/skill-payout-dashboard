#!/usr/bin/env python3
"""Anomaly + large-flow Telegram alerts (separate message from the hourly OK).

Spec (Po, 2026-08-05):
- Baseline from FULL history of hourly flow deltas (USD, out and in
  separately, zero-filled for quiet hours):
    baseline  = median          (primary)
    mean      = secondary reference, shown for context
    sigma     = std of the 25th-75th percentile subset only (IQR-trimmed,
                so past spikes can't inflate the yardstick)
  Flag when the trailing-1h flow exceeds median + 3*sigma.
- Any single transfer >= $5,000 is flagged:
    inflow  -> ask to confirm it's the requested funding arrival
    outflow -> ask to confirm it's a scheduled cognition distribution batch
- Sends ONE message per run, only when something is flagged. Dedup for
  large transfers lives in alert_state.json (committed by the workflow).
Env vars: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.
"""
import json, os, re, statistics, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

import shards

HERE = os.path.dirname(os.path.abspath(__file__))
URL = "https://agentic-po.github.io/skill-payout-dashboard/"
BIG_USD = 5000
STATE_PATH = os.path.join(HERE, "alert_state.json")

DATA = json.loads(re.search(r'const DATA = (\{.*\})\s*;\n',
                            open(os.path.join(HERE, "index.html")).read()).group(1))
RATE = DATA["facts"]["rate"]                      # {sym: usd}
TOKENS = {a.lower(): s for s, a in DATA["scope"]["tokens"].items()}
LABELS = {r["addr"].lower(): r["role"] for r in DATA.get("registry", [])}
WALLET = DATA["scope"]["wallet"].lower()
now = datetime.now(timezone.utc).replace(tzinfo=None)


def usd_rows(dir_name, counterparty_key):
    """(ts, usd, sym, qty, counterparty) for every tracked-token transfer."""
    out = []
    for i in shards.load(os.path.join(HERE, dir_name)):
        sym = TOKENS.get(i["token"]["address_hash"].lower())
        if not sym:
            continue
        qty = int(i["total"]["value"]) / 1e18
        out.append((datetime.fromisoformat(i["timestamp"][:19]), qty * RATE[sym],
                    sym, qty, i[counterparty_key]["hash"],
                    f'{i["transaction_hash"]}:{i["log_index"]}'))
    return out

flows = {"out": usd_rows("transfers", "to"), "in": usd_rows("transfers_in", "from")}


def baseline(rows):
    """median / mean / IQR-trimmed std over zero-filled hourly USD buckets."""
    if not rows:
        return None
    bucket = {}
    for ts, usd, *_ in rows:
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


state = json.load(open(STATE_PATH)) if os.path.exists(STATE_PATH) else {}
seen = set(state.get("seen", []))
anom = state.get("anomaly", {})
lines = []

# --- 1. hourly outflow anomaly vs median + 3*IQR (Tukey fence; decided with
# Po 2026-08-05 after the spec'd median+3*sigma_iqr backtested at a 22% fire
# rate). Edge-triggered with a 6h cooldown: a sustained event alerts once.
# Inflow anomaly is deliberately skipped — inflows are so sparse the median
# and sigma are $0; the $5,000 single-transfer rule below covers them.
b = baseline(flows["out"])
if b and b["iqr"] > 0:
    threshold = b["median"] + 3 * b["iqr"]
    last_h = sum(usd for ts, usd, *_ in flows["out"] if ts > now - timedelta(hours=1))
    above = last_h > threshold
    st = anom.get("out", {})
    last_fire = datetime.fromisoformat(st["last_alert"]) if st.get("last_alert") else None
    cooled = last_fire is None or now - last_fire >= timedelta(hours=6)
    if above and not st.get("above") and cooled:
        anom["out"] = {"above": True, "last_alert": now.isoformat(timespec="minutes")}
        lines += ["", f"📈 <b>Abnormal hourly outflow:</b> <b>${last_h:,.0f}</b> in the last hour",
                  f"  · baseline median ${b['median']:,.2f}/h (mean ${b['mean']:,.2f}) · "
                  f"σ(25–75%) ${b['sigma_iqr']:,.2f} · IQR ${b['iqr']:,.2f}",
                  f"  · threshold median+3×IQR = ${threshold:,.2f} · {b['hours']:,}h history"]
    else:
        anom["out"] = {"above": above, "last_alert": st.get("last_alert")}

# --- 2. single transfers >= $5,000 (last 24h, deduped) ---
for d, rows in flows.items():
    for ts, usd, sym, qty, cp, key in rows:
        if usd < BIG_USD or ts <= now - timedelta(hours=24) or key in seen:
            continue
        seen.add(key)
        who = LABELS.get(cp.lower(), "unlabelled address")
        cp_s = f"{cp[:8]}…{cp[-4:]}"
        if d == "in":
            lines += ["", f"💰 <b>Large inflow:</b> {qty:,.0f} {sym} ≈ <b>${usd:,.0f}</b>",
                      f"  · from {cp_s} — <i>{who}</i>",
                      "  · ❓ <b>Verify:</b> is this your requested fund arrival?"]
        else:
            lines += ["", f"📤 <b>Large outflow:</b> {qty:,.0f} {sym} ≈ <b>${usd:,.0f}</b>",
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
        lines += ["", "⏰ <b>Rebate wallet swap overdue</b> — remind DATops",
                  f"  · Minds Rebate Fireblocks wallet {_sink_addr[:8]}…{_sink_addr[-4:]} holds "
                  f"<b>{_rebate['bal_mente']:,.0f} MENTE</b> (≈${_rebate['bal_mente_usd']:,.0f}) unswapped",
                  f"  · last MENTE→MOCA swap: <b>{_rebate.get('last_swap') or 'never'}</b>"
                  + (f" ({_rebate['days_since_swap']} days ago)" if _rebate.get("days_since_swap") is not None else ""),
                  "  · expected cadence: weekly"]

# keep state bounded: only keys that can still re-trigger (last 48h of rows)
recent = {r[5] for rows in flows.values() for r in rows
          if r[0] > now - timedelta(hours=48)}
json.dump({**state, "seen": sorted(seen & recent), "anomaly": anom}, open(STATE_PATH, "w"))

if lines:
    msg = "\n".join(["🚨 <b>Flow alert</b> — <i>Skill Payout Dashboard</i>"] + lines)
    body = urllib.parse.urlencode({
        "chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": msg, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
        "reply_markup": json.dumps({"inline_keyboard": [[{"text": "📊 Open dashboard", "url": URL}]]}),
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage", data=body)
    with urllib.request.urlopen(req, timeout=30) as r:
        print("alert sent:", r.status)
else:
    print("no alerts")

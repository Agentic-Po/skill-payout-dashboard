#!/usr/bin/env python3
"""Cross-repo peer watch: is the moca-ledger detector still alive?

De-inlined from refresh.yml (council loop 3) — the /tmp dedup marker was dead
on ephemeral runners; dedup now rides alert_state.json via state.py like every
other alert. Non-blocking by contract: any failure here must never fail the
refresh job (the workflow step keeps continue-on-error).
"""
import datetime as dt
import json
import os
import time
import urllib.parse
import urllib.request

import state

RAW = "https://raw.githubusercontent.com/Agentic-Po/moca-ledger/main/heartbeat.json"


def tg(msg):
    tok, chat = os.environ.get("LEDGER_BOT_TOKEN"), os.environ.get("LEDGER_CHAT_ID")
    if not (tok and chat):
        return
    urllib.request.urlopen(urllib.request.Request(
        f"https://api.telegram.org/bot{tok}/sendMessage",
        data=urllib.parse.urlencode({"chat_id": chat, "text": msg, "parse_mode": "HTML"}).encode()), timeout=20)


def main():
    hb = json.load(urllib.request.urlopen(
        urllib.request.Request(RAW, headers={"User-Agent": "peer-watch"}), timeout=20))
    ts = hb.get("run_ts")
    age = ((dt.datetime.now(dt.timezone.utc)
            - dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))).total_seconds() / 60
           if ts else 1e9)
    lag = hb.get("lag_blocks") or 0
    if age > 90 or lag > 900:
        last = float(state.load().get("ledger_stale_last", 0))
        if time.time() - last > 6 * 3600:      # dedupe: at most one every 6 h
            tg(f"⏳ <b>ledger detector stale</b>\nlast run {age:.0f} min ago · {lag} blocks behind tip\n(reported by the dashboard hourly job)")
            state.update({"ledger_stale_last": time.time()})
        print(f"ledger STALE: {age:.0f} min old, lag {lag}")
    else:
        print(f"ledger ok: {age:.0f} min old, lag {lag}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        tg(f"⏳ <b>ledger heartbeat unreadable</b>\n{str(e)[:100]}")
        print("peer unreadable ->", e)

#!/usr/bin/env python3
"""Alert-delivery liveness gate (Cycle-3 Loop 2, item 4).

The dead-man switch catches full-workflow death; nothing caught SEND-PATH
death — a revoked bot token or a changed chat id fails every Telegram call
while each send step is continue-on-error (redline: a Telegram outage must
never kill a refresh that otherwise succeeded), so the workflow stays green
and alerts silently stop. This gate reads the consecutive-failure counters
state.record_send() keeps in alert_state.json and exits 1 — a red workflow,
its own "Alert Telegram on failure" step notwithstanding — once any channel
has failed THRESH times in a row. Individual sends stay continue-on-error;
only this check is blocking.

Caveat (RUNBOOK-deadman.md): alert_state.json rides the Actions cache, and
cache eviction resets the counters.

  python3 alive_check.py     exit 1 when any channel has >= 3 consecutive
                             failed send attempts
"""
import sys

import state

THRESH = 3


def main():
    health = state.load().get("send_health") or {}
    if not health:
        print("alert liveness: no send attempts recorded yet — ok")
        return 0
    bad = []
    for channel, c in sorted(health.items()):
        n = int(c.get("consec_fail", 0))
        last_ok = (c.get("sent") or ["never"])[-1]
        print(f"channel {channel!r}: {n} consecutive failure(s) · last success {last_ok}")
        if n >= THRESH:
            bad.append((channel, n))
    for channel, n in bad:
        print(f"::error::alert channel {channel!r} has failed {n} sends in a row "
              f"(threshold {THRESH}) — the send path is dead: check "
              f"TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID and the Telegram API status")
    print("alert liveness:", "FAIL" if bad else "ok")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

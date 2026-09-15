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
                             failed send attempts, OR when the creator-reward
                             cap detector's heartbeat (cap_probe, written by
                             alerts.py on every run) is older than 2 h

Second leg (Creator Rewards v2, 2026-09-15): alerts.py is continue-on-error
too, so a detector that crashes every run would leave the workflow green and
the cap unwatched. cap_probe.ts is its dead-man. A MISSING probe is red only
once send_health exists — a cold cache (both absent) is not a failure.
"""
import sys
from datetime import datetime, timezone

import state

THRESH = 3
PROBE_MAX_AGE_H = 2


def main():
    st = state.load()
    health = st.get("send_health") or {}
    bad = []
    if not health:
        print("alert liveness: no send attempts recorded yet — ok")
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
    # --- cap detector heartbeat ---
    probe = st.get("cap_probe") or {}
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    probe_bad = False
    if probe.get("ts"):
        age_h = (now - datetime.fromisoformat(probe["ts"])).total_seconds() / 3600
        print(f"cap detector heartbeat: {probe['ts']} ({age_h:.1f}h old, rows {probe.get('rows')})")
        if age_h > PROBE_MAX_AGE_H:
            probe_bad = True
            print(f"::error::cap detector heartbeat is {age_h:.1f}h old (limit {PROBE_MAX_AGE_H}h) — "
                  f"alerts.py has not completed a run: the creator-reward cap is unwatched")
    elif health:
        probe_bad = True
        print("::error::cap detector heartbeat missing while send_health exists — alerts.py "
              "did not write cap_probe: the creator-reward cap is unwatched")
    else:
        print("cap detector heartbeat: none yet (cold cache) — ok")
    print("alert liveness:", "FAIL" if (bad or probe_bad) else "ok")
    return 1 if (bad or probe_bad) else 0


if __name__ == "__main__":
    sys.exit(main())

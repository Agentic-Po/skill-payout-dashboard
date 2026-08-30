#!/usr/bin/env python3
"""Single writer for alert_state.json (council loop 3, 2026-08-30).

Three scripts used to read-modify-write the file independently; a kill
mid-write could leave truncated JSON that silently reset all dedup state.
All mutations now go through update(), which merges onto the latest on-disk
state and writes atomically (tmp file + os.replace). load() never crashes:
missing or corrupt state degrades to {} — the worst case is one duplicate
alert, never silence and never a traceback in a notification path.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "alert_state.json")


def load():
    try:
        with open(PATH) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def update(mutation):
    """Merge mutation onto the latest on-disk state, atomically."""
    st = load()
    st.update(mutation)
    tmp = PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(st, fh)
    os.replace(tmp, PATH)
    return st


def record_send(channel, ok, now=None):
    """Record one Telegram send attempt's outcome (Cycle-3 Loop 2, item 4).

    Per channel ("alerts", "digest"): a CONSECUTIVE-failure counter (reset to
    0 by any success) plus 24h send/fail timestamp lists for the daily
    digest's health line. alive_check.py turns consec_fail >= 3 into a red
    workflow — the send-path dead-man the full-workflow dead-man can't see.
    Only ATTEMPTED sends are recorded; a run with nothing to say leaves the
    counters untouched. State rides the Actions cache (never committed);
    cache eviction resets the counters — documented in RUNBOOK-deadman.md.
    """
    from datetime import datetime, timedelta, timezone
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = (now - timedelta(hours=24)).isoformat(timespec="minutes")
    stamp = now.isoformat(timespec="minutes")
    health = dict(load().get("send_health") or {})
    c = dict(health.get(channel) or {})
    sent = [t for t in c.get("sent", []) if t > cutoff]
    failed = [t for t in c.get("failed", []) if t > cutoff]
    if ok:
        sent.append(stamp)
        c["consec_fail"] = 0
    else:
        failed.append(stamp)
        c["consec_fail"] = int(c.get("consec_fail", 0)) + 1
    c["sent"], c["failed"] = sent[-200:], failed[-200:]
    health[channel] = c
    return update({"send_health": health})

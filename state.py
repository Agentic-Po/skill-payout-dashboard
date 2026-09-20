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


def update(mutation, drop=()):
    """Merge mutation onto the latest on-disk state, atomically. `drop` names
    keys to remove (a merge alone can never retire a renamed key)."""
    st = load()
    st.update(mutation)
    for k in drop:
        st.pop(k, None)
    tmp = PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(st, fh)
    os.replace(tmp, PATH)
    return st


# ---- WARN tier queue (severity tiering, council 2026-09-21) ----
# Three tiers across the whole alert surface: BLOCK/PAGE = its own Telegram
# message, immediately; WARN = degraded-but-published, queued here and drained
# into the NEXT digest by notify.py (never its own message, at most one per
# key per WARN_COOLDOWN_H); LOG = run log only. Queue rides alert_state.json
# (Actions cache, private). A WARN that no digest could carry within
# WARN_TTL_H is dropped — it was never urgent by definition.
WARN_COOLDOWN_H = 6
WARN_TTL_H = 24


def warn(key, text, now=None):
    """Queue one WARN-tier line for the next digest. Same key inside the
    cooldown (measured from the last queued OR sent entry) is dropped.
    Returns True when queued."""
    from datetime import datetime, timedelta, timezone
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    q = [w for w in (load().get("warn_queue") or []) if isinstance(w, dict)]
    last = max((w.get("ts", "") for w in q if w.get("key") == key), default=None)
    if last and now - datetime.fromisoformat(last) < timedelta(hours=WARN_COOLDOWN_H):
        return False
    q.append({"key": key, "text": text, "ts": now.isoformat(timespec="minutes"), "sent": None})
    update({"warn_queue": q[-200:]})
    return True


def pending_warns(now=None):
    """Unsent, unexpired WARN entries (one per key, newest text) in queue order."""
    from datetime import datetime, timedelta, timezone
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    cut = (now - timedelta(hours=WARN_TTL_H)).isoformat(timespec="minutes")
    seen, out = set(), []
    for w in reversed(load().get("warn_queue") or []):
        if not isinstance(w, dict) or w.get("sent") or w.get("ts", "") < cut or w.get("key") in seen:
            continue
        seen.add(w["key"])
        out.append(w)
    return list(reversed(out))


def mark_warns_sent(keys, now=None):
    """Stamp every unsent entry of the given keys as sent; trim entries older
    than 2x the TTL so the queue stays bounded."""
    from datetime import datetime, timedelta, timezone
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    stamp = now.isoformat(timespec="minutes")
    keep = (now - timedelta(hours=2 * WARN_TTL_H)).isoformat(timespec="minutes")
    q = []
    for w in load().get("warn_queue") or []:
        if not isinstance(w, dict) or w.get("ts", "") < keep:
            continue
        if w.get("key") in keys and not w.get("sent"):
            w = {**w, "sent": stamp}
        q.append(w)
    return update({"warn_queue": q})


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

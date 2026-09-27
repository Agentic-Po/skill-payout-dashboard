#!/usr/bin/env python3
"""Pure helpers shared by the alerting modules (council loop 3, 2026-09-27).

Each of these existed as byte-identical copies in fences.py and cap_detect.py;
they now live once, here. PURE by contract — no file or network I/O, no
STATE / alert_state access, no module-level side effects — and
tests/test_util.py enforces that by AST, plus that the old copies are gone.

notify.hkt is deliberately NOT here: it also accepts an ISO string, so it is
not the same function (merging would change what fences/cap_detect do with a
string argument from "raise" to "format").
"""
from datetime import datetime, timedelta

HKT = timedelta(hours=8)


def hkt(dt, fmt="%d %b %H:%M"):
    """A naive-UTC datetime rendered in HKT (Po's clock) for alert text."""
    return (dt + HKT).strftime(fmt) + " HKT"


def iso_min(dt):
    """datetime -> ISO string at minute grain (the alert-state timestamp form)."""
    return dt.isoformat(timespec="minutes")


def parse_dt(s):
    """ISO string -> datetime; empty/None -> None."""
    return datetime.fromisoformat(s) if s else None


def cooled(last_iso, now, hours):
    """True if no previous fire (last_iso empty) or it is at least `hours` old."""
    last = parse_dt(last_iso)
    return last is None or now - last >= timedelta(hours=hours)


PUBLIC_WINDOWS = ("24h", "7d", "30d")
PUBLIC_WINDOW_KEYS = ("label", "out_usd", "out_tx", "out_wallets", "in_usd")


def public_summary(data):
    """data.json -> the payload index.html (the one-screen public summary)
    embeds: a strict subset, nothing computed that data.json does not carry."""
    facts = data["facts"]
    return {"scope": {k: data["scope"].get(k) for k in ("wallet", "generated", "generated_iso")},
            "exec_summary": data["exec_summary"],
            "balance": facts["balance"], "balance_usd": facts["balance_usd"],
            "windows": [{k: w.get(k) for k in PUBLIC_WINDOW_KEYS}
                        for w in facts["windows"] if w.get("label") in PUBLIC_WINDOWS]}

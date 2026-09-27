#!/usr/bin/env python3
"""Quiet public logs (2026-09-27): the ONE switch for monitoring detail.

Anyone can read a public repo's Actions logs, so detector internals and
monitoring status/counts must not be printed there (README "State & privacy
invariants": monitoring status is digest-only). Every detector-side print of
such detail goes through private_print():

  * locally (GITHUB_ACTIONS != "true"): printed as before — debugging works.
  * inside GitHub Actions: NOT printed. The line is appended to
    private_log.json (a bounded ring, gitignored, carried ONLY in the
    STATE_KEY-encrypted Actions cache — tools/state_crypt.py), and the
    optional `public` line — which must be number-free — is printed instead.

The Telegram digest / state.warn / guard_private.json remain the primary
private destinations; private_log.json only keeps the detail lines that used
to exist nowhere but the run log, so no information Po had is lost.

No other module may test GITHUB_ACTIONS for this purpose
(tests/test_quiet_logs.py enforces it).
"""
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "private_log.json")
LOG_MAX = 5000          # ~ one week of detail lines at 4 runs/h


def in_actions():
    return os.environ.get("GITHUB_ACTIONS") == "true"


def _bank(src, line):
    try:
        with open(LOG_PATH) as fh:
            log = json.load(fh)
        if not isinstance(log, list):
            log = []
    except (FileNotFoundError, ValueError):
        log = []
    log.append({"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "src": src, "line": line})
    tmp = LOG_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(log[-LOG_MAX:], fh)
    os.replace(tmp, LOG_PATH)


def private_print(detail, public=None, src=None):
    """Print `detail` locally; in Actions bank it privately and print only
    `public` (number-free status, or nothing when None)."""
    if not in_actions():
        print(detail)
        return
    try:
        _bank(src or os.path.basename(sys.argv[0] or "?"), str(detail))
    except OSError:
        pass            # a full disk must not turn a quiet log into a crash
    if public:
        print(public)

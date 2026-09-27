#!/usr/bin/env python3
"""ONE staleness threshold for the whole pipeline (council loop 3, 2026-09-27).

Before this file the same "how old is too old" number lived in three places
that could drift apart: the page banner (template.html STALE_MIN=90), the
exec-summary / coupon-summary "degraded" flags in refresh.py (1.5 h) and the
weekly health check in .github/workflows/weekly.yml (48 h). Now:

  * refresh.py sets the degraded flags from STALE_MINUTES and substitutes it
    for the __STALE_MIN__ placeholder in template.html at render;
  * weekly.yml computes the page age in MINUTES and asks health() below.

Pure: no I/O, no network, no state. tests/test_freshness.py pins the edges.

    python3 freshness.py health <run_conclusion> <age_minutes>   # -> healthy | UNHEALTHY
"""
import sys

# 90 min: QA over 976 runs — 75 flickered ~1x/day on healthy pages, 90 ~0.5x/day.
STALE_MINUTES = 90


def is_stale(age_minutes):
    """True once the data is OLDER than the threshold (the page's `ageM > STALE_MIN`)."""
    return age_minutes > STALE_MINUTES


def health(conclusion, age_minutes):
    """Weekly health verdict: the last refresh run succeeded AND the page is not stale."""
    return "healthy" if conclusion == "success" and not is_stale(age_minutes) else "UNHEALTHY"


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "health":
        try:
            age = float(sys.argv[3])
        except ValueError:
            age = float("inf")          # unreadable age is not "fresh"
        print(health(sys.argv[2], age))
    else:
        raise SystemExit("usage: freshness.py health <run_conclusion> <age_minutes>")

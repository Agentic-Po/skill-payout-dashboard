#!/usr/bin/env python3
"""Optional PostHog server-recorded tier for the treasury dashboard.

Pulls daily top-up conversions, mind awakenings, and WAU from PostHog
(project-scoped read-only key) and banks closed days in posthog_cache.json —
same immutability pattern as the chain caches: a closed day is fetched once
and never re-queried. Returns None when no key is configured (local .env or
POSTHOG_API_KEY env var), so refresh.py degrades gracefully.

Caveat carried into the UI: PostHog currently holds CLIENT-confirmed top-up
events (lossy); the server-side Stripe webhook 'purchase' event exists only in
hm_events and is not exported yet (open ask to the data team).
"""
import json, os, urllib.request
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "posthog_cache.json")


def _env():
    env = dict(os.environ)
    p = os.path.join(HERE, ".env")
    if os.path.exists(p):
        for l in open(p):
            if "=" in l and not l.startswith("#"):
                k, v = l.strip().split("=", 1)
                env.setdefault(k, v)
    return env


def fetch():
    env = _env()
    key = env.get("POSTHOG_API_KEY")
    if not key:
        return None
    host = env.get("POSTHOG_HOST", "https://us.posthog.com")
    project = env.get("POSTHOG_PROJECT_ID", "459477")

    def hogql(q):
        req = urllib.request.Request(f"{host}/api/projects/{project}/query/",
            data=json.dumps({"query": {"kind": "HogQLQuery", "query": q}}).encode(),
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r).get("results", [])

    cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {"daily": {}}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # closed days already banked are never re-queried; today is always refreshed
    known = {d for d in cache["daily"] if d < today}
    since = (datetime.now(timezone.utc) - timedelta(days=35)).strftime("%Y-%m-%d")
    try:
        rows = hogql(
            "select toDate(timestamp) d,"
            " countIf(event='topup_completed') topups,"
            " sumIf(toFloat(coalesce(properties.amount_usd, properties.amount, 0)), event='topup_completed') topup_usd,"
            " countIf(event='topup_chosen') topup_intents,"
            " countIf(event='mind_awaken') awakens,"
            " countIf(event='sign_up' or event='login') logins"
            f" from events where timestamp >= toDate('{since}')"
            " and event in ('topup_completed','topup_chosen','mind_awaken','sign_up','login')"
            " group by d order by d")
        for d, topups, topup_usd, intents, awakens, logins in rows:
            if d in known:
                continue
            cache["daily"][d] = {"topups": topups, "topup_usd": round(topup_usd or 0, 2),
                                 "topup_intents": intents, "awakens": awakens, "logins": logins,
                                 "partial": d == today}
        wau = hogql("select count(distinct person_id) from events where timestamp > now() - interval 7 day")
        mau = hogql("select count(distinct person_id) from events where timestamp > now() - interval 30 day")
        cache["wau"] = wau[0][0] if wau else None
        cache["mau"] = mau[0][0] if mau else None
        cache["fetched"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        cache["complete"] = True
    except Exception as e:
        print("posthog fetch failed (serving cached):", e)
        cache["complete"] = False
    json.dump(cache, open(CACHE, "w"))
    return cache


if __name__ == "__main__":
    c = fetch()
    if c:
        days = sorted(c["daily"])[-8:]
        for d in days:
            print(d, c["daily"][d])
        print("WAU", c.get("wau"), "MAU", c.get("mau"))

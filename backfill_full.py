#!/usr/bin/env python3
"""One-off deep backfill: extend transfers.json / transfers_in.json back to CUTOFF.

Same pagination as refresh.py but without the 300-page cap, so the cache reaches
the wallet's late-April history that the original backfill missed.
"""
import json, os, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
WALLET = "0xBD956171F5B50936f0Ad1C4db80c022bd2442519"
BASE = f"https://base.blockscout.com/api/v2/addresses/{WALLET}/token-transfers?filter=from"
CUTOFF = "2026-04-18T00:00:00"

def get(url, tries=5):
    for a in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8.4.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception:
            if a == tries - 1:
                raise
            time.sleep(3 * (a + 1))

def deep(base_url, path):
    cache = json.load(open(path)) if os.path.exists(path) else []
    have = {i["transaction_hash"] + str(i["log_index"]) for i in cache}
    oldest = min((i["timestamp"] for i in cache), default="9999")[:19]
    if oldest <= CUTOFF:
        print(path, "already reaches", oldest)
        return
    got, params, pages = [], "", 0
    while True:
        d = get(base_url + params)
        b = d.get("items", [])
        pages += 1
        if not b:
            break
        got += [i for i in b if i["transaction_hash"] + str(i["log_index"]) not in have]
        if pages % 25 == 0:
            print(path, "page", pages, "reached", b[-1]["timestamp"][:19], flush=True)
        if b[-1]["timestamp"][:19] < CUTOFF or not d.get("next_page_params"):
            break
        params = "&" + "&".join(f"{k}={v}" for k, v in d["next_page_params"].items())
        time.sleep(0.15)
    merged = sorted(cache + got, key=lambda i: i["timestamp"], reverse=True)
    json.dump(merged, open(path, "w"))
    print(path, "done:", len(got), "new,", len(merged), "total, oldest", merged[-1]["timestamp"][:19], flush=True)

deep(BASE, os.path.join(HERE, "transfers.json"))
deep(BASE.replace("filter=from", "filter=to"), os.path.join(HERE, "transfers_in.json"))
print("BACKFILL COMPLETE")

#!/usr/bin/env python3
"""Refresh the skill payout dashboard.

Fetches new outgoing token transfers from Blockscout (Base) for the tracked wallet, merges them into transfers.json, recomputes the dashboard
dataset, and renders dashboard.html from template.html.
"""
import json, os, time, urllib.request
from datetime import datetime, timezone, timedelta
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
WALLET = "0xBD956171F5B50936f0Ad1C4db80c022bd2442519"
BASE = f"https://base.blockscout.com/api/v2/addresses/{WALLET}/token-transfers?filter=from"

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.4.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

# --- incremental fetch: pull newest pages until we overlap the cache ---
cache_path = os.path.join(HERE, "transfers.json")
old = json.load(open(cache_path)) if os.path.exists(cache_path) else []
seen = {i["transaction_hash"] + str(i["log_index"]) for i in old}
newest_ts = old[0]["timestamp"] if old else "2026-06-30"
items, params = [], ""
for _ in range(100):
    d = get(BASE + params)
    b = d.get("items", [])
    if not b:
        break
    items += b
    if b[-1]["timestamp"] < newest_ts or not d.get("next_page_params"):
        break
    params = "&" + "&".join(f"{k}={v}" for k, v in d["next_page_params"].items())
    time.sleep(0.1)
add = [i for i in items if i["transaction_hash"] + str(i["log_index"]) not in seen]
full = add + old
json.dump(full, open(cache_path, "w"))
print(f"fetched {len(add)} new transfers, cache now {len(full)}")

# --- live MOCA rate (fall back to last known) ---
RATE = 0.00891226
try:
    tok = get("https://base.blockscout.com/api/v2/tokens/0x2B11834Ed1FeAEd4b4b3a86A6F571315E25A884D")
    RATE = float(tok.get("exchange_rate") or RATE)
except Exception as e:
    print("rate fetch failed, using fallback:", e)

# --- build dataset (MOCA only) ---
rows = []
for i in full:
    if i["token"]["symbol"] != "MOCA":
        continue
    v = int(i["total"]["value"]) / 1e18
    rows.append({"ts": i["timestamp"][:19], "val": round(v, 4), "to": i["to"]["hash"], "tx": i["transaction_hash"]})
rows.sort(key=lambda r: r["ts"], reverse=True)

def cat(v):
    if v < 8: return "micro"
    if v < 20: return "invoke"
    if v < 200: return "equip"
    return "large"

for r in rows:
    r["cat"] = cat(r["val"])
now = datetime.now(timezone.utc)

h0 = datetime.fromisoformat(rows[-1]["ts"]).replace(minute=0, second=0, tzinfo=timezone.utc)
h1 = now.replace(minute=0, second=0, microsecond=0)
byh, mh = defaultdict(Counter), defaultdict(float)
for r in rows:
    byh[r["ts"][:13]][r["cat"]] += 1
    mh[r["ts"][:13]] += r["val"]
hourly, h = [], h0
while h <= h1:
    k = h.strftime("%Y-%m-%dT%H")
    c = byh[k]
    hourly.append({"h": k, "invoke": c["invoke"], "equip": c["equip"], "large": c["large"], "micro": c["micro"], "moca": round(mh[k], 1)})
    h += timedelta(hours=1)

days = sorted(set(r["ts"][:10] for r in rows))
today = now.strftime("%Y-%m-%d")
daily = []
for d in days + ([] if today in days else [today]):
    rs = [r for r in rows if r["ts"][:10] == d]
    c = Counter(r["cat"] for r in rs)
    daily.append({"d": d, "invoke": c["invoke"], "equip": c["equip"], "large": c["large"], "micro": c["micro"],
                  "moca_ce": round(sum(r["val"] for r in rs if r["cat"] in ("invoke", "equip")), 1),
                  "moca_other": round(sum(r["val"] for r in rs if r["cat"] in ("large", "micro")), 1)})

cr = defaultdict(lambda: {"invoke": 0, "equip": 0, "moca": 0.0})
for r in rows:
    if r["cat"] in ("invoke", "equip"):
        cr[r["to"]][r["cat"]] += 1
        cr[r["to"]]["moca"] += r["val"]
creators = sorted(({"addr": a, "invoke": d["invoke"], "equip": d["equip"], "moca": round(d["moca"], 1)} for a, d in cr.items()), key=lambda x: -x["moca"])
other = [{"ts": r["ts"], "val": round(r["val"], 2), "to": r["to"], "tx": r["tx"], "cat": r["cat"]} for r in rows if r["cat"] in ("large", "micro")]

cut24 = now - timedelta(hours=24)
S = {
    "rate": RATE, "generated": now.strftime("%Y-%m-%d %H:%M"),
    "first_invoke": "2026-07-11 17:17:59", "first_moca": "2026-07-11 15:41:07",
    "last_tx": rows[0]["ts"],
    "tot": {c: {"n": sum(1 for r in rows if r["cat"] == c), "moca": round(sum(r["val"] for r in rows if r["cat"] == c), 1)} for c in ["invoke", "equip", "large", "micro"]},
    "inv24": sum(1 for r in rows if r["cat"] == "invoke" and datetime.fromisoformat(r["ts"]).replace(tzinfo=timezone.utc) > cut24),
    "eq24": sum(1 for r in rows if r["cat"] == "equip" and datetime.fromisoformat(r["ts"]).replace(tzinfo=timezone.utc) > cut24),
    "creators_n": len(creators),
}
data = {"S": S, "hourly": hourly, "daily": daily, "creators": creators, "other": other}

tpl = open(os.path.join(HERE, "template.html")).read()
tpl = tpl.replace("MOCA rate used $0.008912", f"MOCA rate used ${RATE:.6f}")
out = os.path.join(HERE, "index.html")
open(out, "w").write("<!doctype html>\n<html lang=\"en\">\n" + tpl.replace("/*__DATA__*/", json.dumps(data)) + "\n</html>")
print("wrote", out, "| invokes:", S["tot"]["invoke"]["n"], "| last tx:", S["last_tx"])

#!/usr/bin/env python3
"""Refresh the skill payout dashboard.

Fetches new outgoing token transfers from Blockscout (Base) for the tracked wallet, merges them into transfers.json, recomputes the dashboard
dataset, and renders dashboard.html from template.html.
"""
import json, math, os, statistics, time, urllib.request
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

# --- treasury balance (live) ---
BALANCE = None
try:
    # Blockscout token-balances omits MOCA for this address; query balanceOf directly
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [
        {"to": "0x2B11834Ed1FeAEd4b4b3a86A6F571315E25A884D",
         "data": "0x70a08231" + "0" * 24 + WALLET[2:].lower()}, "latest"]}).encode()
    req = urllib.request.Request("https://base.blockscout.com/api/eth-rpc", data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "curl/8.4.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        BALANCE = int(json.load(r)["result"], 16) / 1e18
except Exception as e:
    print("balance fetch failed:", e)

# --- inbound transfers (treasury top-ups) ---
in_path = os.path.join(HERE, "transfers_in.json")
old_in = json.load(open(in_path)) if os.path.exists(in_path) else []
try:
    seen_in = {i["transaction_hash"] + str(i["log_index"]) for i in old_in}
    newest_in = old_in[0]["timestamp"] if old_in else "2026-06-30"
    got, params = [], ""
    for _ in range(20):
        d = get(BASE.replace("filter=from", "filter=to") + params)
        b = d.get("items", [])
        if not b:
            break
        got += b
        if b[-1]["timestamp"] < newest_in or not d.get("next_page_params"):
            break
        params = "&" + "&".join(f"{k}={v}" for k, v in d["next_page_params"].items())
        time.sleep(0.1)
    old_in = [i for i in got if i["transaction_hash"] + str(i["log_index"]) not in seen_in] + old_in
    json.dump(old_in, open(in_path, "w"))
except Exception as e:
    print("inbound fetch failed (using cache):", e)
inflows = [{"ts": i["timestamp"][:19], "val": int(i["total"]["value"]) / 1e18, "from": i["from"]["hash"]}
           for i in old_in if i["token"]["address_hash"].lower() == "0x2b11834ed1feaed4b4b3a86a6f571315e25a884d"]

# --- build dataset (MOCA only) ---
rows = []
for i in full:
    if i["token"]["address_hash"].lower() != "0x2b11834ed1feaed4b4b3a86a6f571315e25a884d":
        continue
    v = int(i["total"]["value"]) / 1e18
    rows.append({"ts": i["timestamp"][:19], "val": round(v, 4), "to": i["to"]["hash"], "tx": i["transaction_hash"]})
rows.sort(key=lambda r: r["ts"], reverse=True)

# --- day-anchored rate oracle: the $0.10 invoke cluster reveals each day's
# true MOCA/USD payout rate, so old transfers aren't mispriced at today's rate.
_seed = defaultdict(list)
for r in rows:
    if 0.05 < r["val"] * RATE < 0.4:          # coarse invoke band at live rate
        _seed[r["ts"][:10]].append(r["val"])
DAY_RATE = {d: 0.10 / statistics.median(v) for d, v in _seed.items() if len(v) >= 5}

def day_rate(ts):
    d = ts[:10]
    if d in DAY_RATE: return DAY_RATE[d]
    prior = [k for k in sorted(DAY_RATE) if k <= d]
    return DAY_RATE[prior[-1]] if prior else RATE

GRID = [(0.10, "invoke", "invoke"), (1, "equip", "equip"),
        (3, "growth", "new-user $3"), (5, "growth", "referral $5"),
        (10, "growth", "stripe $10"), (25, "growth", "stripe $25"),
        (50, "growth", "stripe $50")]

def classify(v, ts):
    """Snap to the price grid at the payout-day implied rate (±8%)."""
    usd = v * day_rate(ts)
    if usd < 0.06: return ("micro", "test")
    for unit, coarse, fine in GRID:
        if abs(usd - unit) / unit <= 0.08:
            return (coarse, fine)
    if usd > 7: return ("growth", "top-up other")
    return ("invoke", "nonstandard") if usd < 0.5 else ("growth", "nonstandard")

for r in rows:
    r["cat"], r["fine"] = classify(r["val"], r["ts"])
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
    hourly.append({"h": k, "invoke": c["invoke"], "equip": c["equip"], "growth": c["growth"], "micro": c["micro"], "moca": round(mh[k], 1)})
    h += timedelta(hours=1)

days = sorted(set(r["ts"][:10] for r in rows))
today = now.strftime("%Y-%m-%d")
daily = []
for d in days + ([] if today in days else [today]):
    rs = [r for r in rows if r["ts"][:10] == d]
    c = Counter(r["cat"] for r in rs)
    daily.append({"d": d, "invoke": c["invoke"], "equip": c["equip"], "growth": c["growth"], "micro": c["micro"],
                  "moca_ce": round(sum(r["val"] for r in rs if r["cat"] in ("invoke", "equip")), 1),
                  "moca_other": round(sum(r["val"] for r in rs if r["cat"] in ("growth", "micro")), 1)})

cr = defaultdict(lambda: {"invoke": 0, "equip": 0, "moca": 0.0})
for r in rows:
    if r["cat"] in ("invoke", "equip"):
        cr[r["to"]][r["cat"]] += 1
        cr[r["to"]]["moca"] += r["val"]
creators = sorted(({"addr": a, "invoke": d["invoke"], "equip": d["equip"], "moca": round(d["moca"], 1)} for a, d in cr.items()), key=lambda x: -x["moca"])
other = [{"ts": r["ts"], "val": round(r["val"], 2), "to": r["to"], "tx": r["tx"], "cat": r["fine"]} for r in rows if r["cat"] in ("growth", "micro")]

cut24 = now - timedelta(hours=24)
S = {
    "rate": RATE, "generated": now.strftime("%Y-%m-%d %H:%M"),
    "first_invoke": "2026-07-11 17:17:59", "first_moca": "2026-07-11 15:41:07",
    "last_tx": rows[0]["ts"],
    "tot": {c: {"n": sum(1 for r in rows if r["cat"] == c), "moca": round(sum(r["val"] for r in rows if r["cat"] == c), 1)} for c in ["invoke", "equip", "growth", "micro"]},
    "inv24": sum(1 for r in rows if r["cat"] == "invoke" and datetime.fromisoformat(r["ts"]).replace(tzinfo=timezone.utc) > cut24),
    "eq24": sum(1 for r in rows if r["cat"] == "equip" and datetime.fromisoformat(r["ts"]).replace(tzinfo=timezone.utc) > cut24),
    "creators_n": len(creators),
}
# --- pattern monitor (guardrail) ---
def gap_entropy(gaps):
    """Shannon entropy of inter-arrival gaps over log-spaced bins, normalized 0-1.
    Low = metronomic cadence; high = human-irregular."""
    bins = [30, 120, 600, 3600, 21600]  # 6 coarse bins — stable at small n
    hist = Counter(next((i for i, e in enumerate(bins) if g < e), len(bins)) for g in gaps)
    n = len(gaps)
    H = -sum((c / n) * math.log(c / n) for c in hist.values())
    return max(H / math.log(len(bins) + 1), 0.0)

def acf1(gaps):
    """Lag-1 autocorrelation of gaps; high = scripted pattern even with jitter."""
    if len(gaps) < 3: return 0.0
    a, b = gaps[:-1], gaps[1:]
    ma, mb = statistics.mean(a), statistics.mean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    return num / den if den else 0.0

inc_recip = {r["to"] for r in rows if r["fine"] in ("new-user $3", "referral $5")}
inv_counts = sorted(len([r for r in rows if r["to"] == c["addr"] and r["cat"] == "invoke"]) for c in creators)
vol_hi = max(15, inv_counts[int(len(inv_counts) * 0.95)] if len(inv_counts) >= 20 else 10**9)
grows = []
for c in creators:
    ts = sorted(datetime.fromisoformat(r["ts"]) for r in rows if r["to"] == c["addr"] and r["cat"] == "invoke")
    n = len(ts)
    if n < 10:
        continue
    gaps = [(b - a).total_seconds() for a, b in zip(ts, ts[1:])]
    ent = gap_entropy(gaps)
    ac = acf1(gaps)
    burst = max(sum(1 for t2 in ts if 0 <= (t2 - t1).total_seconds() <= 600) for t1 in ts) / n
    span_h = (ts[-1] - ts[0]).total_seconds() / 3600
    flags = []
    if c["addr"] in inc_recip: flags.append("both-sides")
    if n >= 30 and ent < 0.45: flags.append("uniform cadence")
    if n >= 30 and abs(ac) > max(0.6, 2 / math.sqrt(n - 1)): flags.append("scripted pattern")
    if n >= 15 and burst > 0.7 and span_h > 2: flags.append("burst cluster")
    tags = ["high volume"] if n > vol_hi else []
    grows.append({"addr": c["addr"], "n": n, "span_h": round(span_h, 1), "ent": round(ent, 2),
                  "acf": round(ac, 2), "burst": round(burst * 100), "moca": c["moca"],
                  "flags": flags, "tags": tags, "status": "review" if flags else "organic"})
ce_total = sum(c["moca"] for c in creators) or 1
at_risk = sum(g["moca"] for g in grows if g["status"] == "review")
small_moca = sum(c["moca"] for c in creators if c["addr"] not in {g["addr"] for g in grows})
organic_share = round((ce_total - at_risk) / ce_total * 100, 1)
burn24 = sum(r["val"] for r in rows
             if (r["cat"] in ("invoke", "equip") or r["fine"] in ("new-user $3", "referral $5"))
             and datetime.fromisoformat(r["ts"]).replace(tzinfo=timezone.utc) > cut24) * RATE
burn_prev = sum(r["val"] for r in rows if (r["cat"] in ("invoke", "equip") or r["fine"] in ("new-user $3", "referral $5"))
                and cut24 - timedelta(hours=24) < datetime.fromisoformat(r["ts"]).replace(tzinfo=timezone.utc) <= cut24) * RATE
growth_factor = min(burn24 / burn_prev, 2) if burn_prev > 0 else 1
runway = round(BALANCE * RATE / burn24, 1) if BALANCE and burn24 > 0 else None
runway_adj = round(BALANCE * RATE / (burn24 * growth_factor), 1) if BALANCE and burn24 > 0 else None
UNIT = {"invoke": 0.10, "equip": 1, "new-user $3": 3, "referral $5": 5,
        "stripe $10": 10, "stripe $25": 25, "stripe $50": 50}
promised = sum(UNIT.get(r["fine"], r["val"] * RATE) for r in rows)
settled = sum(r["val"] for r in rows) * RATE
fx_drift = round((settled - promised) / promised * 100, 1) if promised else 0
out24 = sum(r["val"] for r in rows if datetime.fromisoformat(r["ts"]).replace(tzinfo=timezone.utc) > cut24)
in24 = sum(f["val"] for f in inflows if datetime.fromisoformat(f["ts"]).replace(tzinfo=timezone.utc) > cut24)
in24_real = sum(f["val"] for f in inflows if f["val"] >= 100 and datetime.fromisoformat(f["ts"]).replace(tzinfo=timezone.utc) > cut24)
bal_delta24 = in24 - out24
topup24 = round(in24_real, 0)
recon_drift = None
try:
    prev_hist = json.load(open(os.path.join(HERE, "stats_history.json")))
    target = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M")
    older = [h for h in prev_hist if h["ts"] <= target and h.get("balance")]
    if older and BALANCE:
        expected = older[-1]["balance"] + in24 - out24
        drift = BALANCE - expected
        if abs(drift) > 1:
            recon_drift = round(drift, 1)
except Exception:
    pass
topup_needed = round(max(0, 7 * burn24 - BALANCE * RATE) / RATE, 0) if BALANCE and burn24 else None
guard = {"organic_share": organic_share, "at_risk_usd": round(at_risk * RATE, 2),
         "bal_delta24": round(bal_delta24, 0), "topup24": topup24,
         "recon_drift": recon_drift, "topup_needed": topup_needed,
         "runway_days": runway, "runway_adj": runway_adj, "balance": round(BALANCE, 0) if BALANCE else None,
         "burn24": round(burn24, 2), "burn_prev": round(burn_prev, 2),
         "promised_usd": round(promised, 2), "fx_drift_pct": fx_drift, "rows": grows}

data = {"S": S, "hourly": hourly, "daily": daily, "creators": creators, "other": other, "guard": guard}

# --- append snapshot for delta-notifications (notify.py) ---
hist_path = os.path.join(HERE, "stats_history.json")
hist = json.load(open(hist_path)) if os.path.exists(hist_path) else []
hist.append({
    "ts": now.strftime("%Y-%m-%dT%H:%M"),
    "invoke": S["tot"]["invoke"]["n"], "equip": S["tot"]["equip"]["n"],
    "growth": S["tot"]["growth"]["n"],
    "moca": round(sum(S["tot"][c]["moca"] for c in S["tot"]), 1),
    "creators": S["creators_n"], "rate": RATE,
    "balance": round(BALANCE, 1) if BALANCE else None, "runway_adj": runway_adj,
})
cut = (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M")
json.dump([h for h in hist if h["ts"] >= cut], open(hist_path, "w"))

tpl = open(os.path.join(HERE, "template.html")).read()
tpl = tpl.replace("MOCA rate used $0.008912", f"MOCA rate used ${RATE:.6f}")
out = os.path.join(HERE, "index.html")
open(out, "w").write("<!doctype html>\n<html lang=\"en\">\n" + tpl.replace("/*__DATA__*/", json.dumps(data)) + "\n</html>")
print("wrote", out, "| invokes:", S["tot"]["invoke"]["n"], "| last tx:", S["last_tx"])

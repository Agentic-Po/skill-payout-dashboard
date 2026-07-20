#!/usr/bin/env python3
"""Refresh the Minds treasury wallet dashboard.

Fetches token transfers (MENTE + MOCA) from Blockscout (Base) for the tracked
wallet, merges them into transfers.json / transfers_in.json, recomputes the
two-layer dataset (Layer 1: on-chain facts; Layer 2: AI-inferred interpretation),
and renders index.html from template.html. Writes transfers_export.csv (per-tx,
with rate provenance) so every displayed total ties back to transaction hashes.

Historical day rates are persisted in day_rates.json and never recomputed, so
closed days cannot reprice on later runs. Git history of the hourly commits is
the append-only audit trail of every published figure.
"""
import csv, json, math, os, statistics, time, urllib.request
import posthog_source
from datetime import datetime, timezone, timedelta
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
WALLET = "0xBD956171F5B50936f0Ad1C4db80c022bd2442519"
BASE = f"https://base.blockscout.com/api/v2/addresses/{WALLET}/token-transfers?filter=from"
TOKENS = {
    "MOCA":  {"addr": "0x2b11834ed1feaed4b4b3a86a6f571315e25a884d", "fallback_rate": 0.00831},
    "MENTE": {"addr": "0x4cd9a847f39106e19a4e41aea8a232e915c82af5", "fallback_rate": 0.01414},
}
ADDR2SYM = {v["addr"]: k for k, v in TOKENS.items()}
# counterparty labels confirmed off-chain (platform wallet-mind map / treasury ops)
KNOWN = {"0x9a95d76c41aa34093a0db5f26f97309fe734a07f": "The Gamemaster (mind)",
         "0xd85096faec1ac03075667b4c1a1661f5623bf111": "Cognition Credits collector — also the original SWARM-era treasury+collector hub (pre-Apr 2026)",
         "0xea87169699dabd028a78d4b91544b4298086baf6": "SWARM token contract (original Cognition Credit token, migrated to MENTE ~Apr 2026)",
         "0x8004a169fb4a3325136eb29fa0ceb6d2e539a432": "AgentIdentity registry (historic, ERC-8004 era)",
         "0x7b85e278a7446d8349b066e835d3057d895aecff": "registration-era gas funder (historic)",
         "0xd8506866faadfdcfb9600479ba7dc652a203f111": "⚠ ADDRESS-POISONING MIMIC — fake lookalike of the collector, do NOT use",
         "0xf605dbb5626dfc1448cee33e2e1221103021468f": "primary MENTE funding source — owner unconfirmed (Finance/platform ops?), identification open",
         "0x4d3021a52b31ffafde3c46450d02c72807c3a178": "Minds team Fireblocks wallet",
         "0x1c5ebb794335b72d773df2fd8f80f3d1afbb75dd": "gas funder (sends ETH to mind wallets for cognition spends)"}
# Optional wallet↔mind map (drop wallet_mind_map.csv beside this script — gitignored,
# from the platform's wallet-mind-map export). ONLY the display name is surfaced on
# the public page; emails/IDs stay local and feed the private CSV export labels.
_map_path = os.path.join(HERE, "wallet_mind_map.csv")
if os.path.exists(_map_path):
    import csv as _csv
    with open(_map_path, newline="") as _fh:
        _rd = _csv.DictReader(_fh)
        _cols = {c.lower().strip(): c for c in (_rd.fieldnames or [])}
        _wcol = next((_cols[k] for k in ("wallet", "wallet_address", "address") if k in _cols), None)
        _ncol = next((_cols[k] for k in ("mind_name", "name", "mind") if k in _cols), None)
        n_loaded = 0
        if _wcol:
            for _row in _rd:
                _w = (_row.get(_wcol) or "").strip().lower()
                _nm = (_row.get(_ncol) or "").strip() if _ncol else ""
                if _w.startswith("0x") and _nm and _w not in KNOWN:
                    KNOWN[_w] = _nm + " (mind)"
                    n_loaded += 1
        print(f"wallet_mind_map.csv: {n_loaded} mind labels loaded")

RATES_PATH = os.path.join(HERE, "day_rates.json")
STATE = json.load(open(RATES_PATH)) if os.path.exists(RATES_PATH) else {}
STATE.setdefault("day_rates", {s: {} for s in TOKENS})
STATE.setdefault("last_accepted_rate", {})
STATE.setdefault("recon", {})

def get(url, tries=4):
    for a in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8.4.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception:
            if a == tries - 1:
                raise
            time.sleep(2 * (a + 1))

def key(i):
    return f"{i['transaction_hash']}:{i['log_index']}"

# --- incremental fetch: newest pages until we overlap the cache. If the page
# cap is hit before overlap, the cache is NOT updated (a silent gap would
# become permanent and invisible) — the run renders from the last good cache.
def refresh_cache(base_url, path, pages=100):
    old = json.load(open(path)) if os.path.exists(path) else []
    seen = {key(i) for i in old}
    newest = old[0]["timestamp"] if old else "2026-04-01"
    items, params, overlapped = [], "", False
    for _ in range(pages):
        d = get(base_url + params)
        b = d.get("items", [])
        if not b or not d.get("next_page_params") or (old and b[-1]["timestamp"] < newest):
            items += b
            overlapped = True
            break
        items += b
        params = "&" + "&".join(f"{k}={v}" for k, v in d["next_page_params"].items())
        time.sleep(0.1)
    if not overlapped:
        print(f"WARNING: page cap hit before overlap for {path} — keeping previous cache")
        return old, 0, False
    add = [i for i in items if key(i) not in seen]
    full = sorted(add + old, key=lambda i: i["timestamp"], reverse=True)
    json.dump(full, open(path, "w"))
    return full, len(add), True

full, n_new, ok_out = refresh_cache(BASE, os.path.join(HERE, "transfers.json"))
full_in, n_new_in, ok_in = refresh_cache(BASE.replace("filter=from", "filter=to"), os.path.join(HERE, "transfers_in.json"), pages=60)
data_complete = ok_out and ok_in
print(f"fetched {n_new} new OUT / {n_new_in} new IN, cache {len(full)} out / {len(full_in)} in, complete={data_complete}")

# --- live rates, decimals + balances per token (validated) ---
RATE, RATE_SRC, BALANCE, DECIMALS = {}, {}, {}, {}
for sym, t in TOKENS.items():
    # band against the last accepted live rate (persisted) so a genuine large
    # price move doesn't permanently pin us to a stale source-code constant.
    anchor = STATE["last_accepted_rate"].get(sym) or t["fallback_rate"]
    RATE[sym], RATE_SRC[sym] = anchor, "last-accepted" if sym in STATE["last_accepted_rate"] else "fallback"
    DECIMALS[sym] = 18
    try:
        tok = get(f"https://base.blockscout.com/api/v2/tokens/{t['addr']}")
        DECIMALS[sym] = int(tok.get("decimals") or 18)
        r = float(tok.get("exchange_rate") or 0)
        if 0 < r and anchor / 5 < r < anchor * 5:
            RATE[sym], RATE_SRC[sym] = r, "blockscout"
            # re-anchor only after two consecutive in-band quotes, so one bad
            # quote inside the band can't permanently drag the anchor
            pend = STATE.setdefault("pending_rate", {}).get(sym)
            if pend is not None and pend / 2 < r < pend * 2:
                STATE["last_accepted_rate"][sym] = r
            STATE["pending_rate"][sym] = r
        else:
            print(sym, "rate rejected:", r)
            STATE.setdefault("pending_rate", {}).pop(sym, None)
    except Exception as e:
        print(sym, "rate fetch failed, using", RATE_SRC[sym], ":", e)
    if RATE_SRC[sym] not in ("blockscout",):
        # secondary source: DexScreener pair price (same source the internal dashboard uses)
        try:
            dx = get(f"https://api.dexscreener.com/latest/dex/tokens/{t['addr']}")
            # only pairs where OUR token is the base and the price is sane — DexScreener
            # can list fake/mispriced pools with higher liquidity than the real one
            pairs = [p for p in (dx.get("pairs") or [])
                     if p.get("priceUsd")
                     and (p.get("baseToken", {}).get("address", "").lower() == t["addr"])
                     and anchor / 5 < float(p["priceUsd"]) < anchor * 5]
            if pairs:
                r2 = float(sorted(pairs, key=lambda p: -float(p.get("liquidity", {}).get("usd", 0)))[0]["priceUsd"])
                RATE[sym], RATE_SRC[sym] = r2, "dexscreener"
                STATE["last_accepted_rate"][sym] = r2
        except Exception as e:
            print(sym, "dexscreener fallback failed:", e)
BALANCE = {}
def balance_at(addr, sym, block="latest"):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [
        {"to": addr, "data": "0x70a08231" + "0" * 24 + WALLET[2:].lower()}, block]}).encode()
    req = urllib.request.Request("https://base.blockscout.com/api/eth-rpc", data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "curl/8.4.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return int(json.load(r)["result"], 16) / 10 ** DECIMALS[sym]

# Reconciliation values the balance AT a pinned block so transfers landing
# after the fetch can't fake a drift signal. The pin is the MINIMUM of the two
# caches' newest blocks — both caches are guaranteed complete up to that block,
# so a transfer landing between the sequential OUT/IN fetches can't skew delta.
_newest_out = max([i.get("block_number", 0) for i in full] + [0])
_newest_in = max([i.get("block_number", 0) for i in full_in] + [0])
RECON_BLOCK = min([b for b in (_newest_out, _newest_in) if b] or [0])
RECON_DEGRADED = set()
BALANCE_RECON = {}
for sym, t in TOKENS.items():
    try:
        BALANCE[sym] = balance_at(t["addr"], sym)
    except Exception as e:
        print(sym, "balance fetch failed:", e)
        BALANCE[sym] = None
    try:
        BALANCE_RECON[sym] = balance_at(t["addr"], sym, hex(RECON_BLOCK)) if RECON_BLOCK else BALANCE[sym]
    except Exception as e:
        print(sym, "recon balance fetch failed, using live:", e)
        BALANCE_RECON[sym] = BALANCE[sym]
        RECON_DEGRADED.add(sym)

def norm(i, extra_from=False):
    sym = ADDR2SYM.get(i["token"]["address_hash"].lower())
    if not sym:
        return None
    dec = int(i["total"].get("decimals") or DECIMALS[sym])
    r = {"ts": i["timestamp"][:19], "tok": sym, "val": int(i["total"]["value"]) / 10 ** dec,
         "to": i["to"]["hash"], "tx": i["transaction_hash"], "blk": i.get("block_number", 0)}
    if extra_from:
        r["from"] = i["from"]["hash"]
    return r

rows = sorted(filter(None, (norm(i) for i in full)), key=lambda r: r["ts"], reverse=True)
inflows = sorted(filter(None, (norm(i, True) for i in full_in)), key=lambda r: r["ts"], reverse=True)
excluded_in = len(full_in) - len(inflows)

# --- day-anchored rate oracle ---
# Persisted day rates are immutable. New (unseen) days are computed walking
# BACKWARD from the most recent day — the live rate is a good anchor at the
# recent end, and each day's $0.10 cluster is searched in raw-token space
# using the nearest already-known later day's implied rate, so history can't
# be mispriced by today's quote and closed days never reprice.
for sym in TOKENS:
    persisted = STATE["day_rates"][sym]
    by_day = defaultdict(list)
    for r in rows:
        if r["tok"] == sym:
            by_day[r["ts"][:10]].append(r["val"])
    ref = RATE[sym]
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for d in sorted(by_day, reverse=True):
        if d in persisted:
            ref = persisted[d]
            continue
        target = 0.10 / ref
        seed = [v for v in by_day[d] if target / 2.5 < v < target * 2.5]
        if len(seed) >= 5:
            ref = 0.10 / statistics.median(seed)
            if d == today_utc:
                STATE.setdefault("open_day_rate", {})[sym] = {"d": d, "rate": round(ref, 10)}
            else:
                persisted[d] = round(ref, 10)

def day_rate(sym, ts):
    """Return (rate, source) for a timestamp."""
    d, dr = ts[:10], STATE["day_rates"][sym]
    if d in dr: return dr[d], "day-implied"
    od = STATE.get("open_day_rate", {}).get(sym)
    if od and od["d"] == d: return od["rate"], "day-implied (open)"
    prior = [k for k in sorted(dr) if k <= d]
    later = [k for k in sorted(dr) if k > d]
    if prior: return dr[prior[-1]], "carry-forward"
    if later: return dr[later[0]], "carry-back"
    return RATE[sym], "live"

for r in rows:
    r["rate"], r["rsrc"] = day_rate(r["tok"], r["ts"])
    r["usd"] = r["val"] * r["rate"]
for f in inflows:
    f["rate"], f["rsrc"] = day_rate(f["tok"], f["ts"])
    f["usd"] = f["val"] * f["rate"]

# --- market-price cross-check (era-aware pool OHLCV via GeckoTerminal) ---
# Validates the day-implied payout oracle against external market data. MENTE:
# USDC/MENTE Uniswap pool while it carried the volume, then the MOCA/MENTE
# Aerodrome pool (quote side). MOCA: MOCA/USDC Aerodrome pool. Only new days
# are fetched; stored beside the pinned rates for auditors.
MARKET_POOLS = {
    "MENTE": [("0xd76d44875716a708dbd55cd8ffc3eb1f94acbce3", "base"),
              ("0x2a5eeea4d91042f779ee6014f4f6fd41f375262d", "quote")],
    "MOCA":  [("0x2a5eeea4d91042f779ee6014f4f6fd41f375262d", "base")],
}
STATE.setdefault("market_rates", {})
market_summary = {}
try:
    from datetime import datetime as _dt
    for sym, pools in MARKET_POOLS.items():
        mr = STATE["market_rates"].setdefault(sym, {})
        need_recent = (_dt.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
        if not any(d >= need_recent for d in mr):
            merged = {}
            for pool, side in pools:
                try:
                    dd = get(f"https://api.geckoterminal.com/api/v2/networks/base/pools/{pool}/ohlcv/day?limit=120&token={side}")
                    for ts, o, h, l, c, v in dd["data"]["attributes"]["ohlcv_list"]:
                        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                        if day not in merged or v > merged[day][1]:
                            merged[day] = (c, v)
                    time.sleep(0.5)
                except Exception as e:
                    print(sym, pool, "ohlcv failed:", e)
            for day, (c, v) in merged.items():
                if day not in mr and c:
                    mr[day] = round(c, 8)
        devs = []
        for day, r in STATE["day_rates"][sym].items():
            m = mr.get(day)
            if m:
                devs.append(abs(r - m) / m * 100)
        if devs:
            devs.sort()
            market_summary[sym] = {"n": len(devs), "within15": sum(1 for x in devs if x <= 15),
                                   "median_dev": round(devs[len(devs)//2], 1),
                                   "max_dev": round(max(devs), 1)}
except Exception as e:
    print("market cross-check failed:", e)

# --- cognition consumption (collector wallet inbound = minds spending MENTE) ---
COLLECTOR = "0xd85096fAeC1aC03075667B4C1a1661F5623Bf111"
COG_PATH = os.path.join(HERE, "cognition_in.json")
cognition = None
if os.path.exists(COG_PATH):
    cog = json.load(open(COG_PATH))
    # incremental top-up: newest pages until overlap (same banking pattern)
    try:
        seen_c = {i["transaction_hash"] + ":" + str(i["log_index"]) for i in cog}
        newest_c = cog[0]["ts"] if cog else "2026-04-01"
        params, got_c = "", []
        for _ in range(40):
            dd = get(f"https://base.blockscout.com/api/v2/addresses/{COLLECTOR}/token-transfers?filter=to" + params)
            b = dd.get("items", [])
            stop = not b or not dd.get("next_page_params") or (cog and b[-1]["timestamp"][:19] < newest_c)
            got_c += [{"ts": i["timestamp"][:19], "val": int(i["total"]["value"]) / 10 ** int(i["total"].get("decimals") or DECIMALS["MENTE"]),
                       "from": i["from"]["hash"], "tx": i["transaction_hash"],
                       "log_index": i["log_index"], "transaction_hash": i["transaction_hash"]}
                      for i in b if i["token"].get("address_hash", "").lower() == TOKENS["MENTE"]["addr"]
                      and i["transaction_hash"] + ":" + str(i["log_index"]) not in seen_c]
            if stop: break
            params = "&" + "&".join(f"{k}={v}" for k, v in dd["next_page_params"].items())
            time.sleep(0.1)
        if got_c:
            cog = sorted(got_c + cog, key=lambda i: i["ts"], reverse=True)
            json.dump(cog, open(COG_PATH, "w"))
    except Exception as e:
        print("cognition incremental fetch failed (using cache):", e)
    # exclude non-mind flows into the collector (e.g. from the treasury itself)
    treasury_l = WALLET.lower()
    _payout_recips = {r["to"].lower() for r in rows}
    spends = [c for c in cog if c["from"].lower() != treasury_l and c["from"].lower() in _payout_recips]
    _now = datetime.now(timezone.utc)
    _c7 = (_now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    _c24 = (_now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
    def _cog_usd(rows_):
        return round(sum(c["val"] * (STATE["day_rates"]["MENTE"].get(c["ts"][:10]) or RATE["MENTE"]) for c in rows_), 2)
    cog_daily = {}
    for c in spends:
        d0 = c["ts"][:10]
        e = cog_daily.setdefault(d0, {"n": 0, "mente": 0.0, "minds": set()})
        e["n"] += 1; e["mente"] += c["val"]; e["minds"].add(c["from"].lower())
    # --- funding-source split: who pays for cognition? (Po's user-deposit detection) ---
    # Per-wallet conservation: consumed_i − treasury_credits_i > 0 ⇒ the excess was
    # funded by tokens the user brought (direct deposits, swaps, mind-to-mind).
    # Credits are assumed spent FIRST, so user_funded is a strict lower bound.
    _all_spend = defaultdict(float)   # USD consumed per wallet (any spender except treasury itself)
    for c in cog:
        if c["from"].lower() == treasury_l: continue
        _all_spend[c["from"].lower()] += c["val"] * (STATE["day_rates"]["MENTE"].get(c["ts"][:10]) or RATE["MENTE"])
    _credit = defaultdict(float)      # USD credited per wallet by this treasury (both tokens)
    for r in rows:
        _credit[r["to"].lower()] += r["usd"]
    _user = _tre = 0.0; _n_excess = _n_never = 0
    for w, sp_usd in _all_spend.items():
        cr = _credit.get(w, 0.0)
        ex = max(0.0, sp_usd - cr)
        _user += ex
        _tre += sp_usd - ex
        if ex > 0.01:
            _n_excess += 1
            if cr == 0: _n_never += 1
    funding_split = {"era": "MENTE", "consumed_usd": round(sum(_all_spend.values()), 0),
                     "minds": len(_all_spend),
                     "treasury_funded_usd": round(_tre, 0), "user_funded_usd": round(_user, 0),
                     "user_pct": round(_user / sum(_all_spend.values()) * 100, 1) if _all_spend else 0,
                     "minds_excess": _n_excess, "minds_never_credited": _n_never}
    # SWARM era, same method, from the gen-1 crawl + daily CoinGecko prices
    swarm_split = None
    _sw_path, _sp_path = os.path.join(HERE, "swarm_era.json"), os.path.join(HERE, "swarm_prices.json")
    if os.path.exists(_sw_path) and os.path.exists(_sp_path):
        _se = json.load(open(_sw_path)); _sp = json.load(open(_sp_path))
        _ss = defaultdict(float); _sr = defaultdict(float)
        for r0 in _se["in"]:  _ss[r0["cp"].lower()] += r0["val"] * _sp.get(r0["ts"][:10], 0.001)
        for r0 in _se["out"]: _sr[r0["cp"].lower()] += r0["val"] * _sp.get(r0["ts"][:10], 0.001)
        _su = _st = 0.0; _sn = 0
        for w, sp_usd in _ss.items():
            ex = max(0.0, sp_usd - _sr.get(w, 0.0))
            _su += ex; _st += sp_usd - ex
            if ex > 0.01: _sn += 1
        swarm_split = {"era": "SWARM", "consumed_usd": round(sum(_ss.values()), 0), "minds": len(_ss),
                       "treasury_funded_usd": round(_st, 0), "user_funded_usd": round(_su, 0),
                       "user_pct": round(_su / sum(_ss.values()) * 100, 1) if _ss else 0,
                       "minds_excess": _sn}

    cognition = {"funding_split": funding_split, "swarm_split": swarm_split,
                 "total_n": len(spends), "total_mente": round(sum(c["val"] for c in spends), 0),
                 "total_usd": _cog_usd(spends),
                 "usd_7d": _cog_usd([c for c in spends if c["ts"] > _c7]),
                 "n_24h": sum(1 for c in spends if c["ts"] > _c24),
                 "minds_all": len({c["from"].lower() for c in spends}),
                 "minds_7d": len({c["from"].lower() for c in spends if c["ts"] > _c7}),
                 "range_from": spends[-1]["ts"][:10] if spends else None,
                 "crawl_complete": bool(spends) and spends[-1]["ts"][:10] <= "2026-04-30",
                 "daily": [{"d": d0, "n": v["n"], "mente": round(v["mente"], 1), "minds": len(v["minds"])}
                           for d0, v in sorted(cog_daily.items())[-30:]]}

# --- Generation-1 economy: the SWARM era (closed history, computed from
# swarm_era.json + CoinGecko daily prices; both files are static archives) ---
swarm_era = None
_se_path = os.path.join(HERE, "swarm_era.json")
_sp_path = os.path.join(HERE, "swarm_prices.json")
if os.path.exists(_se_path) and os.path.exists(_sp_path):
    _se = json.load(open(_se_path))
    _sp = json.load(open(_sp_path))
    def _susd(rows_):
        return sum(r["val"] * _sp.get(r["ts"][:10], 0.001) for r in rows_)
    _o, _i = _se["out"], _se["in"]
    _md = defaultdict(lambda: {"out": 0.0, "inn": 0.0, "out_usd": 0.0, "in_usd": 0.0})
    for r in _o:
        m = r["ts"][:7]; _md[m]["out"] += r["val"]; _md[m]["out_usd"] += r["val"] * _sp.get(r["ts"][:10], 0.001)
    for r in _i:
        m = r["ts"][:7]; _md[m]["inn"] += r["val"]; _md[m]["in_usd"] += r["val"] * _sp.get(r["ts"][:10], 0.001)
    _spenders = {r["cp"].lower() for r in _i}
    _topped = {r["cp"].lower() for r in _o}
    swarm_era = {"out_swarm": round(sum(r["val"] for r in _o)), "in_swarm": round(sum(r["val"] for r in _i)),
                 "out_usd": round(_susd(_o), 2), "in_usd": round(_susd(_i), 2),
                 "out_tx": len(_o), "in_tx": len(_i),
                 "minds_topped": len(_topped), "minds_spent": len(_spenders),
                 "minds_both": len(_topped & _spenders),
                 "first_spend": min(r["ts"] for r in _i)[:10], "last_spend": max(r["ts"] for r in _i)[:10],
                 "monthly": [{"m": m, "out": round(v["out"]), "inn": round(v["inn"]),
                              "out_usd": round(v["out_usd"], 2), "in_usd": round(v["in_usd"], 2)}
                             for m, v in sorted(_md.items())]}

# ============================ LAYER 1 — FACTS ============================
now = datetime.now(timezone.utc)
today = now.strftime("%Y-%m-%d")
cut24 = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
cut48 = (now - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S")
cut7 = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
cut30 = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")

GRID_POINTS = [(0.10, "b010"), (1, "b1"), (3, "b3"), (5, "b5"), (10, "b10"), (25, "b25"), (50, "b50")]
def band(usd):
    if usd < 0.06: return "micro"
    for p, k in GRID_POINTS:
        if abs(usd - p) / p <= 0.08:
            return k
    return "other"
BAND_LABEL = {"micro": "< $0.06", "b010": "≈ $0.10", "b1": "≈ $1", "b3": "≈ $3", "b5": "≈ $5",
              "b10": "≈ $10", "b25": "≈ $25", "b50": "≈ $50", "other": "other size"}
BAND_KEYS = list(BAND_LABEL)

RECYCLE_SRC = "0xd85096faec1ac03075667b4c1a1661f5623bf111"
def facts_window(rs, ins, label):
    out_usd = sum(r["usd"] for r in rs)
    in_usd = sum(f["usd"] for f in ins)
    in_recycled = sum(f["usd"] for f in ins if f["from"].lower() == RECYCLE_SRC)
    return {"label": label,
            "out_usd": round(out_usd, 2), "in_usd": round(in_usd, 2),
            "in_recycled_usd": round(in_recycled, 2),
            "in_external_usd": round(in_usd - in_recycled, 2),
            "net_usd": round(in_usd - out_usd, 2),
            "out_tx": len(rs), "in_tx": len(ins),
            "out_wallets": len({r["to"] for r in rs}),
            "in_sources": len({f["from"] for f in ins}),
            "out_usd_tok": {s: round(sum(r["usd"] for r in rs if r["tok"] == s), 2) for s in TOKENS},
            "out_raw": {s: round(sum(r["val"] for r in rs if r["tok"] == s), 1) for s in TOKENS},
            "in_raw": {s: round(sum(f["val"] for f in ins if f["tok"] == s), 1) for s in TOKENS}}

def win(cut, hi=None):
    rs = [r for r in rows if r["ts"] > cut and (hi is None or r["ts"] <= hi)]
    ins = [f for f in inflows if f["ts"] > cut and (hi is None or f["ts"] <= hi)]
    return rs, ins

windows = [facts_window(*win(c), lab) for c, lab in
           [(cut24, "24h"), (cut7, "7d"), (cut30, "30d"), ("0", "all history")]]
prev24 = facts_window(*win(cut48, cut24), "prev 24h")

range_from = rows[-1]["ts"][:10] if rows else None
months = sorted({r["ts"][:7] for r in rows} | {f["ts"][:7] for f in inflows})
def mlabel(m):
    if m == today[:7]: return m + " (partial)"
    if range_from and m == range_from[:7] and not range_from.endswith("-01"):
        return m + f" (from {range_from})"
    return m
monthly = [facts_window([r for r in rows if r["ts"][:7] == m],
                        [f for f in inflows if f["ts"][:7] == m], mlabel(m)) for m in months]

days = sorted({r["ts"][:10] for r in rows} | {f["ts"][:10] for f in inflows})
daily = []
for d in days:
    rs = [r for r in rows if r["ts"][:10] == d]
    ins = [f for f in inflows if f["ts"][:10] == d]
    bc, bu = Counter(), defaultdict(float)
    bw = defaultdict(set)
    for r in rs:
        b = band(r["usd"]); bc[b] += 1; bu[b] += r["usd"]; bw[b].add(r["to"])
    out_usd = sum(r["usd"] for r in rs)
    in_usd = sum(f["usd"] for f in ins)
    daily.append({"d": d, "partial": d == today,
                  "out_usd": round(out_usd, 2), "in_usd": round(in_usd, 2),
                  "net_usd": round(in_usd - out_usd, 2),
                  "out_tx": len(rs), "wallets": len({r["to"] for r in rs}),
                  "tok_raw": {s: round(sum(r["val"] for r in rs if r["tok"] == s), 1) for s in TOKENS},
                  "bands": {k: {"n": bc[k], "usd": round(bu[k], 2), "w": len(bw[k])} for k in bc}})

wal_days = defaultdict(set)
for r in rows:
    wal_days[r["to"]].add(r["ts"][:10])
repeat_wallets = sum(1 for v in wal_days.values() if len(v) >= 2)

recip = defaultdict(lambda: {"n": 0, "usd": 0.0, "days": set(), "first": "9999", "last": "0"})
for r in rows:
    a = recip[r["to"]]
    a["n"] += 1; a["usd"] += r["usd"]
    a["days"].add(r["ts"][:10])
    a["first"] = min(a["first"], r["ts"]); a["last"] = max(a["last"], r["ts"])
tot_out_usd = sum(r["usd"] for r in rows) or 1
top_recip = sorted(({"addr": k, "label": KNOWN.get(k.lower()), "n": v["n"], "usd": round(v["usd"], 2),
                     "days": len(v["days"]), "first": v["first"][:10], "last": v["last"][:10],
                     "share": round(v["usd"] / tot_out_usd * 100, 1)}
                    for k, v in recip.items()), key=lambda x: -x["usd"])[:25]

# inflow sources (factual, labeled where known)
src = defaultdict(lambda: {"n": 0, "usd": 0.0, "first": "9999", "last": "0"})
for f in inflows:
    a = src[f["from"]]
    a["n"] += 1; a["usd"] += f["usd"]
    a["first"] = min(a["first"], f["ts"]); a["last"] = max(a["last"], f["ts"])
in_sources = sorted(({"addr": k, "label": KNOWN.get(k.lower()), "n": v["n"], "usd": round(v["usd"], 2),
                      "first": v["first"][:10], "last": v["last"][:10]}
                     for k, v in src.items()), key=lambda x: -x["usd"])[:10]

byh = defaultdict(Counter)
for r in rows:
    if r["ts"] > cut7:
        byh[r["ts"][:13]][r["tok"]] += 1
h0 = datetime.fromisoformat(cut7[:13] + ":00:00").replace(tzinfo=timezone.utc)
hourly, h = [], h0
while h <= now:
    k = h.strftime("%Y-%m-%dT%H")
    hourly.append({"h": k, **{sym: byh[k][sym] for sym in TOKENS}})
    h += timedelta(hours=1)

# balance reconciliation: cache-lifetime net flow vs live balance, per token.
# The delta should be CONSTANT run-over-run (pre-cache history is fixed) — a
# moving delta means missed transfers, so drift is tracked and flagged.
recon = {}
for sym in TOKENS:
    if BALANCE_RECON.get(sym) is None:
        recon[sym] = None
        continue
    net = (sum(f["val"] for f in inflows if f["tok"] == sym and f["blk"] <= RECON_BLOCK)
           - sum(r["val"] for r in rows if r["tok"] == sym and r["blk"] <= RECON_BLOCK))
    delta = round(BALANCE_RECON[sym] - net, 1)
    prev = STATE["recon"].get(sym)
    clean = data_complete and sym not in RECON_DEGRADED
    drift = round(delta - prev, 1) if prev is not None and clean else None
    if clean:
        STATE["recon"][sym] = delta
    recon[sym] = {"net_cached": round(net, 1), "balance": round(BALANCE_RECON[sym], 1),
                  "delta": delta, "drift": drift, "degraded": sym in RECON_DEGRADED,
                  "warn": bool(drift is not None and (drift > 30 or drift < -150))}

facts = {"windows": windows, "prev24": prev24, "monthly": monthly, "daily": daily, "hourly": hourly,
         "top_recipients": top_recip, "in_sources": in_sources,
         "wallets_all": len(wal_days), "wallets_repeat": repeat_wallets,
         "balance": {s: (round(BALANCE[s], 0) if BALANCE[s] is not None else None) for s in TOKENS},
         "balance_usd": {s: (round(BALANCE[s] * RATE[s], 0) if BALANCE[s] is not None else None) for s in TOKENS},
         "rate": RATE, "rate_src": RATE_SRC, "recon": recon,
         "market_check": market_summary, "cognition": cognition, "swarm_era": swarm_era,
         "band_labels": BAND_LABEL, "band_keys": BAND_KEYS,
         "range": {"from": range_from, "to": rows[0]["ts"][:19] if rows else None}}

# ======================= LAYER 2 — INTERPRETATION =======================
FINE = {"b010": "invoke", "b1": "equip", "b3": "$3 credit", "b5": "referral $5",
        "b10": "stripe $10", "b25": "stripe $25", "b50": "stripe $50", "micro": "test"}
COARSE = {"invoke": "invoke", "equip": "equip", "test": "micro"}
def classify(r):
    fine = FINE.get(band(r["usd"]))
    if fine is None:
        # off-grid transfers are NOT folded into invoke/growth — they stay a
        # separate, visible category so classified totals only contain snapped rows
        return "nonstandard", ("nonstandard (small)" if r["usd"] < 0.5 else "nonstandard (large)")
    return COARSE.get(fine, "growth"), fine

for r in rows:
    r["cat"], r["fine"] = classify(r)

CATS = ["invoke", "equip", "growth", "nonstandard", "micro"]
S = {"tot": {c: {"n": sum(1 for r in rows if r["cat"] == c),
                 "usd": round(sum(r["usd"] for r in rows if r["cat"] == c), 2)}
             for c in CATS},
     "inv24": sum(1 for r in rows if r["cat"] == "invoke" and r["ts"] > cut24),
     "eq24": sum(1 for r in rows if r["cat"] == "equip" and r["ts"] > cut24)}

cr = defaultdict(lambda: {"invoke": 0, "equip": 0, "usd": 0.0})
for r in rows:
    if r["cat"] in ("invoke", "equip"):
        cr[r["to"]][r["cat"]] += 1
        cr[r["to"]]["usd"] += r["usd"]
creators = sorted(({"addr": a, "label": KNOWN.get(a.lower()), "invoke": d["invoke"], "equip": d["equip"],
                    "usd": round(d["usd"], 2)} for a, d in cr.items()), key=lambda x: -x["usd"])
S["creators_n"] = len(creators)
ce_total = round(sum(c["usd"] for c in creators), 2) or 1

fine_agg = defaultdict(lambda: {"n": 0, "usd": 0.0})
for r in rows:
    fine_agg[r["fine"]]["n"] += 1
    fine_agg[r["fine"]]["usd"] += r["usd"]
fine_table = sorted(({"fine": k, "n": v["n"], "usd": round(v["usd"], 2)} for k, v in fine_agg.items()),
                    key=lambda x: -x["usd"])

def gap_entropy(gaps):
    bins = [30, 120, 600, 3600, 21600]
    hist = Counter(next((i for i, e in enumerate(bins) if g < e), len(bins)) for g in gaps)
    n = len(gaps)
    H = -sum((c / n) * math.log(c / n) for c in hist.values())
    return max(H / math.log(len(bins) + 1), 0.0)

def acf1(gaps):
    if len(gaps) < 3: return 0.0
    a, b = gaps[:-1], gaps[1:]
    ma, mb = statistics.mean(a), statistics.mean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    return num / den if den else 0.0

inc_recip = {r["to"] for r in rows if r["fine"] in ("$3 credit", "referral $5")}
by_addr = defaultdict(list)
for r in rows:
    if r["cat"] == "invoke":
        by_addr[r["to"]].append(datetime.fromisoformat(r["ts"]))
inv_counts = sorted(len(v) for v in by_addr.values())
vol_hi = max(15, inv_counts[int(len(inv_counts) * 0.95)] if len(inv_counts) >= 20 else 10**9)
grows = []
for c in creators:
    ts = sorted(by_addr.get(c["addr"], []))
    n = len(ts)
    if n < 10:
        continue
    gaps = [(b - a).total_seconds() for a, b in zip(ts, ts[1:])]
    ent, ac = gap_entropy(gaps), acf1(gaps)
    burst = max(sum(1 for t2 in ts if 0 <= (t2 - t1).total_seconds() <= 600) for t1 in ts) / n
    span_h = (ts[-1] - ts[0]).total_seconds() / 3600
    flags = []
    if n >= 30 and ent < 0.45: flags.append("uniform cadence")
    if n >= 30 and abs(ac) > max(0.6, 2 / math.sqrt(n - 1)): flags.append("scripted pattern")
    if n >= 15 and burst > 0.7 and span_h > 2: flags.append("burst cluster")
    tags = ["high volume"] if n > vol_hi else []
    if c["addr"] in inc_recip: tags.append("earn+receive")
    grows.append({"addr": c["addr"], "label": KNOWN.get(c["addr"].lower()), "n": n,
                  "span_h": round(span_h, 1), "ent": round(ent, 2), "acf": round(ac, 2),
                  "burst": round(burst * 100), "usd": c["usd"],
                  "flags": flags, "tags": tags, "status": "review" if flags else "organic"})
credit_recip = {r["to"] for r in rows if r["fine"] == "$3 credit"}
earner_addrs = {c["addr"] for c in creators}
loop_wallets = credit_recip & earner_addrs
loop_usd = round(sum(c["usd"] for c in creators if c["addr"] in loop_wallets), 2)
loop_gt10 = sum(1 for c in creators if c["addr"] in loop_wallets and c["usd"] > 10)
loop_gt50 = sum(1 for c in creators if c["addr"] in loop_wallets and c["usd"] > 50)
grows.sort(key=lambda g: (g["status"] != "review", -g["usd"]))
flagged = [g for g in grows if g["status"] == "review"]
at_risk = round(sum(g["usd"] for g in flagged), 2)

# projections. All burn/outflow figures use the same day-implied USD as the
# facts layer (out_di equals the factual outflow; unbacked burn_di is a strict
# subset of it); the balance side is valued at the live rate — bases stated on
# the tiles.
STRIPE_FINE = ("stripe $10", "stripe $25", "stripe $50")
def burn_di(cut, hi=None):
    """Unbacked classified burn: invoke/equip/credits/referrals, excluding
    Stripe-sized deliveries (fiat-purchase-backed)."""
    return sum(r["usd"] for r in rows if r["ts"] > cut and (hi is None or r["ts"] <= hi)
               and r["cat"] in ("invoke", "equip", "growth") and r["fine"] not in STRIPE_FINE)
def out_di(cut, hi=None):
    """Total factual outflow (every category incl. nonstandard/micro)."""
    return sum(r["usd"] for r in rows if r["ts"] > cut and (hi is None or r["ts"] <= hi))
bal_usd = sum((BALANCE[s] or 0) * RATE[s] for s in TOKENS)
burn24 = burn_di(cut24)
burn_prev = burn_di(cut48, cut24)
span_days = max(min(7.0, (now - datetime.fromisoformat(rows[-1]["ts"]).replace(tzinfo=timezone.utc)).total_seconds() / 86400), 1.0) if rows else 7.0
burn7avg = burn_di(cut7) / span_days
out7avg = out_di(cut7) / span_days
runway24 = round(bal_usd / burn24, 1) if burn24 > 0 else None
runway7 = round(bal_usd / burn7avg, 1) if burn7avg > 0 else None
runway_total = round(bal_usd / out7avg, 1) if out7avg > 0 else None

guard = {"flagged_n": len(flagged), "monitored_n": len(grows), "at_risk_usd": at_risk,
         "loop_n": len(loop_wallets), "loop_usd": loop_usd,
         "loop_gt10": loop_gt10, "loop_gt50": loop_gt50,
         "credit_recip_n": len(credit_recip),
         "ce_total_usd": ce_total,
         "runway24": runway24, "runway7": runway7, "runway_total": runway_total, "bal_usd": round(bal_usd, 0),
         "burn24": round(burn24, 2), "burn_prev": round(burn_prev, 2),
         "burn7avg": round(burn7avg, 2), "rows": grows}

# permanent Stripe snapshot (verified server-side revenue reference)
stripe_snap = None
_snap_path = os.path.join(HERE, "stripe_snapshot.json")
if os.path.exists(_snap_path):
    stripe_snap = json.load(open(_snap_path))
    # distribution over the same period, for a like-for-like subsidy ratio
    _p0, _p1 = stripe_snap["period"]
    _dist = sum(r["usd"] for r in rows if _p0 <= r["ts"][:10] <= _p1
                and r["cat"] in ("invoke", "equip", "growth") and r["fine"] not in STRIPE_FINE)
    stripe_snap["period_unbacked_dist_usd"] = round(_dist, 2)
    _proceeds = stripe_snap.get("net_proceeds_est_usd") or (stripe_snap["net_usd"] - stripe_snap["fees_est_usd"])
    stripe_snap["period_subsidy_ratio"] = round(_dist / _proceeds, 1) if _proceeds else None

infer = {"S": S, "creators": creators[:25], "ce_total": ce_total, "fine_table": fine_table, "guard": guard}

# ================= SERVER-RECORDED TIER (PostHog, optional) =================
# Middle trust tier: platform-recorded events (client-confirmed top-ups, mind
# awakenings, WAU/MAU). Not on-chain truth, but independent of size-inference.
server = None
try:
    _ph = posthog_source.fetch()
except Exception as _e:
    print("posthog tier unavailable:", _e)
    _ph = None
if _ph and _ph.get("daily"):
    _closed = sorted(d for d in _ph["daily"] if _ph["daily"][d].get("settled"))[-7:]
    _pd = [_ph["daily"][d] for d in _closed]
    ph_topup_usd = round(sum(x["topup_usd"] for x in _pd), 2)
    ph_topups = sum(x["topups"] for x in _pd)
    ph_awakens = sum(x["awakens"] for x in _pd)
    # divergence control: our stripe-sized outflows over the same closed days
    stripe_out = round(sum(r["usd"] for r in rows if r["ts"][:10] in _closed and r["fine"] in STRIPE_FINE), 2)
    wau = _ph.get("wau")
    cost_wau = round(burn7avg * 7 / wau, 2) if wau else None
    unbacked_7d = round(sum(r["usd"] for r in rows if r["ts"][:10] in _closed
                            and r["cat"] in ("invoke", "equip", "growth") and r["fine"] not in STRIPE_FINE), 2)
    subsidy_ratio = round(unbacked_7d / ph_topup_usd, 1) if ph_topup_usd else None
    # weekly ratio trend over all settled platform days (the slope is the thesis test)
    _settled_all = sorted(d for d in _ph["daily"] if _ph["daily"][d].get("settled"))
    ratio_weeks = []
    for i in range(0, len(_settled_all) - 6, 7):
        wk = _settled_all[len(_settled_all) - 7 - i:len(_settled_all) - i]
        if len(wk) < 7: break
        t = sum(_ph["daily"][d]["topup_usd"] for d in wk)
        u = round(sum(r["usd"] for r in rows if r["ts"][:10] in wk
                      and r["cat"] in ("invoke", "equip", "growth") and r["fine"] not in STRIPE_FINE), 2)
        ratio_weeks.append({"end": wk[-1], "ratio": round(u / t, 1) if t else None, "topup": round(t, 2), "unbacked": u})
    ratio_weeks.reverse()
    server = {"days": [_closed[0], _closed[-1]] if _closed else None,
              "topup_usd": ph_topup_usd, "topups": ph_topups,
              "stripe_out_usd": stripe_out,
              "diverge_usd": round(stripe_out - ph_topup_usd, 2),
              "diverge_meta": {"owner": "Po", "opened": "2026-07-18",
                               "status": "open — not yet reconcilable: client-side events are lossy; blocked on the hm_events Stripe-webhook export to PostHog (asked of the data team)"},
              "unbacked_7d": unbacked_7d, "subsidy_ratio": subsidy_ratio, "ratio_weeks": ratio_weeks,
              "awakens7": ph_awakens, "wau": wau, "mau": _ph.get("mau"),
              "cost_per_wau": cost_wau,
              "fetched": _ph.get("fetched"), "complete": _ph.get("complete", False),
              "daily": {d: _ph["daily"][d] for d in sorted(_ph["daily"])[-30:]}}

scope = {"wallet": WALLET, "tokens": {s: TOKENS[s]["addr"] for s in TOKENS},
         "generated": now.strftime("%Y-%m-%d %H:%M"),
         "source": "Blockscout (Base) token-transfer API; balances via eth_call; live prices via Blockscout exchange_rate (validated); historical USD via persisted day-implied payout rates",
         "complete": data_complete, "excluded_in_tx": excluded_in,
         "note": "This wallet only. Other Minds wallets (e.g. Fireblocks) are out of scope."}

# ---- computed guided-view layer: insights, open items, gaps (panel-designed) ----
dist_pace = round(out_di(cut7) / span_days + sum(r["usd"] for r in rows if r["ts"] > cut7 and r["cat"] in ("nonstandard", "micro")) / span_days, 2)
_cons_ratio = round(cognition["total_usd"] / facts["windows"][3]["out_usd"] * 100) if cognition else None
_rec_share = round(facts["windows"][3]["in_recycled_usd"] / facts["windows"][3]["in_usd"] * 100) if facts["windows"][3]["in_usd"] else 0
insights = {
    "diagram": "Every token here is a unit of cognition — this diagram is the economy; the rest of the page is its measurements.",
    "flows": f"Outflow is the signal: ~${round(facts['windows'][1]['out_usd']/7):,}/day of distribution IS the ecosystem's activity. Inflow is manual treasury logistics keeping the wallet alive — {_rec_share}% of lifetime inflow is usage fees recycling back.",
    "daily": f"Watch the pulse, not the balance: distribution spikes mark campaigns and growth pushes; the current pace is ~${round(facts['windows'][1]['out_usd']/7):,}/day.",
    "cognition": (f"{_cons_ratio}% of everything ever distributed has been spent on real cognition — demand matches supply; this is the number that makes every other number mean something." if _cons_ratio else "Demand-side data loading."),
    "recipients": "10,000+ wallets hold verifiable on-chain earnings history — the property layer. (This table counts only transfers FROM this treasury wallet.)",
    "sources": "All inflow is deliberate ops — treasury refills and collector recycling. Every source wallet should carry a label; unlabeled = ask treasury ops.",
    "server": "The off-chain shadow (Stripe checkout events only): where it disagrees with the chain is exactly where our data gaps live — revenue figures are floors until the Stripe export lands.",
    "aizone": f"Best-guess triage, never fact: distribution is mostly invoke-sized, ~{guard['runway_total'] or '?'} days of float at current pace, and ${guard['at_risk_usd']:,} ({round(guard['at_risk_usd']/guard['ce_total_usd']*100,1)}% of creator earnings) looks unusual — nothing confirmed.",
}
open_items = [
    {"item": "Confirm MENTE burn mechanism (event-less balance changes, ~$1,250 lifetime; sample txs 0x0080584a…, 0xc9f7afc5… in block 45862329)", "type": "clarify", "owner": "Po → MENTE team", "opened": "2026-07-19", "anchor": "scope"},
    {"item": "Reconcile Stripe-sized outflow vs recorded top-ups — Valerii holds Stripe API access (feeds PostHog) and can close this end-to-end", "type": "follow-up", "owner": "Po → Valerii (Stripe API access confirmed)", "opened": "2026-07-18", "anchor": "serverCard"} if server else None,
    {"item": "Identify owner of 0xf605dBb5…1468f — the primary MENTE funder (4.88M MENTE lifetime incl. an unattributed ~$6K on Jul 7); Po's Fireblocks and the collector are confirmed, three small early funders remain unlabeled", "type": "clarify", "owner": "Po + Finance/treasury ops", "opened": "2026-07-19", "anchor": "srcT"},
    {"item": "Formalize the recycle policy: collector→treasury flows are informal ops habit today — defining the rule defines who owns the economy's cash flow", "type": "clarify", "owner": "Po → Minh / platform", "opened": "2026-07-19", "anchor": "srcT"},
    {"item": "Subsidy-ratio trend: watch whether the weekly ratio bends down as revenue features land", "type": "trend", "owner": "dashboard (auto)", "opened": "2026-07-19", "anchor": "serverCard"},
    {"item": "Manual heartbeat: wallet stays solvent only by hand-refills — standing replenishment policy pending Minh", "type": "follow-up", "owner": "Po → Minh", "opened": "2026-07-19", "anchor": "plainStrip"},
]
open_items = [o for o in open_items if o]
gaps = [
    {"missing": "SWARM era (pre-Apr 2026) not yet integrated", "effect": "this dashboard covers the MENTE/MOCA credit era (from Apr 12/24); the economy's first generation ran on SWARM (Ethoswarm token) through the SAME collector hub 0xd850… — those flows are not yet counted", "unlocks": "full multi-era economy history: crawl the collector's SWARM in/outflows and add an era-aware timeline"},
    {"missing": "Recycle policy (constitutional)", "effect": "collector→treasury flows are informal; ownership of the economy's cash flow undefined", "unlocks": "closed-loop rule, creator revenue-share, or burn discipline — a protocol instead of a babysat wallet"},
    {"missing": "Complete Stripe data feed — Valerii has Stripe API access (pulls for PostHog today, but only client-side events land)", "effect": "live revenue still client-side only; an interim VERIFIED snapshot (Stripe CSV, May 13–Jul 15: net $6,455) now anchors the true numbers — live feed needed for ongoing days", "unlocks": "true revenue-backed split; divergence control closes"},
    {"missing": "Per-transfer memo/event from the payout contract", "effect": "classification is size-inference (±8%); amber zone larger than it needs to be", "unlocks": "exact payout types — most of the amber zone becomes fact"},
    {"missing": "Wallet↔mind map (Katherine)", "effect": "recipients are hex addresses; per-creator economics invisible", "unlocks": "named earnings leaderboard + per-wallet hold/spend/exit disposition — retires the farming debate with data"},
    {"missing": "MENTE burn-mechanism confirmation (platform)", "effect": "~1.2% of MENTE flow explained forensically but unconfirmed", "unlocks": "complete, auditable MENTE accounting; event emission restores full verifiability"},
    {"missing": "Fireblocks wallet scope", "effect": "the $50K manual-support wallet is invisible to this dashboard", "unlocks": "whole-treasury view; no separate manual attestation needed"},
]
guard["dist_pace"] = dist_pace

# ================= COLLECTOR OUTFLOW — recycle vs idle sink =================
# Collected MENTE used to recycle back into this treasury daily. On 2026-06-19 that
# leg was redirected to a holding wallet that has never sent anything out. Both legs
# are measured here so the handover (and the fact the loop no longer closes) is visible.
SINK = "0xf0961686bC71B8A1f42E7888bD8160e9B6240f40"
sink = None
try:
    def _sweep(direction):
        acc, params = [], ""
        for _ in range(20):
            dd = get(f"https://base.blockscout.com/api/v2/addresses/{SINK}/token-transfers?filter={direction}" + params)
            b = dd.get("items", [])
            acc += [{"ts": i["timestamp"][:19],
                     "val": int(i["total"]["value"]) / 10 ** int(i["total"].get("decimals") or DECIMALS["MENTE"]),
                     "cp": (i["from"] if direction == "to" else i["to"])["hash"]}
                    for i in b if i["token"].get("address_hash", "").lower() == TOKENS["MENTE"]["addr"]]
            if not b or not dd.get("next_page_params"):
                break
            params = "&" + "&".join(f"{k}={v}" for k, v in dd["next_page_params"].items())
            time.sleep(0.1)
        return acc

    _in, _out = _sweep("to"), _sweep("from")
    _sd = defaultdict(float)
    for r in _in:
        if r["cp"].lower() == COLLECTOR.lower():
            _sd[r["ts"][:10]] += r["val"]
    _rd = defaultdict(float)                       # the old leg: collector -> treasury
    for f in inflows:
        if f["from"].lower() == COLLECTOR.lower():
            _rd[f["ts"][:10]] += f["val"]
    _ci = defaultdict(float)                       # collector intake, for the sweep rate
    for c in (cog if "cog" in dir() else []):
        _ci[c["ts"][:10]] += c["val"]
    # collector's own MENTE balance — the ~60% that never leaves under either route
    _bal = None
    try:
        for _b in get(f"https://base.blockscout.com/api/v2/addresses/{COLLECTOR}/token-balances"):
            if (_b.get("token") or {}).get("address_hash", "").lower() == TOKENS["MENTE"]["addr"]:
                _bal = int(_b["value"]) / 10 ** int(_b["token"]["decimals"])
    except Exception as _e:
        print("collector balance fetch failed:", _e)

    _sdays, _rdays = sorted(_sd), sorted(_rd)
    if _sdays:
        # the sweep tracks the PRIOR day's intake far more tightly than same-day
        # (stdev ~10pp vs ~32pp), so the rate is stated on a T-1 basis.
        _sh = []
        for d in _sdays:
            _p = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
            if _ci.get(_p):
                _sh.append(_sd[d] / _ci[_p])
        _cum_i = sum(v for d, v in _ci.items() if d >= _sdays[0])
        # old route measured on the same cumulative basis, so the two are comparable
        _rdaily = [d for d in _rdays if d >= "2026-05-21"]           # its daily-cadence phase
        _rwin = sum(v for d, v in _ci.items() if _rdays and _rdays[0] <= d <= _rdays[-1])
        # one continuous series across the boundary: intake vs each route
        _all_days = sorted(set(list(_ci) + _rdays + _sdays))
        _all_days = [d for d in _all_days if _rdays and d >= _rdays[0]]
        _mrate = RATE.get("MENTE") or TOKENS["MENTE"]["fallback_rate"]
        _pre = [v for d, v in _ci.items() if _rdaily and _rdaily[0] <= d <= _rdaily[-1]]
        _post = [v for d, v in _ci.items() if d >= _sdays[0]]
        sink = {"addr": SINK, "days": len(_sdays), "first": _sdays[0], "last": _sdays[-1],
                "total": round(sum(_sd.values()), 2),
                "daily": [{"d": d, "val": round(_sd[d], 2)} for d in _sdays],
                "series": [{"d": d, "i": round(_ci.get(d, 0), 1),
                            "r": round(_rd.get(d, 0), 1), "s": round(_sd.get(d, 0), 1)}
                           for d in _all_days],
                "boundary": _sdays[0],
                "out_n": len(_out), "out_total": round(sum(r["val"] for r in _out), 2),
                "share_median": round(statistics.median(_sh) * 100, 1) if _sh else None,
                "share_cum": round(sum(_sd.values()) / _cum_i * 100, 1) if _cum_i else None,
                "recycle_total": round(sum(_rd.values()), 2), "recycle_n": len(_rdays),
                "recycle_first": _rdays[0] if _rdays else None,
                "recycle_last": _rdays[-1] if _rdays else None,
                "recycle_share_cum": round(sum(_rd.values()) / _rwin * 100, 1) if _rwin else None,
                "recycle_span_days": (datetime.strptime(_rdays[-1], "%Y-%m-%d")
                                      - datetime.strptime(_rdays[0], "%Y-%m-%d")).days + 1 if _rdays else None,
                "collector_bal": round(_bal, 2) if _bal else None,
                "rate": _mrate,
                "intake_pre": round(statistics.mean(_pre)) if _pre else None,
                "intake_post": round(statistics.mean(_post)) if _post else None}
except Exception as e:
    print("sink fetch failed:", e)

# ================= ADDRESS REGISTRY =================
# Every material participant in the loop, with the FULL address. The rest of the
# page truncates to 0xXXXXXXXX…XXXX, which is unsafe here: the collector has a
# poisoning mimic that is identical under that truncation. Built from the pinned
# constants + KNOWN, then auto-extended with any high-volume counterparty that
# has no label yet, so the panel stays complete as new wallets appear.
def _reg(addr, role, group, warn=False):
    return {"addr": addr, "role": role, "group": group, "warn": warn}

registry = [
    _reg(WALLET, "Treasury Distribution wallet — the subject of this dashboard", "Treasury"),
    _reg(COLLECTOR, "Cognition Credits collector — minds pay MENTE here per request; recycled to treasury until 2026-06-18, now swept to the holding wallet below", "Collector"),
    _reg(TOKENS["MENTE"]["addr"], "MENTE token contract — the current cognition credit", "Token contracts"),
    _reg(TOKENS["MOCA"]["addr"], "MOCA token contract — counted by USD value, auto-swaps to MENTE", "Token contracts"),
    _reg("0xea87169699dabd028a78d4b91544b4298086baf6", "SWARM token contract — generation-1 credit (Ethoswarm), migrated ~Apr 2026", "Token contracts"),
    _reg(MARKET_POOLS["MENTE"][0][0], "MENTE price-oracle pool — base leg", "Liquidity"),
    _reg(MARKET_POOLS["MOCA"][0][0], "MOCA/MENTE pool — LP'd by treasury; price oracle", "Liquidity"),
    _reg("0x1c5ebb794335b72d773df2fd8f80f3d1afbb75dd", "Gas funder — sends ETH slivers so cognition spends are gasless for users", "Infrastructure"),
    _reg("0x7b85e278a7446d8349b066e835d3057d895aecff", "Registration-era gas funder (historic)", "Infrastructure"),
    _reg("0x8004a169fb4a3325136eb29fa0ceb6d2e539a432", "AgentIdentity registry — ERC-8004 era (historic, economically inert)", "Infrastructure"),
    _reg("0x4d3021a52b31ffafde3c46450d02c72807c3a178", "Minds team Fireblocks wallet — manual MOCA top-ups", "Funding sources"),
    _reg("0xf605dbb5626dfc1448cee33e2e1221103021468f", "Primary MENTE funder — OWNER UNCONFIRMED, identification open", "Funding sources"),
    _reg(SINK, "Collector sweep destination — receives a daily MENTE sweep from the collector since 2026-06-19; has never sent anything out", "Collector"),
    _reg("0x63c0c19a282a1B52b07dD5a65b58948A07DAE32B", "EIP-7702 delegator implementation the treasury EOA delegates to", "Infrastructure"),
    _reg("0x45d0cEAd7c0a2E1a0528C4131A2d95DE9a394839", "Early MENTE funder (Apr 2026) — unidentified; also spent 100k MENTE into the collector", "Funding sources"),
    _reg("0xbDCb95A80d4C770fa811B1FAF0bb4Cf204d310b5", "Early MENTE funder (Apr–May 2026) — unidentified", "Funding sources"),
    _reg("0x0a2854Fbbd9B3Ef66F17d47284E7f899b9509330", "Swap counterparty — took 72k MENTE, returned 112k MOCA; venue unconfirmed", "Liquidity"),
    _reg("0xd8506866faadfdcfb9600479ba7dc652a203f111", "ADDRESS-POISONING MIMIC of the collector — never copy the collector from a transaction history", "Warnings", True),
    _reg("0x9a95a47a4f90c9c14ae8e3a9c37e822ed0e5a07f", "ADDRESS-POISONING MIMIC of The Gamemaster mind — zero-value poison transfers, SWARM era", "Warnings", True),
]
_have = {r["addr"].lower() for r in registry}
for _c in top_recip[:10]:                      # material outflow counterparties
    if _c["addr"].lower() not in _have and not _c["label"]:
        registry.append(_reg(_c["addr"], f"Top recipient — ${_c['usd']:,.0f} over {_c['n']} transfers · unlabeled", "Recipients"))
        _have.add(_c["addr"].lower())
for _c in in_sources[:10]:                     # material funding counterparties
    if _c["addr"].lower() not in _have and not _c["label"]:
        registry.append(_reg(_c["addr"], f"Inflow source — ${_c['usd']:,.0f} over {_c['n']} transfers · unlabeled", "Funding sources"))
        _have.add(_c["addr"].lower())
for _a, _l in KNOWN.items():                   # anything labeled but not yet surfaced
    if _a not in _have and "(mind)" not in _l:
        registry.append(_reg(_a, _l, "Other labeled"))
        _have.add(_a)

# Truncation-collision detection. The page shortens addresses two ways: tables
# use 0x + 6 hex …4 (0xd85096…f111), the flow diagram and prose use 0x + 4 hex …4
# (0xd850…f111). The collector and its mimic are distinguishable in the first
# form but IDENTICAL in the second — which is the form a reader is most likely
# to copy. Flag against the shortest form actually rendered.
_all = [r["addr"] for r in registry] + [c["addr"] for c in top_recip] + [c["addr"] for c in in_sources]
_short, _long = defaultdict(set), defaultdict(set)
for _a in _all:
    _short[_a[:6].lower() + _a[-4:].lower()].add(_a.lower())
    _long[_a[:8].lower() + _a[-4:].lower()].add(_a.lower())
for _r in registry:
    _a = _r["addr"]
    if len(_long[_a[:8].lower() + _a[-4:].lower()]) > 1:
        _r["collision"] = "table"      # ambiguous even in the 0xXXXXXX…XXXX table form
    elif len(_short[_a[:6].lower() + _a[-4:].lower()]) > 1:
        _r["collision"] = "short"      # ambiguous in the 0xXXXX…XXXX diagram/prose form

data = {"scope": scope, "facts": facts, "infer": infer, "server": server, "stripe_snap": stripe_snap,
        "insights": insights, "open_items": open_items, "gaps": gaps, "registry": registry, "sink": sink}

json.dump(STATE, open(RATES_PATH, "w"), indent=0)

# --- per-tx export with rate provenance ---
with open(os.path.join(HERE, "transfers_export.csv"), "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["timestamp_utc", "direction", "token", "amount", "rate_usd", "rate_source", "usd", "size_band", "counterparty", "counterparty_label", "tx_hash"])
    for r in rows:
        w.writerow([r["ts"], "OUT", r["tok"], f"{r['val']:.6f}", f"{r['rate']:.8f}", r["rsrc"], f"{r['usd']:.4f}",
                    BAND_LABEL[band(r["usd"])], r["to"], KNOWN.get(r["to"].lower(), ""), r["tx"]])
    for f in inflows:
        w.writerow([f["ts"], "IN", f["tok"], f"{f['val']:.6f}", f"{f['rate']:.8f}", f["rsrc"], f"{f['usd']:.4f}",
                    "", f["from"], KNOWN.get(f["from"].lower(), ""), f["tx"]])

# --- snapshot history (append-only; git history is the immutable trail) ---
hist_path = os.path.join(HERE, "stats_history.json")
hist = json.load(open(hist_path)) if os.path.exists(hist_path) else []
hist.append({"ts": now.strftime("%Y-%m-%dT%H:%M"),
             "invoke": S["tot"]["invoke"]["n"], "equip": S["tot"]["equip"]["n"],
             "growth": S["tot"]["growth"]["n"],
             "moca": round(sum(r["val"] for r in rows if r["tok"] == "MOCA"), 1),
             "creators": S["creators_n"], "rate": RATE["MOCA"],
             "balance": round(BALANCE["MOCA"], 1) if BALANCE["MOCA"] is not None else None,
             "runway7": runway7, "runway_adj": runway7})
json.dump(hist, open(hist_path, "w"))


# ==================== LEGACY VIEW (continuity for execs) ====================
# Renders legacy.html with the original MOCA-only layout + method (live-rate
# USD, old category folding, heuristic organic share). Kept while stakeholders
# transition; the banner on the page states it is superseded by index.html.
LRATE = RATE["MOCA"]
def _lcat(r):
    if r["cat"] == "nonstandard":
        return "invoke" if r["usd"] < 0.5 else "growth"
    return r["cat"]
mrows = [dict(r, lcat=_lcat(r)) for r in rows if r["tok"] == "MOCA"]
minf = [f for f in inflows if f["tok"] == "MOCA"]
lcats = ["invoke", "equip", "growth", "micro"]
if mrows:
    LS = {"rate": LRATE, "generated": now.strftime("%Y-%m-%d %H:%M"),
          "first_invoke": "2026-07-11 17:17:59", "first_moca": "2026-07-11 15:41:07",
          "last_tx": mrows[0]["ts"],
          "tot": {c: {"n": sum(1 for r in mrows if r["lcat"] == c),
                      "moca": round(sum(r["val"] for r in mrows if r["lcat"] == c), 1)} for c in lcats},
          "inv24": sum(1 for r in mrows if r["lcat"] == "invoke" and r["ts"] > cut24),
          "eq24": sum(1 for r in mrows if r["lcat"] == "equip" and r["ts"] > cut24)}
    lbyh, lmh = defaultdict(Counter), defaultdict(float)
    for r in mrows:
        lbyh[r["ts"][:13]][r["lcat"]] += 1
        lmh[r["ts"][:13]] += r["val"]
    lh0 = datetime.fromisoformat(mrows[-1]["ts"]).replace(minute=0, second=0, tzinfo=timezone.utc)
    lhourly, lh = [], lh0
    while lh <= now:
        k = lh.strftime("%Y-%m-%dT%H")
        c = lbyh[k]
        lhourly.append({"h": k, "invoke": c["invoke"], "equip": c["equip"], "growth": c["growth"],
                        "micro": c["micro"], "moca": round(lmh[k], 1)})
        lh += timedelta(hours=1)
    ldays = sorted({r["ts"][:10] for r in mrows})
    ldaily = []
    for d in ldays + ([] if today in ldays else [today]):
        rs = [r for r in mrows if r["ts"][:10] == d]
        c = Counter(r["lcat"] for r in rs)
        ldaily.append({"d": d, "invoke": c["invoke"], "equip": c["equip"], "growth": c["growth"], "micro": c["micro"],
                       "moca_ce": round(sum(r["val"] for r in rs if r["lcat"] in ("invoke", "equip")), 1),
                       "moca_other": round(sum(r["val"] for r in rs if r["lcat"] in ("growth", "micro")), 1)})
    lcr = defaultdict(lambda: {"invoke": 0, "equip": 0, "moca": 0.0})
    for r in mrows:
        if r["lcat"] in ("invoke", "equip"):
            lcr[r["to"]][r["lcat"]] += 1
            lcr[r["to"]]["moca"] += r["val"]
    lcreators = sorted(({"addr": a, "invoke": d["invoke"], "equip": d["equip"], "moca": round(d["moca"], 1)}
                        for a, d in lcr.items()), key=lambda x: -x["moca"])
    LS["creators_n"] = len(lcreators)
    lother = [{"ts": r["ts"], "val": round(r["val"], 2), "to": r["to"], "tx": r["tx"],
               "cat": r["fine"] if r["cat"] != "nonstandard" else "nonstandard"}
              for r in mrows if r["lcat"] in ("growth", "micro")]
    lgrows = [dict(g, moca=round(g["usd"] / LRATE, 1)) for g in grows]
    lce = sum(g["moca"] for g in lgrows) or sum(c["moca"] for c in lcreators) or 1
    lce_all = sum(c["moca"] for c in lcreators) or 1
    lat_risk = sum(g["moca"] for g in lgrows if g["status"] == "review")
    lburn24 = sum(r["val"] for r in mrows if r["ts"] > cut24 and (r["lcat"] in ("invoke", "equip")
                  or r["fine"] in ("$3 credit", "referral $5"))) * LRATE
    lburn_prev = sum(r["val"] for r in mrows if cut48 < r["ts"] <= cut24 and (r["lcat"] in ("invoke", "equip")
                     or r["fine"] in ("$3 credit", "referral $5"))) * LRATE
    lbal = BALANCE["MOCA"]
    lgf = min(lburn24 / lburn_prev, 2) if lburn_prev > 0 else 1
    lrun = round(lbal * LRATE / lburn24, 1) if lbal and lburn24 > 0 else None
    lrun_adj = round(lbal * LRATE / (lburn24 * lgf), 1) if lbal and lburn24 > 0 else None
    UNIT_L = {"invoke": 0.10, "equip": 1, "$3 credit": 3, "referral $5": 5,
              "stripe $10": 10, "stripe $25": 25, "stripe $50": 50}
    lprom = sum(UNIT_L.get(r["fine"], r["val"] * LRATE) for r in mrows)
    lsett = sum(r["val"] for r in mrows) * LRATE
    lout24 = sum(r["val"] for r in mrows if r["ts"] > cut24)
    lin24 = sum(f["val"] for f in minf if f["ts"] > cut24)
    lguard = {"organic_share": round((lce_all - lat_risk) / lce_all * 100, 1),
              "at_risk_usd": round(lat_risk * LRATE, 2),
              "bal_delta24": round(lin24 - lout24, 0),
              "topup24": round(sum(f["val"] for f in minf if f["val"] >= 100 and f["ts"] > cut24), 0),
              "recon_drift": None,
              "topup_needed": round(max(0, 7 * lburn24 - (lbal or 0) * LRATE) / LRATE, 0) if lbal and lburn24 else None,
              "runway_days": lrun, "runway_adj": lrun_adj,
              "balance": round(lbal, 0) if lbal else None,
              "burn24": round(lburn24, 2), "burn_prev": round(lburn_prev, 2),
              "promised_usd": round(lprom, 2),
              "fx_drift_pct": round((lsett - lprom) / lprom * 100, 1) if lprom else 0,
              "rows": lgrows}
    def _lbucket(r):
        f = r["fine"]
        if f in ("stripe $10", "stripe $25", "stripe $50"): return "stripe"
        return {"invoke": "c010", "equip": "c1", "$3 credit": "c3", "referral $5": "c5"}.get(f, "other")
    lo_d, lu_d, li_d = defaultdict(float), defaultdict(float), defaultdict(float)
    lb_d = defaultdict(lambda: defaultdict(float))
    lw_d = defaultdict(lambda: defaultdict(set))
    for r in mrows:
        d = r["ts"][:10]
        lo_d[d] += r["val"]; lu_d[d] += r["usd"]
        lb_d[d][_lbucket(r)] += r["usd"]
        lw_d[d][_lbucket(r)].add(r["to"])
    for f in minf:
        li_d[f["ts"][:10]] += f["val"]
    lt_days = sorted(set(lo_d) | set(li_d))
    lt_daily = [{"d": d, "out": round(lo_d[d], 1), "out_usd": round(lu_d[d], 2),
                 "inn": round(li_d[d], 1), "net": round(li_d[d] - lo_d[d], 1),
                 "b": {k: round(v, 2) for k, v in lb_d[d].items()},
                 "w": {k: len(v) for k, v in lw_d[d].items()}} for d in lt_days]
    lfull = [d for d in lt_days if d < today]
    ltreas = {"balance": round(lbal, 0) if lbal else None,
              "balance_usd": round(lbal * LRATE, 0) if lbal else None,
              "out24_moca": round(lout24, 0), "out24_usd": round(lout24 * LRATE, 2),
              "in24_moca": round(lin24, 0),
              "burn7_usd": round(sum(lu_d[d] for d in lfull[-7:]) / max(len(lfull[-7:]), 1), 2),
              "daily": lt_daily}
    ldata = {"S": LS, "hourly": lhourly, "daily": ldaily, "creators": lcreators,
             "other": lother, "guard": lguard, "treasury": ltreas}
    ltpl = open(os.path.join(HERE, "template_legacy.html")).read()
    ltpl = ltpl.replace("MOCA rate used $0.008912", f"MOCA rate used ${LRATE:.6f}")
    open(os.path.join(HERE, "legacy.html"), "w").write(
        "<!doctype html>\n<html lang=\"en\">\n" + ltpl.replace("/*__DATA__*/", json.dumps(ldata)) + "\n</html>")
    print("wrote legacy.html |", len(mrows), "MOCA rows")

tpl = open(os.path.join(HERE, "template.html")).read()
out = os.path.join(HERE, "index.html")
open(out, "w").write("<!doctype html>\n<html lang=\"en\">\n" + tpl.replace("/*__DATA__*/", json.dumps(data)) + "\n</html>")
print("wrote", out, "| rows:", len(rows), "| range:", facts["range"], "| recon:", recon)

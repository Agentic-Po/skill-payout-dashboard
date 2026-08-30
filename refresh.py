#!/usr/bin/env python3
"""Refresh the Minds treasury wallet dashboard.

Fetches token transfers (MENTE + MOCA) from Blockscout (Base) for the tracked
wallet, merges them into the transfers/ and transfers_in/ monthly shard
caches (slimmed rows — see shards.py), recomputes the
two-layer dataset (Layer 1: on-chain facts; Layer 2: AI-inferred interpretation),
and renders index.html from template.html. Writes transfers_export.csv (per-tx,
with rate provenance) so every displayed total ties back to transaction hashes.

Historical day rates are persisted in day_rates.json and never recomputed, so
closed days cannot reprice on later runs. Git history of the hourly commits is
the append-only audit trail of every published figure.
"""
import csv, json, math, os, statistics, time, urllib.request
import posthog_source
import shards
# taxonomy lives in classify.py — the ONE classifier shared with notify/alerts
from classify import band, classify_usd, BAND_LABEL, BAND_KEYS, STRIPE_FINE
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
# PUBLIC structural labels ONLY (redaction, 2026-08-30). Identity labels —
# owners, teams, custodian products, mind names — live in
# moca-ledger-private:labels/ and, for local builds, in the gitignored
# private_labels.json; they feed ONLY private artifacts (guard_private.json).
# The public build must not need identities: absent file = placeholders.
# The four flow-chart wallets (treasury, collector, rebate, gas funder) are
# deliberately public by owner decision and keep their names.
KNOWN = {"0x9a95d76c41aa34093a0db5f26f97309fe734a07f": "creator wallet",
         "0xd85096faec1ac03075667b4c1a1661f5623bf111": "Cognition Credits collector — also the original SWARM-era treasury+collector hub (pre-Apr 2026)",
         "0xea87169699dabd028a78d4b91544b4298086baf6": "SWARM token contract (original Cognition Credit token, migrated to MENTE ~Apr 2026)",
         "0x8004a169fb4a3325136eb29fa0ceb6d2e539a432": "AgentIdentity registry (historic, ERC-8004 era)",
         "0x7b85e278a7446d8349b066e835d3057d895aecff": "registration-era gas funder (historic)",
         "0xd8506866faadfdcfb9600479ba7dc652a203f111": "known mimic — do not copy",
         "0x1c5ebb794335b72d773df2fd8f80f3d1afbb75dd": "gas funder (sends ETH to mind wallets for cognition spends)"}
# Private identity labels ({addr_lower: {"label":..., "note":...}}).
_plbl_path = os.path.join(HERE, "private_labels.json")
PRIVATE_LABELS = json.load(open(_plbl_path)) if os.path.exists(_plbl_path) else {}
if PRIVATE_LABELS:
    print(f"private_labels.json: {len(PRIVATE_LABELS)} private labels loaded (private surfaces only)")
# Optional wallet↔mind map (drop wallet_mind_map.csv beside this script —
# gitignored, from the platform's wallet-mind-map export). Public surfaces get
# the structural "creator wallet" tag only; the mind NAME is an identity and
# goes to PRIVATE_LABELS for the private guard file.
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
                    KNOWN[_w] = "creator wallet"
                    PRIVATE_LABELS.setdefault(_w, {}).setdefault("label", _nm + " (mind)")
                    n_loaded += 1
        print(f"wallet_mind_map.csv: {n_loaded} mind wallets tagged (names stay private)")

def private_label(addr):
    """Identity label for PRIVATE artifacts only — never a public field."""
    return (PRIVATE_LABELS.get(addr.lower()) or {}).get("label") or KNOWN.get(addr.lower())

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

# --- raw-RPC fallback (Blockscout v2 outage resilience, added 2026-08-20 after
# a >14h platform-wide 500 on every /addresses/* endpoint broke hourly runs).
# eth_getLogs returns the REAL log index, so cache keys stay identical to the
# Blockscout rows and dedup/merge is safe across sources.
# Blockscout's eth-rpc endpoint rate-limits the shared Actions IP after the
# page crawl, so public Base RPCs come first and Blockscout stays last.
RPC_ENDPOINTS = ["https://mainnet.base.org", "https://base.drpc.org",
                 "https://base.blockscout.com/api/eth-rpc"]
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

def rpc(method, params, tries=3):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last_err = None
    for url in RPC_ENDPOINTS:
        for a in range(tries):
            try:
                req = urllib.request.Request(url, data=payload,
                    headers={"Content-Type": "application/json", "User-Agent": "curl/8.4.0"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    res = json.load(r)
                if res.get("error"):
                    last_err = Exception(f"{url}: {res['error']}")
                    break  # rpc-level error (e.g. range too large) — next endpoint
                if res.get("result") is not None:
                    return res["result"]
                last_err = Exception(f"{url}: empty result")
                break
            except Exception as e:
                last_err = e
                time.sleep(1)
    raise last_err

_block_ts_cache = {}
def block_ts(bn):
    if bn not in _block_ts_cache:
        b = rpc("eth_getBlockByNumber", [hex(bn), False])
        _block_ts_cache[bn] = datetime.fromtimestamp(
            int(b["timestamp"], 16), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    return _block_ts_cache[bn]

def rpc_transfer_fallback(wallet, direction, token_addrs, from_block):
    """Fetch Transfer logs via eth_getLogs and shape them like Blockscout v2
    items (only the fields shards.slim keeps). Addresses come back lowercase —
    canonicalized against cached casing downstream."""
    topic_w = "0x" + "0" * 24 + wallet[2:].lower()
    topics = ([TRANSFER_TOPIC, topic_w] if direction == "from"
              else [TRANSFER_TOPIC, None, topic_w])
    latest = int(rpc("eth_blockNumber", []), 16)
    logs, start, chunk = [], from_block, 9000
    while start <= latest:
        end = min(start + chunk - 1, latest)
        try:
            logs += rpc("eth_getLogs", [{"fromBlock": hex(start), "toBlock": hex(end),
                                         "address": token_addrs, "topics": topics}])
        except Exception:
            if chunk <= 500:
                raise
            chunk //= 2  # provider range/result cap — retry this span smaller
            continue
        start = end + 1
        time.sleep(0.2)
    items = []
    for lg in logs:
        if lg.get("removed") or len(lg.get("topics", [])) < 3:
            continue
        bn = int(lg["blockNumber"], 16)
        items.append({"timestamp": block_ts(bn),
                      "transaction_hash": lg["transactionHash"],
                      "log_index": int(lg["logIndex"], 16),
                      "block_number": bn,
                      "from": {"hash": "0x" + lg["topics"][1][-40:]},
                      "to": {"hash": "0x" + lg["topics"][2][-40:]},
                      "token": {"address_hash": lg["address"].lower()},
                      "total": {"value": str(int(lg["data"], 16)), "decimals": None}})
    items.sort(key=lambda i: i["timestamp"], reverse=True)
    return items

# --- incremental fetch: newest pages until we overlap the cache. If the page
# cap is hit before overlap, the cache is NOT updated (a silent gap would
# become permanent and invisible) — the run renders from the last good cache.
# Caches are monthly shard dirs of slimmed rows (see shards.py) so no file
# can ever approach GitHub's 100 MB limit again.
def refresh_cache(base_url, dir_path, pages=100):
    shards.migrate_legacy(dir_path)
    old = shards.load(dir_path)
    seen = {key(i) for i in old}
    newest = old[0]["timestamp"] if old else "2026-04-01"
    items, params, overlapped = [], "", False
    try:
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
    except Exception as e:
        # Blockscout v2 down — refetch the gap straight from the chain. Real
        # log indexes keep keys compatible, so dedup against the cache is safe.
        print(f"v2 crawl failed for {dir_path} ({e}) — trying eth_getLogs fallback")
        newest_blk = max([i.get("block_number", 0) for i in old] + [0])
        if not newest_blk:
            print(f"WARNING: no cached block to resume from for {dir_path} — keeping previous cache")
            return old, 0, False
        try:
            direction = "to" if "filter=to" in base_url else "from"
            items = rpc_transfer_fallback(WALLET, direction,
                                          [t["addr"] for t in TOKENS.values()], newest_blk)
            overlapped = True
            print(f"fallback fetched {len(items)} logs for {dir_path} from block {newest_blk}")
        except Exception as e2:
            print(f"WARNING: fallback also failed for {dir_path} ({e2}) — keeping previous cache")
            return old, 0, False
    if not overlapped:
        print(f"WARNING: page cap hit before overlap for {dir_path} — keeping previous cache")
        return old, 0, False
    # Belt-and-braces cross-check: right after an outage Blockscout can serve
    # from a still-backfilling index (observed 2026-08-20: one 6,665-MOCA
    # transfer absent from v2's pages during recovery — the overlap check
    # banked the hole permanently). Re-verify the trailing ~24h against raw
    # chain logs every run and merge anything the crawl didn't return.
    try:
        got = {key(i) for i in items}
        newest_blk = max([i.get("block_number", 0) for i in old] + [0])
        if newest_blk:
            direction = "to" if "filter=to" in base_url else "from"
            xcheck = rpc_transfer_fallback(WALLET, direction,
                                           [t["addr"] for t in TOKENS.values()],
                                           max(newest_blk - 43200, 1))
            extra = [i for i in xcheck if key(i) not in got and key(i) not in seen]
            if extra:
                print(f"cross-check recovered {len(extra)} transfer(s) missing from the crawl for {dir_path}")
            items += extra
    except Exception as e:
        print(f"log cross-check skipped for {dir_path}: {e}")
    add, _k = [], set()
    for i in items:
        k = key(i)
        if k not in seen and k not in _k:
            _k.add(k)
            add.append(shards.slim(i))
    full = sorted(add + old, key=lambda i: i["timestamp"], reverse=True)
    if add:
        shards.save(dir_path, full, months={shards.month_of(r) for r in add})
    return full, len(add), True

full, n_new, ok_out = refresh_cache(BASE, os.path.join(HERE, "transfers"))
full_in, n_new_in, ok_in = refresh_cache(BASE.replace("filter=from", "filter=to"), os.path.join(HERE, "transfers_in"), pages=60)
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
    last_err = None
    for url in RPC_ENDPOINTS:
        try:
            req = urllib.request.Request(url, data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "curl/8.4.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                res = json.load(r).get("result")
            if res:
                return int(res, 16) / 10 ** DECIMALS[sym]
            last_err = Exception(f"{url}: empty result (rate-limited?)")
        except Exception as e:
            last_err = e
        time.sleep(1)
    raise last_err

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

# Canonical address casing: fallback rows carry lowercase addresses while
# Blockscout rows are EIP-55 checksummed — aggregation keys on the exact
# string, so map every address to the first mixed-case form seen in cache.
ADDR_CASE = {}
for _i in full + full_in:
    for _h in (_i["from"]["hash"], _i["to"]["hash"]):
        if _h != _h.lower():
            ADDR_CASE.setdefault(_h.lower(), _h)
def canon(addr):
    return ADDR_CASE.get(addr.lower(), addr)

def norm(i, extra_from=False):
    sym = ADDR2SYM.get(i["token"]["address_hash"].lower())
    if not sym:
        return None
    dec = int(i["total"].get("decimals") or DECIMALS[sym])
    r = {"ts": i["timestamp"][:19], "tok": sym, "val": int(i["total"]["value"]) / 10 ** dec,
         "to": canon(i["to"]["hash"]), "tx": i["transaction_hash"], "blk": i.get("block_number", 0),
         "li": i.get("log_index", "")}
    if extra_from:
        r["from"] = canon(i["from"]["hash"])
    return r

rows = sorted(filter(None, (norm(i) for i in full)), key=lambda r: r["ts"], reverse=True)
inflows = sorted(filter(None, (norm(i, True) for i in full_in)), key=lambda r: r["ts"], reverse=True)
excluded_in = len(full_in) - len(inflows)

# --- market daily closes (era-aware pool OHLCV via GeckoTerminal) ---
# Fetched BEFORE the day-rate oracle because the oracle now uses these closes as
# its second leg: a closed day with no $0.10 invoke cluster is priced by that
# day's market close instead of carrying yesterday's rate forward forever. The
# deviation cross-check that consumes these lives after the oracle (it must only
# score days the oracle priced independently, i.e. day-implied ones).
MARKET_POOLS = {
    "MENTE": [("0xd76d44875716a708dbd55cd8ffc3eb1f94acbce3", "base"),
              ("0x2a5eeea4d91042f779ee6014f4f6fd41f375262d", "quote")],
    "MOCA":  [("0x2a5eeea4d91042f779ee6014f4f6fd41f375262d", "base")],
}
STATE.setdefault("market_rates", {})
try:
    from datetime import datetime as _dt
    for sym, pools in MARKET_POOLS.items():
        mr = STATE["market_rates"].setdefault(sym, {})
        # 1 day, not 3: the oracle's market leg can only fill YESTERDAY if
        # yesterday's close is already banked, so the refetch trigger has to be
        # tighter than the gap it is meant to close.
        need_recent = (_dt.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
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
except Exception as e:
    print("market rate fetch failed:", e)

# --- day-anchored rate oracle ---
# Persisted day rates are immutable. New (unseen) days are computed walking
# BACKWARD from the most recent day — the live rate is a good anchor at the
# recent end, and each day's $0.10 cluster is searched in raw-token space
# using the nearest already-known later day's implied rate, so history can't
# be mispriced by today's quote and closed days never reprice.
#
# SECOND LEG (added 2026-08-30): invokes stopped on 2026-08-21, so from 08-22 on
# no day had a $0.10 cluster and every later day was priced by carrying 08-21
# forward while MOCA kept moving. A closed day the implied oracle cannot price
# now falls back to that day's MARKET close (STATE["market_rates"], fetched
# above). Provenance is recorded per day in STATE["day_rate_src"] so an auditor
# can tell the two apart; every day already persisted before this change is
# implied by construction and is stamped as such, never rewritten. "Closed days
# never reprice" is unchanged: only days ABSENT from day_rates may be filled.
#
# BAND (revised after QA, 2026-08-30): a market close is no longer banded
# against `ref`, the last rate the backward walk happened to accept — that
# anchor can be 42 days newer than the day being priced (MENTE: 08-25 then
# 07-14), where a flat 5x band admits essentially any print. It is now banded
# against the CALENDAR-NEAREST known day rate, with a tolerance that scales
# with the gap: a 10 %/day compounded drift budget (1.10 ** gap_days), floored
# at one day and capped at the old 5x so the band never gets looser than it
# used to be. 10 %/day is deliberately generous for a liquid pair — it is an
# outlier/broken-print detector, not a volatility model — and it reaches the 5x
# cap at ~17 days. Beyond MARKET_MAX_GAP_DAYS there is no defensible anchor at
# all, so the day is refused outright and stamped "market-unbanded".
# Refusals are RECORDED in day_rate_src ("market-rejected" / "market-unbanded")
# so an auditor can tell a thrown-away close from a day that never had one —
# these stamps sit on days ABSENT from day_rates, and day_rate() only reads src
# for days present in it, so pricing is unaffected.
MARKET_DRIFT_PER_DAY = 0.10      # compounded per calendar day of gap
MARKET_BAND_CAP = 5.0            # never looser than the original flat 5x band
MARKET_MAX_GAP_DAYS = 45         # beyond this, refuse to band at all

def _market_band(day, known):
    """(anchor_day, anchor_rate, gap_days, factor) for the nearest known rate."""
    if not known:
        return None
    dd = datetime.strptime(day, "%Y-%m-%d")
    a = min(known, key=lambda k: (abs((datetime.strptime(k, "%Y-%m-%d") - dd).days), k))
    gap = abs((datetime.strptime(a, "%Y-%m-%d") - dd).days)
    factor = min(MARKET_BAND_CAP, (1 + MARKET_DRIFT_PER_DAY) ** max(gap, 1))
    return a, known[a], gap, factor

MARKET_REFUSED = []              # (sym, day, why, close, anchor_day, anchor_rate, gap)
STATE.setdefault("day_rate_src", {})
for sym in TOKENS:
    persisted = STATE["day_rates"][sym]
    src = STATE["day_rate_src"].setdefault(sym, {})
    for d in persisted:
        src.setdefault(d, "implied")
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
                src[d] = "implied"
        elif d != today_utc:
            # Market leg. Banded against the calendar-nearest KNOWN day rate
            # with a gap-scaled drift budget (see MARKET_* above). A close
            # outside the band is a broken pool print, not a price move, and is
            # refused; the day then falls through to carry-forward as before,
            # but the refusal is stamped so it is not silent.
            m = (STATE["market_rates"].get(sym) or {}).get(d)
            if not m:
                continue
            band = _market_band(d, persisted)
            if band is None:
                MARKET_REFUSED.append((sym, d, "market-unbanded", m, None, None, None))
                src[d] = "market-unbanded"
                continue
            adj, arate, gap, factor = band
            if gap > MARKET_MAX_GAP_DAYS:
                MARKET_REFUSED.append((sym, d, "market-unbanded", m, adj, arate, gap))
                src[d] = "market-unbanded"
            elif arate / factor < m < arate * factor:
                persisted[d] = round(m, 10)
                src[d] = "market"
                ref = m
            else:
                MARKET_REFUSED.append((sym, d, "market-rejected", m, adj, arate, gap))
                src[d] = "market-rejected"

# One greppable line per refusal plus a count — a rejection cascade must not
# scroll past unnoticed (it silently reverts days to carry-forward pricing,
# which is exactly what the market leg exists to eliminate).
for sym, d, why, m, adj, arate, gap in MARKET_REFUSED:
    print(f"DATA-QUALITY: {sym} {d} {why}: close={m} anchor={adj} rate={arate} gap={gap}d")
if MARKET_REFUSED:
    print(f"DATA-QUALITY: {len(MARKET_REFUSED)} market close(s) refused "
          f"({sum(1 for x in MARKET_REFUSED if x[2] == 'market-rejected')} out-of-band, "
          f"{sum(1 for x in MARKET_REFUSED if x[2] == 'market-unbanded')} unbandable) — "
          f"those days fall back to carry-forward pricing")
STATE["market_refused"] = [{"sym": s, "day": d, "why": w, "close": m,
                            "anchor_day": adj, "anchor_rate": arate, "gap_days": g}
                           for s, d, w, m, adj, arate, g in MARKET_REFUSED]

# day_rates and day_rate_src must not drift apart: every priced day carries a
# provenance stamp. (src also holds refusal stamps for days NOT in day_rates,
# so the invariant is one-directional.)
for _s in TOKENS:
    _missing = set(STATE["day_rates"][_s]) - set(STATE["day_rate_src"].get(_s, {}))
    assert not _missing, f"{_s}: day_rates days without a day_rate_src stamp: {sorted(_missing)[:5]}"

# ---- pricing provenance (public summary) ----
# Surfacing only: per-token COUNTS by source value of STATE["day_rate_src"].
# The per-day map itself is not embedded in data.json — the full ledger is
# persisted in day_rates.json, which is published. The restatement figure is a
# one-time historical note (git d75e9bd, "+$2,587 / +4.98%"), hard-stamped with
# its date, never recomputed. Counts are DYNAMIC: the market-filled count grows
# with every closed day after the 2026-08-21 cutoff, so nothing here is pinned.
PRICING_RESTATEMENT_USD = 2587
PRICING_RESTATEMENT_DATE = "2026-08-30"
pricing_provenance = {"by_token": {}, "implied": 0, "market": 0, "refused": 0,
                      "restatement_usd": PRICING_RESTATEMENT_USD,
                      "restatement_date": PRICING_RESTATEMENT_DATE}
for _s in TOKENS:
    _counts = dict(Counter((STATE["day_rate_src"].get(_s) or {}).values()))
    pricing_provenance["by_token"][_s] = _counts
    pricing_provenance["implied"] += _counts.get("implied", 0)
    pricing_provenance["market"] += _counts.get("market", 0)
    pricing_provenance["refused"] += (_counts.get("market-rejected", 0)
                                      + _counts.get("market-unbanded", 0))
# Build-time assertion: the published summary must equal a direct len-by-value
# recount of day_rate_src — a summary that drifts from the stamps it claims to
# summarise is worse than no summary.
for _s in TOKENS:
    _direct = Counter((STATE["day_rate_src"].get(_s) or {}).values())
    assert pricing_provenance["by_token"][_s] == dict(_direct), \
        f"{_s}: pricing_provenance drifted from day_rate_src"
    assert sum(pricing_provenance["by_token"][_s].values()) == len(STATE["day_rate_src"].get(_s) or {}), \
        f"{_s}: pricing_provenance count != number of stamped days"
assert (pricing_provenance["implied"] + pricing_provenance["market"]
        + pricing_provenance["refused"]) == sum(
            sum(c.values()) for c in pricing_provenance["by_token"].values()), \
    "pricing_provenance totals do not cover every day_rate_src value"

def day_rate(sym, ts):
    """Return (rate, source) for a timestamp."""
    d, dr = ts[:10], STATE["day_rates"][sym]
    if d in dr:
        return dr[d], ("day-market" if (STATE.get("day_rate_src", {}).get(sym) or {}).get(d) == "market"
                       else "day-implied")
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
    # classify HERE (not in Layer 2) so facts_window's economy/ops split can
    # see r["cat"] — adversary-caught ordering bug in council loop 3
    r["cat"], r["fine"], _ = classify_usd(r["usd"])
for f in inflows:
    f["rate"], f["rsrc"] = day_rate(f["tok"], f["ts"])
    f["usd"] = f["val"] * f["rate"]

# --- market-price cross-check ---
# Validates the day-IMPLIED payout oracle against external market data. MENTE:
# USDC/MENTE Uniswap pool while it carried the volume, then the MOCA/MENTE
# Aerodrome pool (quote side). MOCA: MOCA/USDC Aerodrome pool. The closes
# themselves were fetched above (the oracle's market leg needs them first).
# Only day-implied days are scored: a market-filled day IS the market close, so
# scoring it would report a fake 0% deviation and inflate the agreement stats.
market_summary = {}
try:
    for sym, pools in MARKET_POOLS.items():
        mr = STATE["market_rates"].get(sym) or {}
        src = STATE["day_rate_src"].get(sym) or {}
        devs = []
        for day, r in STATE["day_rates"][sym].items():
            m = mr.get(day)
            if m and src.get(day, "implied") == "implied":
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
COG_DIR = os.path.join(HERE, "cognition_in")
shards.migrate_legacy(COG_DIR, ts_key="ts", do_slim=False)
cognition = None
if os.path.isdir(COG_DIR):
    cog = shards.load(COG_DIR, ts_key="ts")
    # incremental top-up: newest pages until overlap (same banking pattern)
    def _cog_fallback(seen_c, newest_c):
        # cog rows carry no block number — estimate the resume block from the
        # newest cached timestamp (Base ≈ 2s blocks) minus a 24h safety margin
        # (also re-verifies the trailing day against a possibly hole-y v2
        # index); dedup by tx:log_index absorbs the overlap.
        latest_blk = int(rpc("eth_blockNumber", []), 16)
        age_s = (datetime.now(timezone.utc)
                 - datetime.fromisoformat(newest_c).replace(tzinfo=timezone.utc)).total_seconds()
        from_blk = max(1, latest_blk - int(age_s / 2) - 43200)
        items = rpc_transfer_fallback(COLLECTOR, "to", [TOKENS["MENTE"]["addr"]], from_blk)
        return [{"ts": i["timestamp"][:19], "val": int(i["total"]["value"]) / 10 ** DECIMALS["MENTE"],
                 "from": canon(i["from"]["hash"]), "tx": i["transaction_hash"],
                 "log_index": i["log_index"], "transaction_hash": i["transaction_hash"]}
                for i in items
                if i["transaction_hash"] + ":" + str(i["log_index"]) not in seen_c]
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
        # cross-check the trailing day against raw chain logs (v2 recovery holes)
        try:
            _seen_now = seen_c | {c["transaction_hash"] + ":" + str(c["log_index"]) for c in got_c}
            _extra = _cog_fallback(_seen_now, newest_c) if cog else []
            if _extra:
                print(f"cognition cross-check recovered {len(_extra)} row(s) missing from v2")
                got_c += _extra
        except Exception as e:
            print("cognition log cross-check skipped:", e)
        if got_c:
            cog = sorted(got_c + cog, key=lambda i: i["ts"], reverse=True)
            shards.save(COG_DIR, cog, months={shards.month_of(c, "ts") for c in got_c}, ts_key="ts")
    except Exception as e:
        print("cognition v2 fetch failed:", e, "— trying eth_getLogs fallback")
        try:
            got_c = _cog_fallback(seen_c, newest_c) if cog else []
            if got_c:
                cog = sorted(got_c + cog, key=lambda i: i["ts"], reverse=True)
                shards.save(COG_DIR, cog, months={shards.month_of(c, "ts") for c in got_c}, ts_key="ts")
            print(f"cognition fallback added {len(got_c)} rows")
        except Exception as e2:
            print("cognition fallback failed (using cache):", e2)
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


RECYCLE_SRC = "0xd85096faec1ac03075667b4c1a1661f5623bf111"
def facts_window(rs, ins, label):
    out_usd = sum(r["usd"] for r in rs)
    in_usd = sum(f["usd"] for f in ins)
    in_recycled = sum(f["usd"] for f in ins if f["from"].lower() == RECYCLE_SRC)
    # economy = classified payouts; ops = the residual (swaps/treasury moves),
    # computed as out - economy so the two ALWAYS sum to the total exactly
    economy_out = sum(r["usd"] for r in rs if r["cat"] != "nonstandard")
    return {"label": label,
            "out_usd": round(out_usd, 2), "in_usd": round(in_usd, 2),
            "economy_out_usd": round(economy_out, 2),
            "ops_out_usd": round(out_usd - economy_out, 2),
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
    # Provenance marker: day_rate_src is per-TOKEN per-day while a daily row
    # aggregates tokens, so this is a per-token map, never one boolean — a
    # MOCA-market/MENTE-implied day must not render as "the day was market".
    # Omitted entirely when no token was market-filled that day.
    _mkt = {s: True for s in TOKENS
            if (STATE["day_rate_src"].get(s) or {}).get(d) == "market"}
    daily.append({"d": d, "partial": d == today,
                  **({"mkt": _mkt} if _mkt else {}),
                  "out_usd": round(out_usd, 2), "in_usd": round(in_usd, 2),
                  "net_usd": round(in_usd - out_usd, 2),
                  "out_tx": len(rs), "wallets": len({r["to"] for r in rs}),
                  "tok_raw": {s: round(sum(r["val"] for r in rs if r["tok"] == s), 1) for s in TOKENS},
                  "bands": {k: {"n": bc[k], "usd": round(bu[k], 2), "w": len(bw[k])} for k in bc}})

# --- closed-day digest ledger (Cycle-3 Loop 2, item 3) ---
# "Closed days never reprice" becomes mechanical: every closed UTC day's
# outflow aggregates are sealed under a sha256 in day_digests.json; a later
# run recomputing a different value HARD FAILS unless the day is documented
# in RESTATEMENTS.md. Fed the SAME priced rows the page renders from, so the
# ledger cannot diverge from what publishes.
import digests as _digests
_dig_records = _digests.records_for_closed_days(rows, today)
try:
    _digests.enforce(_dig_records,
                     now_iso=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
except _digests.DigestMismatch as e:
    raise SystemExit(f"FATAL: {e}")

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

# inflow sources (factual, structurally labeled)
# Funding-wallet IDENTITY labels are private (moca-ledger-private:labels/ +
# the gitignored private_labels.json). Publicly, every non-KNOWN inflow source
# gets a stable structural placeholder — "Funding wallet A/B/…" ordered by the
# wallet's FIRST-SEEN inflow (append-only history, so letters never reshuffle).
# Notes never publish: the old note field carried audit narratives.
_fw_order = {}
for _f in sorted(inflows, key=lambda f: (f["ts"], f["from"])):
    _fw_order.setdefault(_f["from"].lower(), len(_fw_order))
def _fw_name(i):
    s, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return "Funding wallet " + s
def in_label(addr):
    a = addr.lower()
    if a in KNOWN:
        return KNOWN[a], ""
    if a in _fw_order:
        return _fw_name(_fw_order[a]), ""
    return None, ""

src = defaultdict(lambda: {"n": 0, "usd": 0.0, "first": "9999", "last": "0"})
for f in inflows:
    a = src[f["from"]]
    a["n"] += 1; a["usd"] += f["usd"]
    a["first"] = min(a["first"], f["ts"]); a["last"] = max(a["last"], f["ts"])
in_sources = sorted(({"addr": k, "label": in_label(k)[0], "note": in_label(k)[1],
                      "n": v["n"], "usd": round(v["usd"], 2),
                      "first": v["first"][:10], "last": v["last"][:10]}
                     for k, v in src.items()), key=lambda x: -x["usd"])[:10]

# inflow ledger: one row per day × source wallet × token, newest first — the
# auditable record of exactly who funded the wallet, when, and with how much.
_led = defaultdict(lambda: {"n": 0, "val": 0.0, "usd": 0.0})
for f in inflows:
    e = _led[(f["ts"][:10], f["from"], f["tok"])]
    e["n"] += 1; e["val"] += f["val"]; e["usd"] += f["usd"]
in_ledger = [{"day": d, "addr": a, "tok": t, "n": v["n"], "val": round(v["val"], 4),
              "usd": round(v["usd"], 2), "label": in_label(a)[0], "note": in_label(a)[1]}
             for (d, a, t), v in sorted(_led.items(), reverse=True)]

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
    # Interim symmetric per-token fences (council 2026-08-30): the old
    # (>30 / <-150) pair was MENTE-era and asymmetric. MENTE is frozen, so any
    # movement is signal; MOCA's only legitimate drift is a missed-transfer
    # event (the Aug-19 outage showed ±1,094). Recalibrate from the recon
    # series now being persisted into stats_history once ~2 weeks accumulate.
    DRIFT_FENCE = {"MOCA": 100, "MENTE": 50}
    recon[sym] = {"net_cached": round(net, 1), "balance": round(BALANCE_RECON[sym], 1),
                  "delta": delta, "drift": drift, "degraded": sym in RECON_DEGRADED,
                  "warn": bool(drift is not None and abs(drift) > DRIFT_FENCE.get(sym, 50))}

# ================= DAILY BALANCE, RECONSTRUCTED =================
# Walk BACKWARD from the block-pinned on-chain balance, subtracting each day's
# net transfers. Ties to chain at the recent end by construction; the pre-cache
# holdings + the event-less MENTE burn sit as a constant offset at the oldest day
# (recon delta) — never spread across the series. Never forward-sum from zero.
balance_series = None
try:
    _bdays = sorted({r["ts"][:10] for r in rows if r["blk"] <= RECON_BLOCK}
                    | {f["ts"][:10] for f in inflows if f["blk"] <= RECON_BLOCK})
    _bdays = [d for d in _bdays if d >= "2026-04-25"]        # drop the partial genesis day
    _today = now.strftime("%Y-%m-%d")
    series = {}
    for sym in TOKENS:
        if BALANCE_RECON.get(sym) is None:
            continue
        _out = defaultdict(float); _inc = defaultdict(float)
        for r in rows:
            if r["tok"] == sym and r["blk"] <= RECON_BLOCK:
                _out[r["ts"][:10]] += r["val"]
        for f in inflows:
            if f["tok"] == sym and f["blk"] <= RECON_BLOCK:
                _inc[f["ts"][:10]] += f["val"]
        _net = {d: _inc[d] - _out[d] for d in _bdays}
        # backward walk: bal at end of the newest cached day == pinned balance
        _cached = [d for d in _bdays if d <= max(d2 for d2 in _bdays)]
        _bal = {}; _b = BALANCE_RECON[sym]
        for d in reversed(_bdays):
            _bal[d] = round(_b, 1)
            _b -= _net[d]
        _rec_src = RECYCLE_SRC.lower()
        pts = []
        for d in _bdays:
            _r, _rs = day_rate(sym, d + "T12:00:00")
            row = {"d": d, "qty": _bal[d], "usd": round(_bal[d] * _r, 0), "rsrc": _rs}
            if sym == "MENTE":
                row["out"] = round(_out[d], 1)
                row["in_rec"] = round(sum(f["val"] for f in inflows
                                          if f["tok"] == "MENTE" and f["blk"] <= RECON_BLOCK
                                          and f["ts"][:10] == d and f["from"].lower() == _rec_src), 1)
                row["in_ext"] = round(_inc[d] - row["in_rec"], 1)
                row["delta"] = round(_net[d], 1)
            pts.append(row)
        # today's live row (partial — uses live balance, not end-of-day)
        if BALANCE.get(sym) is not None:
            pts.append({"d": _today, "qty": round(BALANCE[sym], 1),
                        "usd": round(BALANCE[sym] * RATE[sym], 0), "rsrc": RATE_SRC[sym],
                        "partial": True, **({"delta": None} if sym == "MENTE" else {})})
        series[sym] = pts
    # recycle-signal scalars (MENTE). Signed daily change: newest − 7d-ago.
    # negative = draining, ~0 = frozen, positive = growing.
    _mente_in = sorted((f["ts"][:10] for f in inflows if f["tok"] == "MENTE"), reverse=True)
    _mente_rec = sorted((f["ts"][:10] for f in inflows if f["tok"] == "MENTE"
                         and f["from"].lower() == RECYCLE_SRC.lower()), reverse=True)
    _mente_out_days = sorted((r["ts"][:10] for r in rows if r["tok"] == "MENTE"), reverse=True)
    _closed = [p for p in series.get("MENTE", []) if not p.get("partial")]
    _chg = None; _dtz = None; _status = None; _flat = 0
    if len(_closed) >= 8:
        # trailing days with no MENTE movement at all (delta == 0) => frozen
        for p in reversed(_closed):
            if abs(p.get("delta") or 0) < 1:
                _flat += 1
            else:
                break
        _bnow = BALANCE.get("MENTE") or _closed[-1]["qty"]
        # rate of change measured over the ACTIVE window (exclude the flat tail)
        _active = _closed[:len(_closed) - _flat] if _flat else _closed
        if _flat >= 3:
            _status = "frozen"
        elif len(_active) >= 8:
            _chg = round((_active[-1]["qty"] - _active[-8]["qty"]) / 7.0, 1)
            if _chg < 0:
                _status = "declining"; _dtz = int(_bnow / -_chg) if _chg else None
            else:
                _status = "growing"
        else:
            _status = "frozen" if _flat else "declining"
    balance_series = {
        "tokens": [s for s in TOKENS if s in series],
        "series": series,
        "recon_block": RECON_BLOCK,
        "burn_offset": (recon.get("MENTE") or {}).get("delta"),
        "last_mente_in": _mente_in[0] if _mente_in else None,
        "last_mente_recycle": _mente_rec[0] if _mente_rec else None,
        "last_mente_out": _mente_out_days[0] if _mente_out_days else None,
        "mente_change_7d": _chg, "mente_status": _status, "days_to_zero": _dtz,
        "mente_flat_days": _flat,
        "moca_ledger_from": (min((r["ts"][:10] for r in rows if r["tok"] == "MOCA"), default=None)),
        "recon": {s: recon.get(s) for s in TOKENS},
    }
except Exception as _e:
    print("balance_series failed:", _e)

# large single inflows (>= $10k per transfer) — surfaced as a dashboard flag
large_inflows = [{"ts": f["ts"][:16], "tok": f["tok"], "val": round(f["val"], 0),
                  "usd": round(f["usd"], 0), "addr": f["from"], "tx": f["tx"],
                  "label": in_label(f["from"])[0], "note": in_label(f["from"])[1]}
                 for f in inflows if f["usd"] >= 10000]

facts = {"windows": windows, "prev24": prev24, "monthly": monthly, "daily": daily, "hourly": hourly,
         "large_inflows": large_inflows,
         "balance_series": balance_series,
         "top_recipients": top_recip, "in_sources": in_sources, "in_ledger": in_ledger,
         "wallets_all": len(wal_days), "wallets_repeat": repeat_wallets,
         "balance": {s: (round(BALANCE[s], 0) if BALANCE[s] is not None else None) for s in TOKENS},
         "balance_usd": {s: (round(BALANCE[s] * RATE[s], 0) if BALANCE[s] is not None else None) for s in TOKENS},
         "rate": RATE, "rate_src": RATE_SRC, "recon": recon,
         "pricing_provenance": pricing_provenance,
         "market_check": market_summary, "cognition": cognition, "swarm_era": swarm_era,
         "band_labels": BAND_LABEL, "band_keys": BAND_KEYS,
         "range": {"from": range_from, "to": rows[0]["ts"][:19] if rows else None}}

# ======================= LAYER 2 — INTERPRETATION =======================
# (rows were classified up in Layer 1, right after pricing)

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
    # grows lands ONLY in guard_private.json — the one surface where identity
    # labels are allowed, so reviewers keep context the public page lost.
    grows.append({"addr": c["addr"], "label": private_label(c["addr"]), "n": n,
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
def burn_di(cut, hi=None):
    """Unbacked classified burn: invoke/equip/credits/referrals, excluding
    Stripe-sized deliveries (fiat-purchase-backed)."""
    return sum(r["usd"] for r in rows if r["ts"] > cut and (hi is None or r["ts"] <= hi)
               and r["cat"] in ("invoke", "equip", "growth") and r["fine"] not in STRIPE_FINE)
def out_di(cut, hi=None):
    """Total factual outflow (every category incl. nonstandard/micro)."""
    return sum(r["usd"] for r in rows if r["ts"] > cut and (hi is None or r["ts"] <= hi))
# when every balance fetch failed, bal_usd of 0 would fake a 0-day runway
# (and fire the low-float alarm) — keep runway unknown instead
bal_known = any(BALANCE[s] is not None for s in TOKENS)
bal_usd = sum((BALANCE[s] or 0) * RATE[s] for s in TOKENS)
burn24 = burn_di(cut24)
burn_prev = burn_di(cut48, cut24)
span_days = max(min(7.0, (now - datetime.fromisoformat(rows[-1]["ts"]).replace(tzinfo=timezone.utc)).total_seconds() / 86400), 1.0) if rows else 7.0
burn7avg = burn_di(cut7) / span_days
out7avg = out_di(cut7) / span_days
runway24 = round(bal_usd / burn24, 1) if bal_known and burn24 > 0 else None
runway7 = round(bal_usd / burn7avg, 1) if bal_known and burn7avg > 0 else None
runway_total = round(bal_usd / out7avg, 1) if bal_known and out7avg > 0 else None

guard = {"flagged_n": len(flagged), "monitored_n": len(grows), "at_risk_usd": at_risk,
         "loop_n": len(loop_wallets), "loop_usd": loop_usd,
         "loop_gt10": loop_gt10, "loop_gt50": loop_gt50,
         "credit_recip_n": len(credit_recip),
         "ce_total_usd": ce_total,
         "runway24": runway24, "runway7": runway7, "runway_total": runway_total, "bal_usd": round(bal_usd, 0),
         "burn24": round(burn24, 2), "burn_prev": round(burn_prev, 2),
         "burn7avg": round(burn7avg, 2)}
# Per-wallet detector rows (addresses + ent/acf/burst signal values) are a
# calibration oracle — the Q3 flagged-wallets lesson. They now go ONLY to a
# git-ignored private file (rides the Actions cache for reviewer access),
# never into the public DATA/index.html/data.json.
# Auditable retired-straggler ledger, recomputed deterministically from the
# chain every run (council loop 3): the tripwire's alert_state copy rides an
# evictable cache; THIS is the ledger of record, tying every straggler to a
# tx hash. Private file — the member list is band-derived (oracle class).
from classify import RETIRED
retired_ledger = {}
for _cat, _rule in RETIRED.items():
    _hits = [{"ts": r0["ts"], "to": r0["to"], "tx": r0["tx"], "li": r0.get("li", ""),
              "usd": round(r0["usd"], 2)}
             for r0 in rows if r0["ts"][:10] > _rule["cutoff"]
             and abs(r0["usd"] - _rule["point"]) / _rule["point"] <= _rule["tol"]]
    retired_ledger[_cat] = {"cutoff": _rule["cutoff"], "n": len(_hits),
                            "usd": round(sum(h["usd"] for h in _hits), 2), "entries": _hits}
json.dump({"generated": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "rows": grows,
           "retired_ledger": retired_ledger},
          open(os.path.join(HERE, "guard_private.json"), "w"))

# Public aggregate for the retired-payout stragglers, derived from the SAME
# object that just went to the private file — one codepath, so the page and
# the private ledger can never state different counts. AGGREGATE FIELDS ONLY:
# no entries[], no tx hashes, no per-entry addresses, no detection band
# (point/tol) — those are the calibration-oracle class. check_publish.py
# --scan enforces the tx-hash/entries half of this in CI.
retired_public = []
for _cat, _v in retired_ledger.items():
    retired_public.append({
        "cat": _cat, "cutoff": _v["cutoff"], "n": _v["n"], "usd": _v["usd"],
        "last_seen": max((h["ts"][:10] for h in _v["entries"]), default=None)})
for _rp in retired_public:
    _src = retired_ledger[_rp["cat"]]
    assert _rp["n"] == len(_src["entries"]), f"retired_public {_rp['cat']}: n != len(entries)"
    assert _rp["usd"] == round(sum(h["usd"] for h in _src["entries"]), 2), \
        f"retired_public {_rp['cat']}: usd != sum(entries)"
    assert set(_rp) == {"cat", "cutoff", "n", "usd", "last_seen"}, \
        "retired_public carries a field outside the published aggregate contract"

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

infer = {"S": S, "creators": creators[:25], "ce_total": ce_total, "fine_table": fine_table, "guard": guard,
         "retired_public": retired_public}

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
         "generated_iso": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
         "source": "Blockscout (Base) token-transfer API; balances via eth_call; live prices via Blockscout exchange_rate (validated); historical USD via persisted day-implied payout rates",
         "complete": data_complete, "excluded_in_tx": excluded_in,
         # Mechanism only, deliberately WITHOUT a date claim: the reconcile
         # step in the hourly refresh runs with no MOCA_LEDGER_PATH and skips,
         # so no run-derived date exists that this file could truthfully stamp.
         # The real row-exact cross-check is the weekly reconcile.yml job.
         "cross_check": {"text": "Cross-checked weekly, row-exact, against an independent second crawler",
                         "repo": "https://github.com/Agentic-Po/moca-ledger"},
         "note": "This wallet only. Other Minds treasury wallets are out of scope. "
                 "Coupon credit deliveries flow from a separate distributor wallet and are "
                 "out of scope for this wallet's ledger — that wallet's own ledger is being banked "
                 "privately, so no coupon leg appears in any figure on this page."}

# ---- computed guided-view layer: insights, open items, gaps (panel-designed) ----
# out_di already covers every category — the old formula re-added
# nonstandard/micro on top and published an inflated pace (QA, loop 3)
dist_pace = round(out_di(cut7) / span_days, 2)
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
    {"item": "Reconcile Stripe-sized outflow vs recorded top-ups — the data platform team holds Stripe API access (feeds PostHog) and can close this end-to-end", "type": "follow-up", "owner": "Po → data platform team (Stripe API access confirmed)", "opened": "2026-07-18", "anchor": "serverCard"} if server else None,
    {"item": "Identify owner of 0xf605dBb5…1468f — the primary MENTE funder; three small early funders also remain unattributed", "type": "clarify", "owner": "Po + treasury ops", "opened": "2026-07-19", "anchor": "srcT"},
    {"item": "Formalize the recycle policy: collector→treasury flows are informal ops habit today — defining the rule defines who owns the economy's cash flow", "type": "clarify", "owner": "Po → platform lead", "opened": "2026-07-19", "anchor": "srcT"},
    {"item": "Subsidy-ratio trend: watch whether the weekly ratio bends down as revenue features land", "type": "trend", "owner": "dashboard (auto)", "opened": "2026-07-19", "anchor": "serverCard"},
    {"item": "Manual heartbeat: wallet stays solvent only by hand-refills — standing replenishment policy pending platform lead", "type": "follow-up", "owner": "Po → platform lead", "opened": "2026-07-19", "anchor": "plainStrip"},
]
open_items = [o for o in open_items if o]
gaps = [
    {"missing": "SWARM era (pre-Apr 2026) not yet integrated", "effect": "this dashboard covers the MENTE/MOCA credit era (from Apr 12/24); the economy's first generation ran on SWARM (Ethoswarm token) through the SAME collector hub 0xd850… — those flows are not yet counted", "unlocks": "full multi-era economy history: crawl the collector's SWARM in/outflows and add an era-aware timeline"},
    {"missing": "Recycle policy (constitutional)", "effect": "collector→treasury flows are informal; ownership of the economy's cash flow undefined", "unlocks": "closed-loop rule, creator revenue-share, or burn discipline — a protocol instead of a babysat wallet"},
    {"missing": "Complete Stripe data feed — the data platform team has Stripe API access (pulls for PostHog today, but only client-side events land)", "effect": "live revenue still client-side only; an interim VERIFIED snapshot (Stripe CSV, May 13–Jul 15: net $6,455) now anchors the true numbers — live feed needed for ongoing days", "unlocks": "true revenue-backed split; divergence control closes"},
    {"missing": "Per-transfer memo/event from the payout contract", "effect": "classification is size-inference (±8%); amber zone larger than it needs to be", "unlocks": "exact payout types — most of the amber zone becomes fact"},
    {"missing": "Wallet↔mind map (platform export)", "effect": "recipients are hex addresses; per-creator economics invisible", "unlocks": "named earnings leaderboard + per-wallet hold/spend/exit disposition — retires the farming debate with data"},
    {"missing": "MENTE burn-mechanism confirmation (platform)", "effect": "~1.2% of MENTE flow explained forensically but unconfirmed", "unlocks": "complete, auditable MENTE accounting; event emission restores full verifiability"},
    {"missing": "Manual-support wallet scope", "effect": "the $50K manual-support wallet (separate custody) is invisible to this dashboard", "unlocks": "whole-treasury view; no separate manual attestation needed"},
]
guard["dist_pace"] = dist_pace

# ================= COLLECTOR OUTFLOW — recycle vs idle sink =================
# Collected MENTE used to recycle back into this treasury daily. On 2026-06-19 that
# leg was redirected to a holding wallet that has never sent anything out. Both legs
# are measured here so the handover (and the fact the loop no longer closes) is visible.
SINK = "0xf0961686bC71B8A1f42E7888bD8160e9B6240f40"
sink = None
try:
    SINK_GENESIS_BLOCK = 47_400_000  # 2026-06-16, safely before the sink's first sweep (Jun 19)
    def _sweep(direction):
        acc, params = [], ""
        try:
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
        except Exception as _e:
            # the sink has no cache (it's small: ~1 tx/day) — refetch its whole
            # history from the chain when Blockscout v2 is down
            print(f"sink v2 fetch failed ({_e}) — eth_getLogs fallback")
            items = rpc_transfer_fallback(SINK, direction, [TOKENS["MENTE"]["addr"]], SINK_GENESIS_BLOCK)
            return [{"ts": i["timestamp"][:19],
                     "val": int(i["total"]["value"]) / 10 ** DECIMALS["MENTE"],
                     "cp": canon((i["from"] if direction == "to" else i["to"])["hash"])}
                    for i in items]

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
        print("collector balance v2 fetch failed, trying eth_call:", _e)
        try:
            _res = rpc("eth_call", [{"to": TOKENS["MENTE"]["addr"],
                                     "data": "0x70a08231" + "0" * 24 + COLLECTOR[2:].lower()}, "latest"])
            _bal = int(_res, 16) / 10 ** DECIMALS["MENTE"]
        except Exception as _e2:
            print("collector balance fallback failed:", _e2)

    # --- rebate-wallet monitor (Po, 2026-08-20): the sink is the Minds Rebate
    # wallet. DATops is expected to swap its accumulated MENTE to
    # MOCA roughly weekly — track both balances and the last swap (any MENTE
    # outflow), and flag when the swap is overdue so DATops can be reminded.
    def _erc20_bal(token_addr, holder):
        res = rpc("eth_call", [{"to": token_addr,
                                "data": "0x70a08231" + "0" * 24 + holder[2:].lower()}, "latest"])
        return int(res, 16) / 10 ** 18
    rebate = None
    try:
        _rb_mente = _erc20_bal(TOKENS["MENTE"]["addr"], SINK)
        _rb_moca = _erc20_bal(TOKENS["MOCA"]["addr"], SINK)
        _swaps = sorted({r["ts"][:10] for r in _out})
        _last_swap = _swaps[-1] if _swaps else None
        _days_since = ((datetime.now(timezone.utc).replace(tzinfo=None)
                        - datetime.strptime(_last_swap, "%Y-%m-%d")).days
                       if _last_swap else None)
        _mrate0 = RATE.get("MENTE") or TOKENS["MENTE"]["fallback_rate"]
        # overdue = no swap for >8 days (weekly cadence + 1 day slack) while a
        # material MENTE pile (>= $500) sits unswapped
        rebate = {"bal_mente": round(_rb_mente, 1), "bal_moca": round(_rb_moca, 1),
                  "bal_mente_usd": round(_rb_mente * _mrate0, 0),
                  "bal_moca_usd": round(_rb_moca * (RATE.get("MOCA") or TOKENS["MOCA"]["fallback_rate"]), 0),
                  "swap_days": _swaps, "last_swap": _last_swap,
                  "days_since_swap": _days_since,
                  "overdue": bool((_days_since is None or _days_since > 8)
                                  and _rb_mente * _mrate0 >= 500)}
    except Exception as _e:
        print("rebate balance fetch failed:", _e)

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
        sink = {"addr": SINK, "rebate": rebate,
                "days": len(_sdays), "first": _sdays[0], "last": _sdays[-1],
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

        # --- contract audit -------------------------------------------------
        # DATops is owed a contractual 40% of cognition spend. The old route
        # delivered 39.7% against total collector intake over ~7 weeks, which is
        # what validates that denominator; the same base is used for both routes.
        CONTRACT = 0.40
        _base_new = _cum_i
        _base_old = _rwin
        _exp_new, _got_new = _base_new * CONTRACT, sum(_sd.values())
        _by_month = {}
        for d, v in _sd.items():
            _by_month.setdefault(d[:7], [0.0, 0.0])[0] += v
        for d, v in _ci.items():
            if d >= _sdays[0]:
                _by_month.setdefault(d[:7], [0.0, 0.0])[1] += v
        sink["contract"] = {
            "rate": CONTRACT * 100,
            "old_pct": round(sum(_rd.values()) / _base_old * 100, 1) if _base_old else None,
            "new_pct": round(_got_new / _base_new * 100, 1) if _base_new else None,
            "expected": round(_exp_new), "actual": round(_got_new),
            "variance": round(_got_new - _exp_new),
            "variance_usd": round((_got_new - _exp_new) * _mrate),
            "months": [{"m": m, "pct": round(v[0] / v[1] * 100, 1), "short": round(v[1] * CONTRACT - v[0])}
                       for m, v in sorted(_by_month.items()) if v[1]],
        }
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
    # Funding-source roles are structural placeholders — the identity map is
    # private (moca-ledger-private:labels/). Mimic warnings name no victim.
    _reg("0x4d3021a52b31ffafde3c46450d02c72807c3a178", f"{in_label('0x4d3021a52b31ffafde3c46450d02c72807c3a178')[0] or 'Funding wallet'} — manual MOCA top-ups", "Funding sources"),
    _reg("0xf605dbb5626dfc1448cee33e2e1221103021468f", f"{in_label('0xf605dbb5626dfc1448cee33e2e1221103021468f')[0] or 'Funding wallet'} — primary MENTE funder", "Funding sources"),
    _reg(SINK, "Minds Rebate wallet — receives the daily 40% MENTE sweep from the collector since 2026-06-19; DATops swaps its MENTE to MOCA on a weekly cadence", "Collector"),
    _reg("0x63c0c19a282a1B52b07dD5a65b58948A07DAE32B", "EIP-7702 delegator implementation the treasury EOA delegates to", "Infrastructure"),
    _reg("0x45d0cEAd7c0a2E1a0528C4131A2d95DE9a394839", f"{in_label('0x45d0cEAd7c0a2E1a0528C4131A2d95DE9a394839')[0] or 'Funding wallet'} — early MENTE funder (Apr 2026)", "Funding sources"),
    _reg("0xbDCb95A80d4C770fa811B1FAF0bb4Cf204d310b5", f"{in_label('0xbDCb95A80d4C770fa811B1FAF0bb4Cf204d310b5')[0] or 'Funding wallet'} — early MENTE funder (Apr–May 2026)", "Funding sources"),
    _reg("0x0a2854Fbbd9B3Ef66F17d47284E7f899b9509330", "Swap counterparty — took 72k MENTE, returned 112k MOCA; venue unconfirmed", "Liquidity"),
    _reg("0xd8506866faadfdcfb9600479ba7dc652a203f111", "known mimic — do not copy", "Warnings", True),
    _reg("0x9a95a47a4f90c9c14ae8e3a9c37e822ed0e5a07f", "known mimic — do not copy", "Warnings", True),
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
    # "creator wallet" is a structural tag, not a curated call-out — putting
    # those wallets in the registry would single them out by name-lessness.
    if _a not in _have and _l != "creator wallet":
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

# schema_version 2 (2026-08-30): identity labels redacted from all public
# fields; transfers_export.csv dropped counterparty_label. See CONSUMERS.md.
data = {"schema_version": 2,
        "scope": scope, "facts": facts, "infer": infer, "server": server, "stripe_snap": stripe_snap,
        "insights": insights, "open_items": open_items, "gaps": gaps, "registry": registry, "sink": sink}

# machine-readable copy of exactly what the page embeds — alerts.py/notify.py
# read this instead of regex-scraping index.html (council item 3, 2026-08-28).
# Strict subset of the public page, so it exposes nothing new.
json.dump(data, open(os.path.join(HERE, "data.json"), "w"), default=str)

json.dump(STATE, open(RATES_PATH, "w"), indent=0)

# --- per-tx export with rate provenance ---
with open(os.path.join(HERE, "transfers_export.csv"), "w", newline="") as fh:
    w = csv.writer(fh)
    # class_coarse/class_fine speak classify_usd's canonical vocabulary so an
    # auditor can tie every CSV row to the page fine_table and the Telegram
    # digest without re-implementing the taxonomy; log_index completes the
    # (tx_hash, log_index) primary key for multi-transfer transactions.
    # schema v2: no counterparty_label column — identity labels are private.
    w.writerow(["timestamp_utc", "direction", "token", "amount", "rate_usd", "rate_source", "usd", "size_band", "counterparty", "tx_hash", "log_index", "class_coarse", "class_fine"])
    for r in rows:
        w.writerow([r["ts"], "OUT", r["tok"], f"{r['val']:.6f}", f"{r['rate']:.8f}", r["rsrc"], f"{r['usd']:.4f}",
                    BAND_LABEL[band(r["usd"])], r["to"], r["tx"],
                    r.get("li", ""), r.get("cat", ""), r.get("fine", "")])
    for f in inflows:
        w.writerow([f["ts"], "IN", f["tok"], f"{f['val']:.6f}", f"{f['rate']:.8f}", f["rsrc"], f"{f['usd']:.4f}",
                    "", f["from"], f["tx"], f.get("li", ""), "", ""])

# --- snapshot history (append-only; git history is the immutable trail) ---
hist_path = os.path.join(HERE, "stats_history.json")
hist = json.load(open(hist_path)) if os.path.exists(hist_path) else []
hist.append({"ts": now.strftime("%Y-%m-%dT%H:%M"),
             "recon": {s: {"delta": recon[s]["delta"], "drift": recon[s]["drift"]}
                       for s in TOKENS if recon.get(s)},
             "invoke": S["tot"]["invoke"]["n"], "equip": S["tot"]["equip"]["n"],
             "growth": S["tot"]["growth"]["n"],
             "moca": round(sum(r["val"] for r in rows if r["tok"] == "MOCA"), 1),
             "creators": S["creators_n"], "rate": RATE["MOCA"],
             "balance": round(BALANCE["MOCA"], 1) if BALANCE["MOCA"] is not None else None,
             "mente_balance": round(BALANCE["MENTE"], 1) if BALANCE.get("MENTE") is not None else None,
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
    # legacy view is intentionally FROZEN on the old taxonomy (no $20/$100
    # packs) — it exists to match what stakeholders originally saw; do not extend.
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
              "rows_private_n": len(lgrows)}
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

# --- data catalog (LAST: it measures files, so every one must be written) ---
# The spec placed this right after data.json; it has to run at the END instead,
# because transfers_export.csv and stats_history.json are written below that
# point and `catalog.py --check` immediately after a refresh would otherwise
# see one more row than the catalog recorded. Imported, not shelled out.
import catalog
catalog.build()

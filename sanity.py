#!/usr/bin/env python3
"""Monitor-of-the-monitor: an independent recompute-and-diff publish gate.

Council verdict 2026-09-21: in three weeks live, six detectors never fired
while four real incidents shipped (blank charts under green checks, a
shadowed-import crash, an oracle that priced MOCA at exactly 2x, and a
rounding nudge that paged Po every third run). The system could not tell
"the treasury is fine" from "the monitor is wrong". This gate recomputes the
headline facts from the RAW inputs (shards + day_rates.json) by a path that
does NOT import refresh.py, diffs them against the published data.json, and
blocks the commit when the two disagree beyond an explicit, tiered bound.

It may use shards.py for row access and classify.py for sizing (the
one-classifier rule); the totals, the balance check and the pricing sanity
are derived here.

Tiers — the council's binding warning was that a gate like this is
structurally the thing that caused the Sep 18-20 false-page storm, so every
check names its tier and only EXACT/BOUNDED-over-bound can block:

  EXACT    any difference BLOCKS publish (exit 1)
             out row count, out wallet count, raw MOCA/MENTE totals per
             window (24h/7d/30d/all), closed-day count, schema_version
             present, every window's group keys
  BOUNDED  blocks only beyond the bound, logs below it
             USD totals per window       max(0.5%, $1)
             balance_usd vs live eth_call x live rate        1.5%  (online)
             runway days (7d pace)                           5%
             MOCA/MENTE day rate vs the day's market close   6%   (MARKET_AGREE)
             live rate vs an second-source quote (DexScreener; NOTE: for MENTE the published rate may itself be DexScreener, so that leg is a consistency check, not an independent source)   6%   (online)
  LOG      never blocks, never pages: one line in the run log — including
           the cent-scale rounding drift of Sep 18-20
  WARN     a bounded check at >= 50% of its bound, a live leg that could not
           run online, or a degraded peer catalog is queued for the next
           digest via state.warn() (never its own message)

Always prints one summary line:
  SANITY: N exact ok, M bounded ok, K logged drift
plus a detail line per non-clean check. Blocks additionally print ::error::.

  python3 sanity.py                 full gate (live balance + live quote legs)
  python3 sanity.py --offline       no network: live legs skipped (CI, tests)
  python3 sanity.py --root DIR      gate a tree other than this checkout

The same raw-row pass also banks the PRIVATE anomaly classification (item 3:
repeat credit grants + daily grant-wallet series, and — loop 2 — the
slow-bleed grant measurement, fences.grant_bleed) into guard_private.json
when that file exists — Actions cache only, never published. The bleed
measurement is WARN-batched on a new 30-day high and NEVER paged: it is a
number to watch until the platform answers the account-to-wallet question,
not a sybil claim (see fences.py).
"""
import json, os, sys, time, urllib.request
from datetime import datetime, timedelta

import shards
import fences
from classify import classify_usd, pin_rate, group_for, GROUP_KEYS

HERE = os.path.dirname(os.path.abspath(__file__))

USD_BOUND_PCT = 0.5          # BOUNDED: USD totals per window, or $1 if larger
USD_BOUND_MIN = 1.0
BAL_BOUND_PCT = 1.5          # BOUNDED: balance_usd vs live eth_call x rate
RUNWAY_BOUND_PCT = 5.0       # BOUNDED: days_7d_pace
MARKET_AGREE_PCT = 6.0       # BOUNDED: day rate vs market close; live rate vs quote
WARN_AT = 0.5                # fraction of a bound that queues a WARN
PRICE_DAYS = 30              # closed days scored against their market close
# The implied payout oracle was retired on this day (refresh.py's
# IMPLIED_NEEDS_MARKET_FROM — mirrored, not imported): from here every day
# rate is a market close, so ANY disagreement with the close is the Sep 15
# collision class. Before it, implied days legitimately sat up to 22% off
# their close and are sealed history — scoring them would re-create the
# false-block storm (backtest: 2026-08-21 at 6.5% blocked every 18-20 Sep run).
MARKET_ONLY_FROM = "2026-09-14"
WINDOWS = (("24h", 24), ("7d", 24 * 7), ("30d", 24 * 30), ("all history", None))
# Different path than refresh.py on purpose: its endpoint order is
# mainnet.base.org -> drpc -> blockscout; this leg starts elsewhere so a
# single misbehaving RPC cannot feed both the figure and its check.
RPC_ENDPOINTS = ["https://base.drpc.org", "https://base-rpc.publicnode.com",
                 "https://mainnet.base.org"]
DEXSCREENER = "https://api.dexscreener.com/latest/dex/tokens/"


class Report:
    """Collects check results by tier and renders the summary."""

    def __init__(self):
        self.exact_ok = self.bounded_ok = 0
        self.blocks, self.logs, self.warns, self.skipped = [], [], [], []

    def exact(self, name, published, mine, fmt="{}", eps=0.0):
        """eps only absorbs float summation order on values BOTH sides
        already rounded the same way; a real row difference is far larger."""
        same = (published == mine) or (eps and isinstance(published, (int, float))
                                       and isinstance(mine, (int, float))
                                       and abs(published - mine) <= eps)
        if same:
            self.exact_ok += 1
        else:
            self.blocks.append(f"BLOCK EXACT {name}: published {fmt.format(published)} "
                               f"recompute {fmt.format(mine)}")

    def bounded(self, name, published, mine, bound, unit="$", warn_key=None):
        """|published - mine| against an absolute bound. Under the bound the
        difference is LOG only; at or over it, BLOCK. >= WARN_AT of the bound
        additionally queues a WARN for the next digest."""
        d = abs(published - mine)
        f = (lambda v: f"{unit}{v:,.4f}") if unit else (lambda v: f"{v:.6g}")
        line = (f"{name}: published {f(published)} recompute {f(mine)} "
                f"(Δ {f(d)}, bound {f(bound)})")
        if d >= bound:
            self.blocks.append("BLOCK BOUNDED " + line)
        elif d == 0:
            self.bounded_ok += 1
        else:
            self.bounded_ok += 1
            self.logs.append("LOG " + line)
            if d >= WARN_AT * bound:
                self.warns.append((warn_key or name, f"sanity: {name} drifted Δ {f(d)} "
                                                     f"({d / bound:.0%} of its {f(bound)} bound)"))

    def skip(self, name, why, warn=False):
        self.skipped.append(f"SKIP {name}: {why}")
        if warn:
            self.warns.append((name, f"sanity: {name} could not run ({why})"))

    def summary(self):
        return (f"SANITY: {self.exact_ok} exact ok, {self.bounded_ok} bounded ok, "
                f"{len(self.logs)} logged drift" + (f", {len(self.blocks)} BLOCK" if self.blocks else ""))

    def blocked(self):
        return bool(self.blocks)


# ---------------------------------------------------------------- raw inputs
def load_raw(root):
    """Published artifacts + raw rows priced by classify.pin_rate. Rows carry
    only what the checks need; nothing here is refresh.py's row dict."""
    D = json.load(open(os.path.join(root, "data.json")))
    dr = json.load(open(os.path.join(root, "day_rates.json")))
    toks = {a.lower(): s for s, a in D["scope"]["tokens"].items()}
    syms = list(D["scope"]["tokens"])
    rates = {s: dict(dr["day_rates"].get(s, {})) for s in syms}
    for s, od in (dr.get("open_day_rate") or {}).items():
        rates.setdefault(s, {}).setdefault(od["d"], od["rate"])
    live = D["facts"]["rate"]
    rows = []
    for i in shards.load(os.path.join(root, "transfers")):
        s = toks.get(i["token"]["address_hash"].lower())
        if not s:
            continue
        dec = int(i["total"].get("decimals") or 18)
        val = int(i["total"]["value"]) / 10 ** dec
        ts = i["timestamp"][:19]
        usd = val * pin_rate(rates.get(s, {}), ts[:10], live.get(s) or 0)
        coarse, fine, _ = classify_usd(usd, ts)
        rows.append({"ts": ts, "tok": s, "val": val, "usd": usd, "to": i["to"]["hash"].lower(),
                     "grp": group_for(coarse, fine)})
    ins = []
    for i in shards.load(os.path.join(root, "transfers_in")):
        s = toks.get(i["token"]["address_hash"].lower())
        if not s:
            continue
        dec = int(i["total"].get("decimals") or 18)
        ins.append({"ts": i["timestamp"][:19], "tok": s, "val": int(i["total"]["value"]) / 10 ** dec})
    return D, dr, rows, ins, syms


def _cut(gen, hours):
    return "0" if hours is None else (gen - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")


# ------------------------------------------------------------------- checks
def check_windows(rep, D, rows, ins, syms, gen):
    byw = {w.get("label"): w for w in D["facts"].get("windows", [])}
    rep.exact("window labels", [w[0] for w in WINDOWS], list(byw))
    for label, hours in WINDOWS:
        w = byw.get(label)
        if not w:
            rep.blocks.append(f"BLOCK EXACT window {label!r} missing from data.json")
            continue
        cut = _cut(gen, hours)
        rs = [r for r in rows if r["ts"] > cut]
        fs = [f for f in ins if f["ts"] > cut]
        rep.exact(f"{label} group keys", GROUP_KEYS, list((w.get("groups") or {}).keys()))
        rep.exact(f"{label} out_tx", w.get("out_tx"), len(rs))
        rep.exact(f"{label} in_tx", w.get("in_tx"), len(fs))
        rep.exact(f"{label} out_wallets", w.get("out_wallets"), len({r["to"] for r in rs}))
        for s in syms:
            # published to 0.1 token; both sides rounded the same way, then
            # compared exactly (a dropped dust row moves the raw total by its
            # own value, never by a float-order rounding artefact)
            mine = round(sum(r["val"] for r in rs if r["tok"] == s), 1)
            pub = (w.get("out_raw") or {}).get(s)
            rep.exact(f"{label} out_raw {s}", pub, mine, eps=1e-6)
        out_usd = sum(r["usd"] for r in rs)
        bound = max(USD_BOUND_MIN, out_usd * USD_BOUND_PCT / 100)
        rep.bounded(f"{label} out_usd", float(w.get("out_usd") or 0), out_usd, bound)
        for g in GROUP_KEYS:
            pg = (w.get("groups") or {}).get(g) or {}
            gu = sum(r["usd"] for r in rs if r["grp"] == g)
            rep.bounded(f"{label} groups.{g}.usd", float(pg.get("usd") or 0), gu,
                        max(USD_BOUND_MIN, gu * USD_BOUND_PCT / 100))
            rep.exact(f"{label} groups.{g}.n", pg.get("n"), sum(1 for r in rs if r["grp"] == g))


def check_days(rep, D, rows, root, gen):
    """Closed-day count: day_digests.json seals every day with OUT rows that
    is at least two days old (digests.enforce's one-day grace)."""
    try:
        sealed = json.load(open(os.path.join(root, "day_digests.json")))
    except (OSError, ValueError):
        sealed = {}
    today = gen.date()
    closed = {r["ts"][:10] for r in rows
              if (today - datetime.fromisoformat(r["ts"][:10]).date()).days >= 2}
    rep.exact("closed days sealed", len(sealed), len(closed))
    rep.exact("schema_version present", True, isinstance(D.get("schema_version"), int))


def check_prices(rep, dr, syms, gen):
    """Published day rate vs the day's market close (MARKET_AGREE idea): the
    Sep 15 incident was a day rate at exactly 2.00x its market close. Scored
    over the trailing PRICE_DAYS calendar days of the run clock AND only
    from MARKET_ONLY_FROM: closed days never reprice, so a legacy implied-era
    day must never be able to block a publish."""
    floor = max((gen - timedelta(days=PRICE_DAYS)).strftime("%Y-%m-%d"),
                (datetime.fromisoformat(MARKET_ONLY_FROM) - timedelta(days=1)).strftime("%Y-%m-%d"))
    for s in syms:
        rates = dr["day_rates"].get(s) or {}
        mkt = (dr.get("market_rates") or {}).get(s) or {}
        days = sorted(d for d in rates if d in mkt and d > floor)
        for d in days:
            if not mkt[d]:
                continue
            rep.bounded(f"{s} day rate {d} vs market close", rates[d], mkt[d],
                        mkt[d] * MARKET_AGREE_PCT / 100, unit="", warn_key=f"price_{s}")
        od = (dr.get("open_day_rate") or {}).get(s) or {}
        mo = (dr.get("market_open") or {}).get(s) or {}
        if od.get("rate") and mo.get("close") and od.get("d") == mo.get("d"):
            rep.bounded(f"{s} open-day rate {od['d']} vs running candle", od["rate"], mo["close"],
                        mo["close"] * MARKET_AGREE_PCT / 100, unit="", warn_key=f"price_{s}")


def _eth_call_balance(token_addr, holder):
    data = "0x70a08231" + "0" * 24 + holder[2:].lower()
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                          "params": [{"to": token_addr, "data": data}, "latest"]}).encode()
    last = None
    for url in RPC_ENDPOINTS:
        try:
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json",
                                                                     "User-Agent": "curl/8.4.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                res = json.load(r).get("result")
            if res:
                return int(res, 16) / 1e18
            last = f"{url}: empty result"
        except Exception as e:                       # noqa: BLE001 — every endpoint is tried
            last = f"{url}: {type(e).__name__}"
    raise RuntimeError(last or "no endpoint")


def _dexscreener_price(token_addr, anchor):
    with urllib.request.urlopen(urllib.request.Request(
            DEXSCREENER + token_addr, headers={"User-Agent": "curl/8.4.0"}), timeout=10) as r:
        dx = json.load(r)
    pairs = [p for p in (dx.get("pairs") or [])
             if p.get("priceUsd") and (p.get("baseToken") or {}).get("address", "").lower() == token_addr
             and anchor / 5 < float(p["priceUsd"]) < anchor * 5]
    if not pairs:
        raise RuntimeError("no sane pair")
    best = sorted(pairs, key=lambda p: -float((p.get("liquidity") or {}).get("usd") or 0))[0]
    return float(best["priceUsd"])


def check_balance(rep, D, syms, offline):
    """balance_usd vs an independent eth_call x the published live rate; the
    live rate itself vs an independent quote. Both online-only."""
    pub = D["facts"].get("balance_usd") or {}
    rate = D["facts"]["rate"]
    wallet = D["scope"]["wallet"]
    live_bal = {}
    for s, addr in D["scope"]["tokens"].items():
        if offline:
            rep.skip(f"balance_usd {s} vs live eth_call", "offline")
            continue
        try:
            bal = _eth_call_balance(addr.lower(), wallet)
        except Exception as e:                       # noqa: BLE001
            rep.skip(f"balance_usd {s} vs live eth_call", f"eth_call failed: {e}", warn=True)
            continue
        live_bal[s] = bal
        mine = bal * (rate.get(s) or 0)
        bound = max(1.0, mine * BAL_BOUND_PCT / 100)
        rep.bounded(f"balance_usd {s} vs live eth_call x rate", float(pub.get(s) or 0), mine, bound,
                    warn_key="balance")
    for s, addr in D["scope"]["tokens"].items():
        if offline:
            rep.skip(f"live rate {s} vs DexScreener", "offline")
            continue
        if not rate.get(s):
            continue
        try:
            q = _dexscreener_price(addr.lower(), rate[s])
        except Exception as e:                       # noqa: BLE001
            rep.skip(f"live rate {s} vs DexScreener", f"quote failed: {e}", warn=True)
            continue
        rep.bounded(f"live rate {s} vs DexScreener quote", rate[s], q, q * MARKET_AGREE_PCT / 100,
                    unit="", warn_key=f"rate_{s}")
    return live_bal


def check_runway(rep, D, rows, syms, gen, live_bal):
    """days_7d_pace = balance / (7d total outflow / span days). Balance from
    the live leg when it ran (independent), else the published one — the
    5% bound absorbs an hour of outflow drift either way."""
    fl = D["facts"].get("float") or {}
    pub = fl.get("days_7d_pace")
    if pub is None:
        rep.skip("runway days_7d_pace", "not published (balance unknown)")
        return
    rate = D["facts"]["rate"]
    bal_pub = D["facts"].get("balance_usd") or {}
    bal = sum((live_bal[s] * (rate.get(s) or 0)) if s in live_bal else float(bal_pub.get(s) or 0)
              for s in syms)
    oldest = min((r["ts"] for r in rows), default=None)
    span = 7.0
    if oldest:
        span = max(min(7.0, (gen - datetime.fromisoformat(oldest)).total_seconds() / 86400), 1.0)
    out7 = sum(r["usd"] for r in rows if r["ts"] > _cut(gen, 24 * 7))
    if out7 <= 0:
        rep.skip("runway days_7d_pace", "no outflow in 7d")
        return
    mine = bal / (out7 / span)
    rep.bounded("runway days_7d_pace", float(pub), mine, max(0.1, mine * RUNWAY_BOUND_PCT / 100),
                unit="", warn_key="runway")


def check_peer(rep, root):
    p = os.path.join(root, "DATASETS.md")
    if os.path.exists(p) and "degraded: peer catalog not fetched" in open(p, errors="replace").read():
        rep.warns.append(("peer_catalog", "sanity: peer catalog (moca-ledger) not fetched this run — DATASETS.md degraded"))


# ------------------------------------------------ item 3: private anomaly pass
GRANT_BUCKETS = (("1", 1, 1), ("2-5", 2, 5), ("6-10", 6, 10), ("11-50", 11, 50), (">50", 51, None))


def anomaly_pass(rows, gen):
    """Repeat credit grants + the daily distinct-grant-wallet series. PRIVATE
    by construction (per-wallet counts, top-20 wallets) — guard_private.json
    only. Grants = classify.group_for 'credit_grants' ($3 credit / referral $5);
    the $1 system top-up has its own one-per-wallet detector in alerts.py.
    No account identifiers: the chain shows wallets, this reports wallets."""
    per = {}
    for r in rows:
        if r["grp"] != "credit_grants":
            continue
        c = per.setdefault(r["to"], {"n": 0, "usd": 0.0, "first": r["ts"], "last": r["ts"]})
        c["n"] += 1
        c["usd"] += r["usd"]
        c["first"] = min(c["first"], r["ts"])
        c["last"] = max(c["last"], r["ts"])
    total_usd = sum(c["usd"] for c in per.values())
    buckets = {}
    for label, lo, hi in GRANT_BUCKETS:
        cs = [c for c in per.values() if c["n"] >= lo and (hi is None or c["n"] <= hi)]
        buckets[label] = {"wallets": len(cs), "grants": sum(c["n"] for c in cs),
                          "usd": round(sum(c["usd"] for c in cs), 2)}
    rep_w = [c for c in per.values() if c["n"] > 1]
    top = sorted(per.items(), key=lambda kv: (-kv[1]["n"], -kv[1]["usd"]))[:20]
    repeat = {"as_of": gen.strftime("%Y-%m-%dT%H:%M:%SZ"), "grant_wallets": len(per),
              "grant_usd": round(total_usd, 2), "buckets": buckets,
              "repeat_wallets": len(rep_w), "repeat_usd": round(sum(c["usd"] for c in rep_w), 2),
              "repeat_share_pct": round(sum(c["usd"] for c in rep_w) / total_usd * 100, 1) if total_usd else 0.0,
              "heaviest_grants": top[0][1]["n"] if top else 0,
              "top20": [{"addr": a, "n": c["n"], "usd": round(c["usd"], 2),
                         "first": c["first"][:10], "last": c["last"][:10]} for a, c in top]}
    cut = (gen - timedelta(days=60)).strftime("%Y-%m-%d")
    days = {}
    for r in rows:
        d = r["ts"][:10]
        if r["grp"] != "credit_grants" or d < cut:
            continue
        x = days.setdefault(d, {"w": set(), "n": 0, "usd": 0.0})
        x["w"].add(r["to"])
        x["n"] += 1
        x["usd"] += r["usd"]
    series = [{"d": d, "wallets": len(x["w"]), "n": x["n"], "usd": round(x["usd"], 2)}
              for d, x in sorted(days.items())]
    return repeat, series


def bank_private(root, repeat, series, bleed=None):
    """Merge into guard_private.json if it exists (refresh.py creates it each
    run; alerts.py merges the same way). Atomic, never creates a public file."""
    gp = os.path.join(root, "guard_private.json")
    if not os.path.exists(gp):
        return False
    try:
        g = json.load(open(gp)) or {}
    except ValueError:
        g = {}
    g["repeat_grants"] = repeat
    g["daily_grant_wallets"] = series
    if bleed is not None:
        g["grant_bleed"] = bleed
    tmp = gp + ".tmp"
    json.dump(g, open(tmp, "w"))
    os.replace(tmp, gp)
    return True


# --------------------------------------------------------------------- main
def run(root=HERE, offline=False, bank=True, queue_warns=True):
    t0 = time.time()
    rep = Report()
    D, dr, rows, ins, syms = load_raw(root)
    gen = datetime.fromisoformat(D["scope"]["generated_iso"].rstrip("Z")[:19])
    check_windows(rep, D, rows, ins, syms, gen)
    check_days(rep, D, rows, root, gen)
    check_prices(rep, dr, syms, gen)
    live_bal = check_balance(rep, D, syms, offline)
    check_runway(rep, D, rows, syms, gen, live_bal)
    check_peer(rep, root)
    repeat, series = anomaly_pass(rows, gen)
    # loop 2, item 3: the slow-bleed measurement — private, WARN on a new
    # 30-day high of the 7 d repeat-grant share, never a page
    bleed = fences.grant_bleed(((datetime.fromisoformat(r["ts"]), r["usd"], r["to"])
                                for r in rows if r["grp"] == "credit_grants"), gen)
    if bleed["new_high"]:
        rep.warns.append(("grant_bleed", fences.bleed_line(bleed)))
    banked = bank_private(root, repeat, series, bleed) if bank else False
    if queue_warns and rep.warns and not rep.blocked():
        # a BLOCK is its own page; WARNs only matter on a run that publishes
        import state as _state
        for key, text in rep.warns:
            _state.warn(f"sanity:{key}", text)
    return rep, {"rows": len(rows), "gen": gen, "secs": time.time() - t0, "banked": banked,
                 "repeat": repeat, "bleed": bleed}


def main(argv):
    offline = "--offline" in argv
    root = HERE
    if "--root" in argv:
        root = os.path.abspath(argv[argv.index("--root") + 1])
    rep, meta = run(root, offline=offline, queue_warns=root == HERE)
    print(rep.summary())
    for ln in rep.blocks:
        print("  " + ln)
        print("::error::" + ln)
    for ln in rep.logs + rep.skipped:
        print("  " + ln)
    for key, text in rep.warns:
        print(f"  WARN queued [{key}]: {text}")
    print(f"  rows {meta['rows']:,} · clock {meta['gen']:%Y-%m-%dT%H:%M:%S}Z · "
          f"{meta['secs']:.1f}s · private anomaly pass {'banked' if meta['banked'] else 'not banked (no guard_private.json)'}")
    b = meta["bleed"]
    print(f"  grant bleed (private): 7d repeat share {b['share_7d_pct']:g}% of ${b['grant_usd_7d']:,.0f} · "
          f"30d ref median {b['ref_median_pct']} / max {b['ref_max_pct']} ({b['ref_days']} d) · "
          f"crossed 5/20 lifetime grants this week {b['crossed_5']}/{b['crossed_20']}"
          + (" · NEW 30d HIGH (WARN queued)" if b["new_high"] else ""))
    # refresh.yml's failure notice reads this to name the gate and its tier
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write("summary=" + rep.summary() + (" — " + rep.blocks[0] if rep.blocks else "") + "\n")
    return 1 if rep.blocked() else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

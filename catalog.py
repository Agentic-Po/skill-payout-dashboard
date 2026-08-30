#!/usr/bin/env python3
"""Computed catalog of every dataset this repo publishes.

Nothing here is hand-typed: rows, bytes and coverage are MEASURED off the
files on every build, so DATASETS.md cannot drift from the data the way a
hand-maintained table does. Private datasets appear by name only — no
schema, no path, no coverage — because a row schema is itself a calibration
hint for the detector they feed.

  python3 catalog.py           rebuild catalog.json + DATASETS.md
  python3 catalog.py --check   recompute and exit 1 on disagreement

--check tolerance: everything is compared exactly except (a) generated_iso,
which is build time, and (b) on datasets marked live, the fields that the
CURRENT month legitimately grows between two builds (rows, bytes,
coverage.to, max_row_ts, source_generated_iso) — compared as recomputed >=
committed. rows_closed (rows in months strictly before the current UTC month)
is always exact, so a closed shard can never change unnoticed.

MONTH ROLLOVER: because rows_closed is exact, an out-of-band --check that
spans a UTC month boundary (committed before the 1st, recomputed after) fails
on rows_closed by design — last month's rows have just become closed. That is
the check working, not data corruption; rebuild the catalog and move on.
"""
import csv, json, os, sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG = os.path.join(HERE, "catalog.json")
DATASETS_MD = os.path.join(HERE, "DATASETS.md")
PEER_CATALOG_URL = "https://raw.githubusercontent.com/Agentic-Po/moca-ledger/main/catalog.json"

KINDS = ["ledger", "oracle", "derived", "archive"]


def _now_month():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _size(rel):
    p = os.path.join(HERE, rel)
    if os.path.isdir(p):
        return sum(os.path.getsize(os.path.join(p, f)) for f in os.listdir(p)
                   if os.path.isfile(os.path.join(p, f)))
    return os.path.getsize(p) if os.path.exists(p) else 0


def _measured(stamps):
    """(rows, rows_closed, from, to) from a list of ISO timestamp strings."""
    cur = _now_month()
    if not stamps:
        return 0, 0, None, None
    return (len(stamps), sum(1 for s in stamps if s[:7] < cur),
            min(stamps), max(stamps))


# ---- measure functions: each returns a list of ISO stamps, one per row ----

def _shard_stamps(rel, ts_key="timestamp"):
    import shards
    return [r[ts_key][:19] for r in shards.load(os.path.join(HERE, rel), ts_key=ts_key)]


def _csv_stamps(rel):
    p = os.path.join(HERE, rel)
    if not os.path.exists(p):
        return []
    with open(p, newline="") as fh:
        rd = csv.reader(fh)
        next(rd, None)                                  # header
        return [r[0][:19] for r in rd if r and r[0]]


def _day_rate_stamps(rel):
    st = json.load(open(os.path.join(HERE, rel)))
    return sorted(d for sym in st.get("day_rates", {}) for d in st["day_rates"][sym])


def _swarm_stamps(rel):
    se = json.load(open(os.path.join(HERE, rel)))
    return sorted(r["ts"][:19] for side in ("in", "out") for r in se.get(side, []))


def _data_generated(rel):
    """data.json carries its OWN build stamp. Recording it per-dataset means
    catalog.json can answer "when were these figures written?" and not only
    "when did catalog.py last run?" — the distinction notify.py's second
    freshness leg depends on."""
    return json.load(open(os.path.join(HERE, rel))).get("scope", {}).get("generated_iso")


def _data_stamps(rel):
    d = json.load(open(os.path.join(HERE, rel)))
    rng = d.get("facts", {}).get("range", {})
    # data.json is one document over a window; its "rows" is that one document,
    # its coverage is the transfer range it summarises.
    return [rng.get("from") or "", rng.get("to") or ""]


def _hist_stamps(rel):
    return [h["ts"] for h in json.load(open(os.path.join(HERE, rel)))]


PUBLIC = [
    dict(name="transfers", path=["transfers/"], kind="ledger", live=True,
         measure=lambda: _shard_stamps("transfers"),
         row_schema="timestamp, transaction_hash, log_index, block_number, from.hash, to.hash, token.address_hash, total.value, total.decimals",
         update_cadence="every refresh (~4x/hour)", expected_cadence_minutes=15,
         provenance="Blockscout v2 token-transfers (filter=from) → eth_getLogs fallback → 24h cross-check",
         not_included="outbound only; inbound lives in transfers_in/ (partial: tracked tokens only)"),
    dict(name="transfers_in", path=["transfers_in/"], kind="ledger", live=True,
         measure=lambda: _shard_stamps("transfers_in"),
         row_schema="timestamp, transaction_hash, log_index, block_number, from.hash, to.hash, token.address_hash, total.value, total.decimals",
         update_cadence="every refresh (~4x/hour)", expected_cadence_minutes=15,
         provenance="Blockscout v2 token-transfers (filter=to) → eth_getLogs fallback → 24h cross-check",
         not_included="every token the wallet ever received is stored, but only MOCA and MENTE are priced and counted — airdrops/spam are kept raw and excluded downstream"),
    dict(name="cognition_in", path=["cognition_in/"], kind="ledger", live=True,
         measure=lambda: _shard_stamps("cognition_in", "ts"),
         row_schema="ts, val (MENTE token amount, float), from, tx, log_index, transaction_hash",
         update_cadence="every refresh (~4x/hour)", expected_cadence_minutes=15,
         provenance="Blockscout v2 token-transfers into the Cognition Credits collector → eth_getLogs fallback → 24h cross-check",
         not_included="MENTE only, collector-inbound only; rows are pre-slimmed and val is a float token amount, not wei — see rows.py for the precision caveat"),
    dict(name="transfers_export", path=["transfers_export.csv"], kind="derived", live=True,
         measure=lambda: _csv_stamps("transfers_export.csv"),
         row_schema="timestamp_utc, direction, token, amount, rate_usd, rate_source, usd, size_band, counterparty, counterparty_label, tx_hash, log_index, class_coarse, class_fine",
         update_cadence="rewritten in full every refresh", expected_cadence_minutes=15,
         provenance="refresh.py, from transfers/ + transfers_in/ priced at the day-pinned rate",
         not_included="no cognition_in rows and no SWARM-era rows; counterparty_label is blank for unlabeled wallets"),
    dict(name="day_rates", path=["day_rates.json"], kind="oracle", live=True,
         measure=lambda: _day_rate_stamps("day_rates.json"),
         row_schema="day_rates[symbol][YYYY-MM-DD] -> USD rate; plus last_accepted_rate, recon, pending_rate, open_day_rate, market_rates",
         update_cadence="one immutable entry per token per day", expected_cadence_minutes=1440,
         provenance="day-implied payout rate from the $0.10 invoke cluster, anchored by the live Blockscout exchange rate; closed days are never recomputed",
         not_included="no intraday rates and no pre-crawl days — a day with no payouts gets no entry and is carried forward/back by classify.pin_rate"),
    dict(name="swarm_era", path=["swarm_era.json", "swarm_prices.json"], kind="archive", live=False,
         measure=lambda: _swarm_stamps("swarm_era.json"),
         row_schema="in[]/out[]: ts, val, cp (counterparty), tx, li; swarm_prices.json: YYYY-MM-DD -> USD",
         update_cadence="static archive — frozen", expected_cadence_minutes=None,
         provenance="one-time crawl of the generation-1 SWARM treasury hub, priced with CoinGecko daily closes",
         not_included="pre-migration SWARM only; not reconciled against the MOCA/MENTE ledger and not part of any economy figure"),
    dict(name="data", path=["data.json"], kind="derived", live=True,
         measure=lambda: _data_stamps("data.json"),
         row_schema="schema_version, scope, facts, infer, server, stripe_snap, insights, open_items, gaps, registry, sink",
         update_cadence="every refresh (~4x/hour)", expected_cadence_minutes=15,
         source_generated=lambda: _data_generated("data.json"),
         provenance="refresh.py — the versioned contract; a strict subset of what index.html embeds",
         not_included="no per-wallet detector signals (those stay in guard_private.json) and no raw transfer rows"),
    dict(name="stats_history", path=["stats_history.json"], kind="derived", live=True,
         measure=lambda: _hist_stamps("stats_history.json"),
         row_schema="ts, recon, invoke, equip, growth, moca, creators, rate, balance, mente_balance, runway7, runway_adj",
         update_cadence="one append per refresh (~4x/hour)", expected_cadence_minutes=15,
         provenance="refresh.py append-only snapshot; git history of the hourly commits is the immutable trail",
         not_included="counts and balances only — no per-wallet or per-transaction detail"),
]

PRIVATE = [
    dict(name="guard_private.json", kind="derived",
         note="private — see companion doc"),
    dict(name="alert_state.json", kind="derived",
         note="private — see companion doc"),
]


def build_entries():
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = []
    for d in PUBLIC:
        stamps = d["measure"]()
        rows, rows_closed, lo, hi = _measured([s for s in stamps if s])
        out.append({
            "name": d["name"], "path": d["path"], "public": True, "kind": d["kind"],
            "row_schema": d["row_schema"],
            "rows": 1 if d["name"] == "data" else rows,
            "rows_closed": 1 if d["name"] == "data" else rows_closed,
            "bytes": sum(_size(p) for p in d["path"]),
            "coverage": {"from": lo, "to": hi},
            "generated_iso": gen, "max_row_ts": hi,
            "expected_cadence_minutes": d["expected_cadence_minutes"],
            "update_cadence": d["update_cadence"],
            "provenance": d["provenance"], "not_included": d["not_included"],
            "live": d["live"],
        })
        # Where the SOURCE file carries its own build stamp, record it: the
        # shared `generated_iso` above is only when catalog.py ran.
        if d.get("source_generated"):
            out[-1]["source_generated_iso"] = d["source_generated"]()
    for d in PRIVATE:
        # NAME ONLY. No schema, no path beyond the repo-relative name, no
        # coverage — and no BYTES: guard_private.json's size tracks the number
        # of flagged wallets, so publishing it every refresh is a live
        # calibration oracle of exactly the class this section withholds.
        out.append({"name": d["name"], "public": False, "kind": d["kind"],
                    "note": d["note"]})
    return out


# ---------------------------- DATASETS.md ----------------------------

def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _peer_catalog():
    """moca-ledger's catalog, fetched at build time. The two repos' CIs stay
    independent: any failure degrades DATASETS.md to a one-line note, it never
    fails a refresh."""
    import urllib.request
    with urllib.request.urlopen(PEER_CATALOG_URL, timeout=20) as r:
        return json.load(r)


def render_md(entries, peer=None, peer_error=None):
    pub = [e for e in entries if e.get("public")]
    priv = [e for e in entries if not e.get("public")]
    total_rows = sum(e["rows"] for e in pub)
    total_bytes = sum(e["bytes"] for e in pub)
    L = ["# Data bank",
         "",
         "Generated by `catalog.py` on every refresh — **do not edit by hand**.",
         "Rows, bytes and coverage are measured off the files, never asserted.",
         "",
         f"**Data bank: {total_rows:,} rows · {_human(total_bytes)} across {len(pub)} datasets**",
         ""]
    for kind in KINDS:
        ks = [e for e in pub if e["kind"] == kind]
        if not ks:
            continue
        L += [f"## {kind}", "",
              "| Dataset | Path | Rows | Size | Coverage | Cadence |",
              "|---|---|---:|---:|---|---|"]
        for e in ks:
            cov = f"{(e['coverage']['from'] or '?')[:10]} → {(e['coverage']['to'] or '?')[:10]}"
            L.append(f"| `{e['name']}` | {' · '.join('`%s`' % p for p in e['path'])} | "
                     f"{e['rows']:,} | {_human(e['bytes'])} | {cov} | {e['update_cadence']} |")
        L.append("")
        for e in ks:
            L += [f"**`{e['name']}`** — {e['row_schema']}", "",
                  f"- provenance: {e['provenance']}",
                  f"- not included: {e['not_included']}", ""]
    L += ["## private (not published)", "",
          "Listed so the absence is deliberate and visible. No schema, no path,",
          "no coverage and no size: those are calibration hints for the detectors", "they feed.", "",
          "| Dataset | Kind | Size | Note |", "|---|---|---:|---|"]
    for e in priv:
        # Size is an em dash on purpose — see build_entries().
        L.append(f"| `{e['name']}` | {e['kind']} | — | {e['note']} |")
    L += ["", "## peer repo — Agentic-Po/moca-ledger", ""]
    if peer:
        pp = [e for e in peer if e.get("public")]
        L += [f"Fetched from `catalog.json` at build time: "
              f"**{sum(e['rows'] for e in pp):,} rows · {_human(sum(e['bytes'] for e in pp))} "
              f"across {len(pp)} datasets**.", "",
              "| Dataset | Rows | Size | Coverage |", "|---|---:|---:|---|"]
        for e in pp:
            cov = f"{(e['coverage']['from'] or '?')[:10]} → {(e['coverage']['to'] or '?')[:10]}"
            L.append(f"| `{e['name']}` | {e['rows']:,} | {_human(e['bytes'])} | {cov} |")
        L.append("")
    else:
        L += [f"_peer catalog unavailable this run_ ({peer_error or 'not fetched'}).", ""]
    return "\n".join(L) + "\n"


def build(fetch_peer=True):
    """Write catalog.json + DATASETS.md. Called by refresh.py after the data
    files are written. Only DATASETS.md carries the peer numbers — catalog.json
    stays purely local so --check needs no network and stays deterministic."""
    entries = build_entries()
    json.dump(entries, open(CATALOG, "w"), indent=1)
    peer, err = None, None
    if fetch_peer:
        try:
            peer = _peer_catalog()
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            # Actions log only: an exception string can carry a URL or an
            # auth hint, and DATASETS.md is a public artifact.
            print("peer catalog fetch failed:", err)
            err = "degraded: peer catalog not fetched this run"
    open(DATASETS_MD, "w").write(render_md(entries, peer, err))
    pub = [e for e in entries if e.get("public")]
    print(f"catalog: {len(pub)} public datasets, {sum(e['rows'] for e in pub):,} rows")
    return entries


# Fields the CURRENT month legitimately grows between two builds. Everything
# else must match byte-for-byte or --check fails.
DRIFTY = ("rows", "max_row_ts", "source_generated_iso")
# bytes is a display figure: re-serialising data.json can move it either way,
# so on live datasets it carries no integrity signal and is not compared.
# rows_closed (months before the current one) is the exact anchor instead.
SKIP_WHEN_LIVE = ("bytes",)


def check():
    if not os.path.exists(CATALOG):
        print("FAIL: catalog.json missing — run catalog.py first")
        return 1
    old = json.load(open(CATALOG))
    new = build_entries()
    bad = []
    if len(old) != len(new):
        bad.append(f"entry count {len(old)} -> {len(new)}")
    for o, n in zip(old, new):
        if o.get("name") != n.get("name"):
            bad.append(f"order/name drift: {o.get('name')} vs {n.get('name')}")
            continue
        live = n.get("live", False)
        for k in set(o) | set(n):
            if k == "generated_iso" or (live and k in SKIP_WHEN_LIVE):
                continue
            ov, nv = o.get(k), n.get(k)
            if live and k in DRIFTY:
                if ov is None or nv is None:
                    # A key present on one side only is a SCHEMA change (a
                    # field added to build_entries in a later release), not
                    # data moving backwards. Say which.
                    bad.append(f"{n['name']}.{k}: new/absent field ({ov!r} -> {nv!r}) "
                               f"— rebuild catalog.json after a schema change")
                elif nv < ov:
                    bad.append(f"{n['name']}.{k}: {ov} -> {nv} (went backwards)")
            elif live and k == "coverage":
                if (ov or {}).get("from") != (nv or {}).get("from"):
                    bad.append(f"{n['name']}.coverage.from: {ov} -> {nv}")
                if (nv or {}).get("to") is None or (nv or {}).get("to") < (ov or {}).get("to", ""):
                    bad.append(f"{n['name']}.coverage.to went backwards: {ov} -> {nv}")
            elif ov != nv:
                bad.append(f"{n['name']}.{k}: {ov!r} -> {nv!r}")
    for b in bad:
        print("catalog drift:", b)
    print("catalog --check:", "FAIL" if bad else "ok")
    return 1 if bad else 0


if __name__ == "__main__":
    if "--check" in sys.argv:
        raise SystemExit(check())
    build(fetch_peer="--no-peer" not in sys.argv)

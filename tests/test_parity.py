#!/usr/bin/env python3
"""The digest and the page must never quote different money.

CHOICE (spec offered two): the window sums are REIMPLEMENTED here from
classify.py + shards, NOT imported from notify.py. notify.py is a script —
importing it loads data.json, builds a message and, past the guards, POSTS TO
TELEGRAM. Refactoring it into functions to make it importable would touch the
send path for the sake of a test, which is the wrong trade: a test must not be
able to send a message. What is shared is what matters — classify.py's
taxonomy and pin_rate, the same two things notify.py calls.

The recompute is anchored at data.json's generated_iso, not at wall clock:
facts_window's 24h/7d cuts were taken at THAT instant, and a window boundary
that moves while the test runs is not a disagreement.

  python3 tests/test_parity.py     (no network, <30s)
"""
import json, os, re, sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import shards
from classify import classify_usd, pin_rate

TOL = 0.01

# Every exec-facing figure the README glossary must document, by the literal
# key/name the glossary is required to carry.
GLOSSARY_KEYS = ["economy_out_usd", "ops_out_usd", "out_usd", "usd_ce",
                 "usd_incent", "usd_topup", "wallet balance", "subsidy ratio",
                 "user-funded cognition lower bound", "distribution float"]


def _load():
    D = json.load(open(os.path.join(ROOT, "data.json")))
    dr = json.load(open(os.path.join(ROOT, "day_rates.json")))
    toks = {a.lower(): s for s, a in D["scope"]["tokens"].items()}
    rates = {sym: dict(dr["day_rates"].get(sym, {})) for sym in toks.values()}
    for sym, od in (dr.get("open_day_rate") or {}).items():
        rates.setdefault(sym, {}).setdefault(od["d"], od["rate"])
    rows = []
    for i in shards.load(os.path.join(ROOT, "transfers")):
        sym = toks.get(i["token"]["address_hash"].lower())
        if not sym:
            continue
        dec = int(i["total"].get("decimals") or 18)
        val = int(i["total"]["value"]) / 10 ** dec
        ts = i["timestamp"][:19]
        usd = val * pin_rate(rates.get(sym, {}), ts[:10], D["facts"]["rate"].get(sym) or 0)
        coarse, fine, _ = classify_usd(usd)
        rows.append({"ts": ts, "tok": sym, "usd": usd, "cat": coarse, "fine": fine})
    return D, rows


def _sums(rows, cut):
    rs = [r for r in rows if r["ts"] > cut]
    eco = sum(r["usd"] for r in rs if r["cat"] != "nonstandard")
    out = sum(r["usd"] for r in rs)
    return {"out_usd": out, "economy_out_usd": eco, "ops_out_usd": out - eco,
            "usd_ce": sum(r["usd"] for r in rs if r["cat"] in ("invoke", "equip")),
            "usd_incent": sum(r["usd"] for r in rs if r["cat"] == "growth"
                              and not r["fine"].startswith("stripe")),
            "usd_topup": sum(r["usd"] for r in rs if r["fine"].startswith("stripe")),
            "usd_micro": sum(r["usd"] for r in rs if r["cat"] == "micro")}


def main():
    D, rows = _load()
    # schema v2 = the identity-redacted contract. Every figure check below is
    # the pre/post-redaction parity proof: the sums recomputed from raw shards
    # must still match the published page to the cent, so redaction provably
    # touched labels only, never money.
    assert D.get("schema_version") == 2, \
        f"schema_version {D.get('schema_version')!r} != 2 — redaction contract not in force"
    print("ok schema_version == 2 (redacted contract)")
    gen = datetime.fromisoformat(D["scope"]["generated_iso"].replace("Z", ""))
    byw = {w["label"]: w for w in D["facts"]["windows"]}

    checked = 0
    for label, hours in (("24h", 24), ("7d", 24 * 7)):
        cut = (gen - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
        mine, page = _sums(rows, cut), byw[label]
        for k in ("out_usd", "economy_out_usd", "ops_out_usd"):
            d = abs(mine[k] - page[k])
            assert d <= TOL, f"{label} {k}: digest ${mine[k]:,.4f} vs page ${page[k]:,.4f} (Δ ${d:,.4f})"
            print(f"ok {label} {k}: ${page[k]:,.2f}")
            checked += 1
        # The three digest money lines must close on economy_out_usd exactly —
        # this is the check that would have caught the forked classifier.
        parts = mine["usd_ce"] + mine["usd_incent"] + mine["usd_topup"] + mine["usd_micro"]
        d = abs(parts - page["economy_out_usd"])
        assert d <= TOL, (f"{label}: digest lines (creators ${mine['usd_ce']:,.2f} + incentive "
                          f"${mine['usd_incent']:,.2f} + top-ups ${mine['usd_topup']:,.2f} + micro "
                          f"${mine['usd_micro']:,.2f}) = ${parts:,.4f} != economy ${page['economy_out_usd']:,.4f}")
        print(f"ok {label} digest lines close on economy_out_usd (${parts:,.2f})")
        checked += 1

    # The glossary is part of the contract: a figure nobody documented is a
    # figure an exec will misquote.
    readme = open(os.path.join(ROOT, "README.md")).read()
    m = re.search(r"^## Figures glossary$(.*?)(?=^## |\Z)", readme, re.M | re.S)
    assert m, "README.md has no '## Figures glossary' section"
    section = m.group(1)
    missing = [k for k in GLOSSARY_KEYS if k not in section]
    assert not missing, f"glossary is missing: {missing}"
    print(f"ok glossary documents all {len(GLOSSARY_KEYS)} declared figures")

    print(f"test_parity: PASS ({checked} figure checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

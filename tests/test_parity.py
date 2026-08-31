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


def _exec_sentence_checks(D):
    """Executive-summary parity (Cycle-3 Loop 3, item 1): the plain-English
    sentences embedded in the BUILT index.html must quote facts_window /
    balance values to the cent. The text is extracted from index.html itself
    (the page is the artifact execs read), cross-checked against data.json's
    copy, then each $ figure is parsed back out and compared."""
    html = open(os.path.join(ROOT, "index.html"), errors="replace").read()
    m = re.search(r'"exec_summary":\s*\{[^{}]*?"text":\s*"((?:[^"\\]|\\.)*)"', html)
    assert m, "index.html embeds no exec_summary text — rebuild the page"
    text = json.loads('"' + m.group(1) + '"')
    ex = D.get("exec_summary") or {}
    assert ex.get("text") == text, \
        f"exec text drifted between index.html and data.json:\n  page {text!r}\n  data {ex.get('text')!r}"

    def num(pat):
        mm = re.search(pat, text)
        assert mm, f"exec sentence pattern {pat!r} missing from: {text!r}"
        return float(mm.group(1).replace(",", ""))

    w7 = {w["label"]: w for w in D["facts"]["windows"]}["7d"]
    checked = 0
    paid = num(r"treasury paid \$([\d,]+\.\d{2}) to creators and users")
    assert abs(paid - w7["economy_out_usd"]) <= TOL, \
        f"exec 'paid' ${paid:,.2f} != 7d economy_out_usd ${w7['economy_out_usd']:,.2f}"
    print(f"ok exec sentence: paid ${paid:,.2f} == 7d economy_out_usd")
    checked += 1
    ntx = int(num(r"across ([\d,]+) transfers"))
    assert ntx == w7["out_tx"], f"exec transfer count {ntx} != 7d out_tx {w7['out_tx']}"
    print(f"ok exec sentence: {ntx:,} transfers == 7d out_tx")
    checked += 1
    if w7["ops_out_usd"]:
        ops = num(r"\$([\d,]+\.\d{2}) more went to treasury operations")
        assert abs(ops - w7["ops_out_usd"]) <= TOL, \
            f"exec 'ops' ${ops:,.2f} != 7d ops_out_usd ${w7['ops_out_usd']:,.2f}"
        print(f"ok exec sentence: ops ${ops:,.2f} == 7d ops_out_usd")
        checked += 1
    bal = num(r"wallet holds \$([\d,]+\.\d{2})")
    want = sum(v for v in D["facts"]["balance_usd"].values() if v)
    assert abs(bal - want) <= TOL, \
        f"exec 'holds' ${bal:,.2f} != published balance_usd sum ${want:,.2f}"
    print(f"ok exec sentence: holds ${bal:,.2f} == balance_usd sum")
    checked += 1
    # "about W weeks of payouts" must be denominated by PAYOUT pace
    # (economy_out_usd), never total outflow — a swap-heavy week must not
    # read as a near-empty wallet (Cycle-3 Loop-3 QA defect D1/D2)
    m = re.search(r"about ([\d,.]+) weeks? of payouts", text)
    econ7 = D["facts"]["windows"][1]["economy_out_usd"]
    if m and econ7 > 0:
        got = float(m.group(1).replace(",", ""))
        assert abs(got - want / econ7) < 0.05 + 1e-9, \
            f"exec weeks {got} != balance/economy pace {want / econ7:.2f}"
        print(f"ok exec sentence: {got} weeks == balance / 7d economy pace")
        checked += 1
    # degraded prefix must agree with the flag the pipeline recorded
    has_prefix = bool(re.search(r"^Data is \d+ hours old — figures may lag\.", text))
    assert has_prefix == bool(ex.get("degraded")), \
        f"degraded prefix present={has_prefix} but exec_summary.degraded={ex.get('degraded')}"
    print(f"ok exec degraded prefix consistent (degraded={bool(ex.get('degraded'))})")
    checked += 1
    return checked


def _coupon_checks():
    """Coupon page parity. Same discipline as the treasury half: the totals
    are RECOMPUTED here from coupon_out/ + classify.pin_rate — never imported
    from refresh.py — and the summary sentence is read out of the BUILT
    coupon.html, so a page whose text drifted from its own data block fails.

    pin_rate is called on day_rates ALONE, with no open_day_rate leg: that is
    exactly how refresh.py prices claims, and a test that priced the open day
    differently would disagree with the page every day before midnight.
    """
    C = json.load(open(os.path.join(ROOT, "coupon_data.json")))
    dr = json.load(open(os.path.join(ROOT, "day_rates.json")))
    rates = dict(dr["day_rates"].get("MOCA", {}))
    moca = C["scope"]["token"]["MOCA"].lower()
    n = usd = qty = 0.0
    wallets = set()
    for i in shards.load(os.path.join(ROOT, "coupon_out")):
        if i["token"]["address_hash"].lower() != moca:
            continue
        val = int(i["total"]["value"]) / 10 ** int(i["total"].get("decimals") or 18)
        n += 1
        qty += val
        usd += val * pin_rate(rates, i["timestamp"][:10], C["scope"]["rate"])
        wallets.add(i["to"]["hash"].lower())
    T = C["totals"]
    assert int(n) == T["claims"], f"coupon claims {int(n)} != published {T['claims']}"
    assert len(wallets) == T["claimants"], \
        f"coupon claimants {len(wallets)} != published {T['claimants']}"
    assert abs(qty - T["moca_out"]) <= 1e-4, \
        f"coupon MOCA out {qty:,.4f} != published {T['moca_out']:,.4f}"
    assert abs(usd - T["usd_out"]) <= TOL, \
        f"coupon USD out ${usd:,.4f} != published ${T['usd_out']:,.4f}"
    print(f"ok coupon totals recomputed from shards: {T['claims']:,} claims · "
          f"{T['moca_out']:,.0f} MOCA · ${T['usd_out']:,.2f} · {T['claimants']:,} wallets")
    checked = 4

    html = open(os.path.join(ROOT, "coupon.html"), errors="replace").read()
    m = re.search(r'"summary":\s*\{\s*"text":\s*"((?:[^"\\]|\\.)*)"', html)
    assert m, "coupon.html embeds no summary text — rebuild the page"
    text = json.loads('"' + m.group(1) + '"')
    assert text == C["summary"]["text"], \
        f"coupon summary drifted between coupon.html and coupon_data.json:\n  page {text!r}\n  data {C['summary']['text']!r}"

    def num(pat):
        mm = re.search(pat, text)
        assert mm, f"coupon sentence pattern {pat!r} missing from: {text!r}"
        return float(mm.group(1).replace(",", ""))

    # tolerance per figure: USD is rendered to the cent, MOCA and the counts
    # are rendered with no decimals, so a whole unit of rounding is expected.
    for pat, want, tol, name in (
            (r"distributed ([\d,]+) MOCA", T["moca_out"], 1.0, "MOCA distributed"),
            (r"MOCA \(≈\$([\d,]+\.\d{2})\)", T["usd_out"], TOL, "USD distributed"),
            (r"across ([\d,]+) claims", T["claims"], 0, "claims"),
            (r"to ([\d,]+) unique wallets", T["claimants"], 0, "unique wallets")):
        got = num(pat)
        assert abs(got - want) <= tol, \
            f"coupon sentence {name} {got:,.2f} != coupon_data {want:,.2f}"
        print(f"ok coupon sentence: {name} {got:,.2f} == coupon_data")
        checked += 1
    assert f"since its funding on {C['scope']['genesis_day']}" in text, \
        f"coupon sentence does not state the genesis day {C['scope']['genesis_day']}"
    checked += 1
    if T["balance_moca"] is not None:
        held = num(r"It holds ([\d,]+) MOCA")
        assert abs(held - T["balance_moca"]) <= 1.0, \
            f"coupon 'holds' {held:,.0f} MOCA != balance_moca {T['balance_moca']:,.0f}"
        print(f"ok coupon sentence: holds {held:,.0f} MOCA == balance_moca")
        checked += 1
        # weeks are the balance divided by the 7d CLAIM pace, and the clause is
        # ABSENT when there were no claims in 7 days — never a guessed number.
        mw = re.search(r"about ([\d,.]+) weeks? of claims", text)
        assert bool(mw) == bool(T["weeks_left"]), \
            f"weeks clause present={bool(mw)} but weeks_left={T['weeks_left']}"
        if mw:
            got = float(mw.group(1).replace(",", ""))
            assert abs(got - T["balance_moca"] / T["moca_7d"]) < 0.05 + 1e-9, \
                f"coupon weeks {got} != balance / 7d claim pace"
            print(f"ok coupon sentence: {got} weeks == balance / 7d claim pace")
            checked += 1
    has_prefix = bool(re.search(r"^Data is \d+ hours old — figures may lag\.", text))
    assert has_prefix == bool(C["summary"].get("degraded")), \
        f"coupon degraded prefix present={has_prefix} but degraded={C['summary'].get('degraded')}"
    print(f"ok coupon degraded prefix consistent (degraded={bool(C['summary'].get('degraded'))})")
    checked += 1
    # concentration is an aggregate of the same ranking the table publishes
    top5 = round(sum(t["moca"] for t in C["top"][:5]) / T["moca_out"] * 100, 1)
    assert abs(top5 - C["concentration"]["top5_pct"]) <= 0.1, \
        f"top-5 concentration {C['concentration']['top5_pct']}% != recomputed {top5}%"
    print(f"ok coupon concentration: top 5 hold {C['concentration']['top5_pct']}% of claimed MOCA")
    checked += 1
    return checked


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

    checked += _exec_sentence_checks(D)
    checked += _coupon_checks()

    print(f"test_parity: PASS ({checked} figure checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
from classify import classify_usd, pin_rate, group_for, GROUP_KEYS

TOL = 0.01

# Every exec-facing figure the README glossary must document, by the literal
# key/name the glossary is required to carry. (2026-09-15: subsidy ratio,
# user-funded lower bound and implied user spend are GONE — Po rule 5 — and
# must not come back; the groups, float and creator_wallets are new.)
GLOSSARY_KEYS = ["economy_out_usd", "ops_out_usd", "out_usd", "groups",
                 "skill_rewards", "credit_grants", "system_topups", "topups_delivered",
                 "wallet balance", "distribution float", "facts.float",
                 "rewards_v2", "$1 system free top-up", "creator_wallets"]
GLOSSARY_GONE = ["subsidy ratio", "user-funded", "implied_user_spend", "top-up (legacy)"]


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
        coarse, fine, _ = classify_usd(usd, ts)
        rows.append({"ts": ts, "tok": sym, "usd": usd, "cat": coarse, "fine": fine,
                     "grp": group_for(coarse, fine)})
    return D, rows


def _one_classifier_guard():
    """No file other than classify.py may know a reward size, and every
    classify_usd()/band() call outside it must pass the row timestamp
    (Creator Rewards v2: a day-blind copy of the grid is the failure that
    priced 2026-09-14 at 2x). Root *.py only — tests/ legitimately carry
    literals (test_eras.py) and one-argument fixtures."""
    import glob
    bad = []
    lits = re.compile(r"< 0\.06|0\.05,|0\.005|(?<![\w.])_snap\(")
    for p in sorted(glob.glob(os.path.join(ROOT, "*.py"))):
        name = os.path.basename(p)
        if name == "classify.py":
            continue
        src = open(p).read()
        for m in lits.finditer(src):
            line = src[:m.start()].count("\n") + 1
            bad.append(f"{name}:{line}: reward-size literal {m.group(0)!r} outside classify.py")
        for m in re.finditer(r"(?<![\w.])(classify_usd|band)\(([^()]*(?:\([^()]*\)[^()]*)*)\)", src):
            args = [a for a in m.group(2).split(",") if a.strip()]
            if len(args) != 2:
                line = src[:m.start()].count("\n") + 1
                bad.append(f"{name}:{line}: {m.group(1)}() called with {len(args)} arg(s), want (usd, ts)")
    assert not bad, "one-classifier guard:\n  " + "\n  ".join(bad)
    print("ok one-classifier guard: no reward-size literal and no day-blind classify_usd()/band() call outside classify.py")
    return 1


def _sums(rows, cut):
    """Window sums through the ONE grouping layer (classify.group_for) — the
    digest, the page tiles and the exec summary all bucket rows this way, so
    a category hand-listed anywhere else is the drift this test exists to catch."""
    rs = [r for r in rows if r["ts"] > cut]
    eco = sum(r["usd"] for r in rs if r["cat"] != "nonstandard")
    out = sum(r["usd"] for r in rs)
    g = {k: sum(r["usd"] for r in rs if r["grp"] == k) for k in GROUP_KEYS}
    gn = {k: sum(1 for r in rs if r["grp"] == k) for k in GROUP_KEYS}
    return {"out_usd": out, "economy_out_usd": eco, "ops_out_usd": out - eco,
            "groups": g, "groups_n": gn}


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
    left = num(r"In the last 7 days \$([\d,]+\.\d{2}) left the treasury")
    assert abs(left - w7["out_usd"]) <= TOL, \
        f"exec 'left' ${left:,.2f} != 7d out_usd ${w7['out_usd']:,.2f}"
    print(f"ok exec sentence: left ${left:,.2f} == 7d out_usd")
    checked += 1
    ntx = int(num(r"across ([\d,]+) transfers"))
    assert ntx == w7["out_tx"], f"exec transfer count {ntx} != 7d out_tx {w7['out_tx']}"
    print(f"ok exec sentence: {ntx:,} transfers == 7d out_tx")
    checked += 1
    # X2: every group figure in the sentence is a facts_window group sum
    for pat, key in ((r"\$([\d,]+\.\d{2}) skill rewards to", "skill_rewards"),
                     (r"\$([\d,]+\.\d{2}) credit grants", "credit_grants"),
                     (r"\$([\d,]+\.\d{2}) system free top-ups", "system_topups"),
                     (r"\$([\d,]+\.\d{2}) top-ups delivered", "topups_delivered"),
                     (r"\$([\d,]+\.\d{2}) ops", "ops")):
        got = num(pat)
        assert abs(got - w7["groups"][key]["usd"]) <= TOL, \
            f"exec {key} ${got:,.2f} != 7d group sum ${w7['groups'][key]['usd']:,.2f}"
        checked += 1
    ncw = int(num(r"skill rewards to ([\d,]+) creator wallets"))
    assert ncw == w7["groups"]["skill_rewards"]["wallets"], \
        f"exec creator wallets {ncw} != 7d skill_rewards wallets {w7['groups']['skill_rewards']['wallets']}"
    assert "creators" not in text.replace("creator wallets", ""), "exec sentence says 'creators'"
    print("ok exec sentence: five group figures + creator-wallet count == 7d group sums")
    checked += 1
    bal = num(r"wallet holds \$([\d,]+\.\d{2})")
    want = sum(v for v in D["facts"]["balance_usd"].values() if v)
    assert abs(bal - want) <= TOL, \
        f"exec 'holds' ${bal:,.2f} != published balance_usd sum ${want:,.2f}"
    print(f"ok exec sentence: holds ${bal:,.2f} == balance_usd sum")
    checked += 1
    # X2: ONE runway, on TOTAL outflow (facts.float), never economy pace
    fl = D["facts"]["float"]
    m = re.search(r"about ([\d,.]+) days at the 7-day pace", text)
    assert bool(m) == bool(fl.get("days_7d_pace")), f"days clause present={bool(m)} but float.days_7d_pace={fl.get('days_7d_pace')}"
    if m:
        got = float(m.group(1).replace(",", ""))
        assert abs(got - fl["days_7d_pace"]) < 0.05 + 1e-9, f"exec days {got} != float.days_7d_pace {fl['days_7d_pace']}"
        # The published pace is round(bal_usd / out7avg, 1), whose own error is
        # at most 0.05 — so a `< 0.05` bound could never fail on rounding alone.
        # What tips it over is that we divide a DIFFERENT numerator than the
        # producer: refresh.py uses raw bal_usd, we use the sum of published
        # facts.balance_usd, each token already round(x, 0). That adds up to
        # $0.50/token, worst case ~0.0502 — over the bound in ~0.08% of runs,
        # about one false "refresh FAILED" page a month (it fired 2026-09-20
        # 20:37Z at 9.45; the next run on the SAME commit passed). Bound the
        # numerator gap structurally instead of rounding the published value to
        # make the check close (see the 2026-09-18 group-rounding note).
        bal_slack = 0.5 * len(D["facts"]["balance_usd"]) / fl["out_7d_avg_usd"]
        assert abs(fl["days_7d_pace"] - want / fl["out_7d_avg_usd"]) <= 0.05 + bal_slack + 1e-9, \
            (f"float.days_7d_pace {fl['days_7d_pace']} != balance ${want:,.2f} / "
             f"7d pace ${fl['out_7d_avg_usd']:,.2f} (= {want / fl['out_7d_avg_usd']:.4f}, "
             f"slack {0.05 + bal_slack:.4f})")
        print(f"ok exec sentence: {got} days == facts.float.days_7d_pace == balance / 7d total outflow")
        checked += 1
    assert "weeks of payouts" not in text and "week" not in text, "old economy-pace runway sentence is back"
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
    assert D.get("schema_version") == 4, \
        f"schema_version {D.get('schema_version')!r} != 4 — 2026-09-18 contract not in force"
    print("ok schema_version == 3 (grouped, status-free contract)")
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
        # A2: every published group sum equals the group_for() recompute, the
        # six groups close on out_usd, and the non-ops groups on economy —
        # this is the check that would have caught a forked classifier OR a
        # hand-listed category list drifting from group_for.
        for g in GROUP_KEYS:
            d = abs(mine["groups"][g] - page["groups"][g]["usd"])
            assert d <= TOL, f"{label} group {g}: recompute ${mine['groups'][g]:,.4f} vs page ${page['groups'][g]['usd']:,.4f}"
            assert mine["groups_n"][g] == page["groups"][g]["n"], f"{label} group {g}: n {mine['groups_n'][g]} != {page['groups'][g]['n']}"
        parts = sum(mine["groups"].values())
        d = abs(parts - page["out_usd"])
        assert d <= TOL, f"{label}: group sums ${parts:,.4f} != out_usd ${page['out_usd']:,.4f}"
        eco_parts = sum(v for k, v in mine["groups"].items() if k != "ops")
        assert abs(eco_parts - page["economy_out_usd"]) <= TOL, \
            f"{label}: non-ops groups ${eco_parts:,.4f} != economy ${page['economy_out_usd']:,.4f}"
        print(f"ok {label} six group_for() sums close on out_usd (${parts:,.2f}) and on economy (${eco_parts:,.2f})")
        checked += 1
    # A1: the creator-wallet card recomputes to the cent per window
    cw = D["facts"]["creator_wallets"]["windows"]
    rw = [r for r in rows if r["grp"] == "skill_rewards"]
    for label, lo in (("24h", (gen - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")),
                      ("7d", (gen - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")),
                      ("since_resume", "2026-09-14T14:18:59"), ("all", "0")):
        rs = [r for r in rw if r["ts"] > lo]
        usd_w = sum(r["usd"] for r in rs)
        assert abs(usd_w - cw[label]["usd"]) <= TOL, f"creator_wallets[{label}].usd ${cw[label]['usd']:,.2f} != recompute ${usd_w:,.2f}"
        assert cw[label]["equips"] == sum(1 for r in rs if r["cat"] == "equip")
        assert cw[label]["invokes"] == sum(1 for r in rs if r["cat"] == "invoke")
        checked += 1
    print("ok creator_wallets: 24h / 7d / since_resume / all recompute to the cent")

    # The glossary is part of the contract: a figure nobody documented is a
    # figure an exec will misquote.
    readme = open(os.path.join(ROOT, "README.md")).read()
    m = re.search(r"^## Figures glossary$(.*?)(?=^## |\Z)", readme, re.M | re.S)
    assert m, "README.md has no '## Figures glossary' section"
    section = m.group(1)
    missing = [k for k in GLOSSARY_KEYS if k not in section]
    assert not missing, f"glossary is missing: {missing}"
    back = [k for k in GLOSSARY_GONE if k in section]
    assert not back, f"glossary re-documents a retired paid-vs-free figure: {back}"
    print(f"ok glossary documents all {len(GLOSSARY_KEYS)} declared figures, none of the {len(GLOSSARY_GONE)} retired ones")

    checked += _exec_sentence_checks(D)
    checked += _coupon_checks()
    checked += _one_classifier_guard()

    print(f"test_parity: PASS ({checked} figure checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

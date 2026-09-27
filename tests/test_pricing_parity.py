#!/usr/bin/env python3
"""Pricing parity — five loaders, one USD per row.

Classification has one home (classify.py); PRICING does not. A row's USD is
computed independently in five places:

  refresh   refresh.py day_rate()      (exact day -> open_day_rate -> carry-
                                        forward -> carry-back -> live)
  notify    notify.py _DAY_RATES + classify() + its row loop
  alerts    alerts.py _day_rates + usd_rows()  (index 6, day-pinned)
  sanity    sanity.load_raw()
  digests   digests.rows_from_disk()   (digests._pin, a copy of classify.pin_rate)

None of refresh / notify / alerts is importable (module-level crawl, freshness
gates, Telegram), so their pricing code is extracted by AST and executed
against the inputs below — the real statements, not a re-implementation, EXCEPT the
refresh.py leg's row quantity (val = int/10**dec), which this test rebuilds itself
(only day_rate is extracted from refresh.py) and its inflow rows (compared for
alerts only) — documented as a blind spot in README (QA loop 3).
sanity and digests take a root directory and are called directly.

Inputs: (1) the REAL tree — every tracked-token row in transfers/ and
transfers_in/, day_rates.json and data.json's live rate; (2) SYNTHETIC
fixtures covering carry-back, exact, gap carry-forward, the open (today) day,
a day after the open day, and a token with no closed rates at all.

Every loader must give the byte-identical float USD for every row. A known
divergence is NOT fixed here (pricing is an owner decision) — it is listed in
ALLOW with a one-line reason; an unlisted divergence fails, and so does a
listed one that stopped happening (the allow-list must not rot).
"""
import ast, json, math, os, shutil, sys, tempfile
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import shards                                   # noqa: E402
from classify import pin_rate, classify_usd, group_for   # noqa: E402
import sanity, digests                          # noqa: E402

MOCA = "0x2b11834ed1feaed4b4b3a86a6f571315e25a884d"
MENTE = "0x4cd9a847f39106e19a4e41aea8a232e915c82af5"

# (case, loader, day) -> reason; day "*" covers every day of that case.
# Synthetic-only entries: on the real tree today every loader agrees with
# refresh.day_rate on the RATE for every row (see ULP_ALLOW for the one
# real-tree difference, which is in the token quantity, not the rate).
ALLOW = {
    ("after_open_day", "notify", "2026-09-28"):
        "day AFTER open_day_rate.d: pin_rate loaders merge the open-day rate into the closed-day dict and carry it "
        "forward; refresh.day_rate carries the last CLOSED day. Unreachable while od.d == today.",
    ("after_open_day", "alerts", "2026-09-28"): "same as notify (open-day rate merged, then carried forward)",
    ("after_open_day", "sanity", "2026-09-28"): "same as notify (open-day rate merged, then carried forward)",
    ("after_open_day", "digests", "2026-09-28"): "same as notify (open-day rate merged, then carried forward)",
    ("no_closed_rates", "notify", "*"):
        "token with NO closed day rates: pin_rate loaders carry the open-day rate to every other day; "
        "refresh.day_rate uses the live rate. Bootstrap-only (both tokens have closed days since April).",
    ("no_closed_rates", "alerts", "*"): "same as notify (open-day rate carried to every day; refresh uses live)",
    ("no_closed_rates", "sanity", "*"): "same as notify (open-day rate carried to every day; refresh uses live)",
    ("no_closed_rates", "digests", "*"): "same as notify (open-day rate carried to every day; refresh uses live)",
}
# loader -> reason: differences of at most ULP_MAX units-in-the-last-place of
# the USD are tolerated (counted and printed, never silent). Anything larger
# fails. The entry must keep matching at least one real row, or it has rotted.
ULP_MAX = 2
ULP_ALLOW = {
    "notify": "qty = int(wei) / 1e18 (int->float, then divide: double rounding) vs refresh's int / 10**18 "
              "(correctly rounded): 1 ulp in qty on 20,077 of 150,526 out rows today, 18,645 of which survive into USD (≤2 ulp, ~1e-16 relative)",
    "alerts": "same int / 1e18 quantity as notify (usd_rows, out and in legs)",
}


# ------------------------------------------------------------ AST extraction
def _module(path):
    src = open(os.path.join(ROOT, path)).read()
    return src, ast.parse(src)


def _seg(src, node):
    return ast.get_source_segment(src, node)


def _assigns(tree, names):
    return [n for n in tree.body if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id in names for t in n.targets)]


def _func(tree, name):
    fs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(fs) == 1, f"expected one def {name}, found {len(fs)}"
    return fs[0]


def _for_over(src, tree, needle):
    fs = [n for n in tree.body if isinstance(n, ast.For) and needle in _seg(src, n.iter)]
    assert len(fs) == 1, f"expected one top-level for-loop over {needle!r}, found {len(fs)}"
    return fs[0]


def _base_ns(root):
    D = json.load(open(os.path.join(root, "data.json")))
    return D, {"json": json, "os": os, "HERE": root, "shards": shards, "datetime": datetime,
               "pin_rate": pin_rate, "classify_usd": classify_usd, "group_for": group_for,
               "TOKENS": {a.lower(): s for s, a in D["scope"]["tokens"].items()}}


# ------------------------------------------------------------------ loaders
# each returns {"out": [(sym, ts, usd)], "in": [(sym, ts, usd)] or None}
def load_refresh(root):
    src, tree = _module("refresh.py")
    D, _ = _base_ns(root)
    ns = {"STATE": json.load(open(os.path.join(root, "day_rates.json"))),
          "RATE": dict(D["facts"]["rate"])}
    exec(_seg(src, _func(tree, "day_rate")), ns)
    toks = {a.lower(): s for s, a in D["scope"]["tokens"].items()}

    def price(d):
        out = []
        for i in shards.load(os.path.join(root, d)):
            sym = toks.get(i["token"]["address_hash"].lower())
            if not sym:
                continue
            # refresh.py's row build: decimals or DECIMALS[sym] (18 for both)
            val = int(i["total"]["value"]) / 10 ** int(i["total"].get("decimals") or 18)
            ts = i["timestamp"][:19]
            out.append((sym, ts, val * ns["day_rate"](sym, ts)[0]))
        return out
    return {"out": price("transfers"), "in": price("transfers_in")}


def load_notify(root):
    src, tree = _module("notify.py")
    D, ns = _base_ns(root)
    ns["F"] = D["facts"]
    ns["rows"] = []
    code = [_seg(src, n) for n in _assigns(tree, {"_dr_state", "_DAY_RATES"})]
    code.append(_seg(src, _for_over(src, tree, "open_day_rate")))
    code.append(_seg(src, _func(tree, "classify")))
    code.append(_seg(src, _for_over(src, tree, '"transfers"')))
    for c in code:
        exec(c, ns)
    # row tuple: (ts, cat, qty, wallet, tier, usd_day_pinned, sym, fine, group)
    return {"out": [(r[6], r[0].isoformat(), r[5]) for r in ns["rows"]], "in": None}


def load_alerts(root):
    src, tree = _module("alerts.py")
    D, ns = _base_ns(root)
    ns["RATE"] = D["facts"]["rate"]
    code = [_seg(src, n) for n in _assigns(tree, {"_dr_state", "_day_rates"})]
    code.append(_seg(src, _for_over(src, tree, "open_day_rate")))
    code.append(_seg(src, _func(tree, "usd_rows")))
    for c in code:
        exec(c, ns)
    conv = lambda rs: [(r[2], r[7], r[6]) for r in rs]
    return {"out": conv(ns["usd_rows"]("transfers", "to")),
            "in": conv(ns["usd_rows"]("transfers_in", "from"))}


def load_sanity(root):
    D, dr, rows, ins, syms = sanity.load_raw(root)
    return {"out": [(r["tok"], r["ts"], r["usd"]) for r in rows], "in": None}


def load_digests(root):
    rows, _today = digests.rows_from_disk(root)
    return {"out": [(r["tok"], r["ts"], r["usd"]) for r in rows], "in": None}


LOADERS = {"refresh": load_refresh, "notify": load_notify, "alerts": load_alerts,
           "sanity": load_sanity, "digests": load_digests}


# ----------------------------------------------------------------- fixtures
def _row(tok, ts, qty, cp="0x" + "ab" * 20, n=[0]):
    n[0] += 1
    wei = str(int(round(qty * 1e6)) * 10 ** 12)
    return {"timestamp": ts + ".000000Z", "token": {"address_hash": tok},
            "total": {"value": wei, "decimals": "18" if n[0] % 2 else None},
            "to": {"hash": cp}, "from": {"hash": cp},
            "transaction_hash": "0x%064x" % n[0], "log_index": n[0]}


def make_fixture(tmp, name, day_rates, open_day, live, out_rows, in_rows):
    root = os.path.join(tmp, name)
    os.makedirs(root)
    json.dump({"scope": {"tokens": {"MOCA": MOCA, "MENTE": MENTE},
                         "generated_iso": "2026-09-27T12:00:00Z"},
               "facts": {"rate": live}}, open(os.path.join(root, "data.json"), "w"))
    json.dump({"day_rates": day_rates, "open_day_rate": open_day,
               "day_rate_src": {s: {d: "market" for d in v} for s, v in day_rates.items()}},
              open(os.path.join(root, "day_rates.json"), "w"))
    shards.save(os.path.join(root, "transfers"), sorted(out_rows, key=lambda r: r["timestamp"], reverse=True))
    shards.save(os.path.join(root, "transfers_in"), sorted(in_rows, key=lambda r: r["timestamp"], reverse=True))
    return root


def fixtures(tmp):
    live = {"MOCA": 0.0105, "MENTE": 0.018}
    od = {"MOCA": {"d": "2026-09-27", "rate": 0.0104}, "MENTE": {"d": "2026-09-27", "rate": 0.0177}}
    dr = {"MOCA": {"2026-07-11": 0.02, "2026-07-13": 0.021, "2026-09-26": 0.011},
          "MENTE": {"2026-05-01": 0.5, "2026-08-25": 0.03}}
    base_out = [
        _row(MOCA, "2026-07-01T10:00:00", 50),        # carry-back
        _row(MOCA, "2026-07-11T10:00:00", 50.5),      # exact
        _row(MOCA, "2026-07-12T23:59:59", 5),         # gap carry-forward
        _row(MOCA, "2026-08-15T00:00:00", 1),         # long carry-forward
        _row(MOCA, "2026-09-26T13:00:00", 4.8),       # latest closed day
        _row(MOCA, "2026-09-27T01:00:00", 96),        # open day
        _row(MENTE, "2026-04-24T08:00:00", 2),        # carry-back
        _row(MENTE, "2026-05-01T08:00:00", 0.2),      # exact
        _row(MENTE, "2026-08-30T08:00:00", 33.3),     # carry-forward past the last closed day
        _row(MENTE, "2026-09-27T08:00:00", 56.5),     # open day
    ]
    base_in = [_row(MOCA, "2026-07-02T00:00:00", 1000), _row(MENTE, "2026-09-27T09:00:00", 700)]
    yield "carry_legs", make_fixture(tmp, "carry_legs", dr, od, live, base_out, base_in)
    # a row stamped the day AFTER the open day (clock skew / a run straddling midnight)
    yield "after_open_day", make_fixture(tmp, "after_open_day", dr, od, live,
                                         base_out + [_row(MOCA, "2026-09-28T00:10:00", 7)], base_in)
    # open-day stamp on a day that is ALSO closed: the closed rate must win everywhere
    od2 = {"MOCA": {"d": "2026-09-26", "rate": 0.5}, "MENTE": od["MENTE"]}
    yield "open_day_already_closed", make_fixture(tmp, "open_day_already_closed", dr, od2, live, base_out, base_in)
    # bootstrap: MENTE has no closed day rates at all
    dr3 = {"MOCA": dr["MOCA"], "MENTE": {}}
    yield "no_closed_rates", make_fixture(tmp, "no_closed_rates", dr3, od, live,
                                          base_out + [_row(MENTE, "2026-09-20T00:00:00", 3),
                                                      _row(MENTE, "2026-09-29T00:00:00", 3)], base_in)


# ----------------------------------------------------------------- compare
def compare(case, root, seen):
    got = {name: fn(root) for name, fn in LOADERS.items()}
    ref = got["refresh"]
    fails = []
    for name, res in got.items():
        if name == "refresh":
            continue
        for leg in ("out", "in"):
            if res[leg] is None:
                continue
            a, b = ref[leg], res[leg]
            if len(a) != len(b):
                fails.append(f"{case}/{name}/{leg}: {len(b)} rows vs refresh {len(a)}")
                continue
            bad, ulps = {}, 0
            for (s1, t1, u1), (s2, t2, u2) in zip(a, b):
                if (s1, t1[:19]) != (s2, t2[:19]):
                    fails.append(f"{case}/{name}/{leg}: row order differs at {s1} {t1} vs {s2} {t2}")
                    break
                if u1 == u2:
                    continue
                if name in ULP_ALLOW and abs(u1 - u2) <= ULP_MAX * math.ulp(max(abs(u1), abs(u2))):
                    ulps += 1
                    continue
                bad.setdefault(t1[:10], []).append((s1, u1, u2))
            if ulps:
                seen.add(("ulp", name))
                print(f"allow {case}/{name}/{leg}: {ulps:,} row(s) within {ULP_MAX} ulp — {ULP_ALLOW[name][:80]}")
            for day, rs in sorted(bad.items()):
                key = (case, name, day) if (case, name, day) in ALLOW else (case, name, "*")
                if key in ALLOW:
                    seen.add(key)
                    print(f"allow {case}/{name}/{leg} {day}: {len(rs)} row(s) differ — {ALLOW[key][:90]}")
                else:
                    s, u1, u2 = rs[0]
                    fails.append(f"{case}/{name}/{leg} {day}: {len(rs)} row(s) differ from refresh "
                                 f"(first {s}: refresh {u1!r} vs {name} {u2!r})")
    n = len(ref["out"]) + len(ref["in"])
    return fails, n


def main():
    tmp = tempfile.mkdtemp(prefix="pricing-parity-")
    seen, fails = set(), []
    try:
        f, n = compare("real", ROOT, seen)
        fails += f
        print(f"{'ok' if not f else 'FAIL'} real tree: {n:,} rows through {len(LOADERS)} loaders")
        for case, root in fixtures(tmp):
            f, n = compare(case, root, seen)
            fails += f
            print(f"{'ok' if not f else 'FAIL'} fixture {case}: {n} rows")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    stale = sorted(set(ALLOW) - seen) + sorted(("ulp", n) for n in ULP_ALLOW if ("ulp", n) not in seen)
    for k in stale:
        fails.append(f"allow-list entry {k} no longer diverges — remove it")
    for f in fails:
        print("FAIL", f)
    if fails:
        print(f"test_pricing_parity: FAIL ({len(fails)})")
        sys.exit(1)
    print(f"test_pricing_parity: PASS ({len(seen)} allow-listed divergence class(es))")


if __name__ == "__main__":
    main()

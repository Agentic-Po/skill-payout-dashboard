#!/usr/bin/env python3
"""Consumer contract tests (Cycle-3 Loop 2, item 5).

Freezes the post-Loop-1 (schema v2) shapes that external consumers —
moca-ledger's cross-checker, spreadsheet pulls of transfers_export.csv,
alerts.py/notify.py themselves — depend on. A breaking change must arrive
as red CI plus a DELIBERATE CONSUMERS.md + schema_version bump, never as a
silent drift; loosen an assertion here only in the same change-set that
documents the new contract.

Plain asserts, stdlib only, no network:

  1. data.json: EXACT top-level key set, schema_version == 2, EXACT
     facts_window key set on every windows/prev24/monthly entry.
  2. transfers_export.csv: exact 13-column header tuple (and no
     counterparty_label — gone in v2, must stay gone).
  3. catalog.json: every entry carries name; every public dataset entry
     carries path/rows/coverage/generated_iso (private entries carry no
     path BY CONSTRUCTION — that is check_publish's stage-door invariant).
  4. classify.classify_usd / classify.pin_rate importable with stable
     signatures (inspect.signature string compare).

  python3 tests/test_contract.py     (no network, instant)
"""
import csv
import inspect
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

TOP_KEYS = {"schema_version", "scope", "facts", "infer", "server", "stripe_snap",
            "insights", "open_items", "gaps", "registry", "sink"}

WINDOW_KEYS = {"label", "out_usd", "in_usd", "economy_out_usd", "ops_out_usd",
               "in_recycled_usd", "in_external_usd", "net_usd", "out_tx", "in_tx",
               "out_wallets", "in_sources", "out_usd_tok", "out_raw", "in_raw"}

CSV_HEADER = ("timestamp_utc", "direction", "token", "amount", "rate_usd",
              "rate_source", "usd", "size_band", "counterparty", "tx_hash",
              "log_index", "class_coarse", "class_fine")

SIGNATURES = {"classify_usd": "(usd)", "pin_rate": "(day_rates, day, fallback)"}


def main():
    # 1. data.json
    D = json.load(open(os.path.join(ROOT, "data.json")))
    assert set(D) == TOP_KEYS, \
        f"data.json top-level drifted: extra={sorted(set(D)-TOP_KEYS)} missing={sorted(TOP_KEYS-set(D))}"
    assert D["schema_version"] == 2, f"schema_version {D['schema_version']!r} != 2"
    windows = D["facts"]["windows"] + [D["facts"]["prev24"]] + D["facts"]["monthly"]
    assert len(D["facts"]["windows"]) == 4, "facts.windows is no longer the 24h/7d/30d/all quartet"
    for w in windows:
        assert set(w) == WINDOW_KEYS, \
            f"facts_window {w.get('label')!r} drifted: extra={sorted(set(w)-WINDOW_KEYS)} missing={sorted(WINDOW_KEYS-set(w))}"
    print(f"ok data.json: top-level exact, schema_version 2, {len(windows)} window entries exact")

    # 2. transfers_export.csv
    with open(os.path.join(ROOT, "transfers_export.csv"), newline="") as fh:
        hdr = tuple(next(csv.reader(fh)))
    assert hdr == CSV_HEADER, f"CSV header drifted:\n  got  {hdr}\n  want {CSV_HEADER}"
    assert len(hdr) == 13 and "counterparty_label" not in hdr
    print("ok transfers_export.csv: exact 13-column header, no counterparty_label")

    # 3. catalog.json
    cat = json.load(open(os.path.join(ROOT, "catalog.json")))
    assert isinstance(cat, list) and cat, "catalog.json is not a non-empty list"
    n_pub = 0
    for e in cat:
        assert e.get("name"), f"catalog entry without a name: {e}"
        if e.get("public"):
            n_pub += 1
            for k in ("path", "rows", "coverage", "generated_iso"):
                assert k in e, f"public catalog entry {e['name']!r} lost {k!r}"
        else:
            assert "path" not in e, \
                f"PRIVATE catalog entry {e['name']!r} carries a path — the stage door depends on it not"
    assert n_pub, "catalog.json lists no public datasets"
    print(f"ok catalog.json: {n_pub} public entries carry name/path/rows/coverage/generated_iso")

    # 4. classify API
    import classify
    for fn, want in SIGNATURES.items():
        got = str(inspect.signature(getattr(classify, fn)))
        assert got == want, f"classify.{fn} signature drifted: {got!r} != {want!r}"
    print("ok classify.classify_usd / classify.pin_rate signatures stable")

    print("test_contract: PASS")


if __name__ == "__main__":
    main()

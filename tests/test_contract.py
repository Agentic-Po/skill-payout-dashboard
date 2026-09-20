#!/usr/bin/env python3
"""Consumer contract tests (Cycle-3 Loop 2, item 5).

Freezes the post-Loop-1 (schema v2) shapes that external consumers —
moca-ledger's cross-checker, spreadsheet pulls of transfers_export.csv,
alerts.py/notify.py themselves — depend on. A breaking change must arrive
as red CI plus a DELIBERATE CONSUMERS.md + schema_version bump, never as a
silent drift; loosen an assertion here only in the same change-set that
documents the new contract.

Plain asserts, stdlib only, no network:

  1. data.json: EXACT top-level key set, schema_version == 4, EXACT
     facts_window key set on every windows/prev24/monthly entry, group sums
     closing on out_usd, facts.float / facts.creator_wallets shapes.
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

# exec_summary added 2026-08-30 (Cycle-3 Loop 3, item 1) — ADDITIVE, no
# schema_version bump; documented in CONSUMERS.md §5.
TOP_KEYS = {"schema_version", "scope", "facts", "infer", "server", "stripe_snap",
            "insights", "open_items", "gaps", "registry", "sink", "exec_summary"}

# `groups` added 2026-09-15 (schema_version 3): the one grouping layer's sums
WINDOW_KEYS = {"label", "groups", "out_usd", "in_usd", "economy_out_usd", "ops_out_usd",
               "in_recycled_usd", "in_external_usd", "net_usd", "out_tx", "in_tx",
               "out_wallets", "in_sources", "out_usd_tok", "out_raw", "in_raw"}
GROUP_KEYS = ["skill_rewards", "credit_grants", "system_topups", "topups_delivered", "ops", "micro"]
FLOAT_KEYS = {"basis", "bal_usd", "out_24h_usd", "out_prev24_usd", "out_7d_avg_usd",
              "days_24h_pace", "days_7d_pace", "driver_24h"}
CW_WINDOW_KEYS = {"wallets", "usd", "equips", "invokes", "top1_share_pct", "top5_share_pct",
                  "top10_share_pct", "median_usd", "top"}
CW_TOP_KEYS = {"addr", "usd", "equips", "invokes", "first_seen"}

CSV_HEADER = ("timestamp_utc", "direction", "token", "amount", "rate_usd",
              "rate_source", "usd", "size_band", "counterparty", "tx_hash",
              "log_index", "class_coarse", "class_fine")

# classify_usd / band became era-aware on 2026-09-15 (Creator Rewards v2):
# the row timestamp is a REQUIRED positional argument — CONSUMERS.md §3.
SIGNATURES = {"classify_usd": "(usd, ts)", "band": "(usd, ts)",
              "pin_rate": "(day_rates, day, fallback)", "group_for": "(cat, fine)"}

# infer keys: exact. schema 3 (2026-09-15): `creators` (per-wallet ranking)
# retired in favour of facts.creator_wallets; legacy_public -> system_topup_public.
INFER_KEYS = {"S", "ce_total", "fine_table", "guard", "retired_public", "system_topup_public"}
GUARD_KEYS = {"loop_n", "loop_usd", "loop_gt10", "loop_gt50", "credit_recip_n", "ce_total_usd",
              "bal_usd", "dist_pace"}
REWARDS_V2_KEYS = {"resumed_utc", "grid", "usd_24h", "usd_since_resume", "n_equip_24h",
                   "n_invoke_24h", "creator_wallets_24h", "creator_wallets_since_resume"}
PROVENANCE_KEYS = {"by_token", "implied", "market", "refused", "carry_forward", "market_open",
                   "restatement_usd", "restatement_date"}
BAND_KEYS = ["micro", "b0005", "b005", "b010", "b1", "b3", "b5", "b10", "b20", "b25",
             "b50", "b100", "other"]


def main():
    # 1. data.json
    D = json.load(open(os.path.join(ROOT, "data.json")))
    assert set(D) == TOP_KEYS, \
        f"data.json top-level drifted: extra={sorted(set(D)-TOP_KEYS)} missing={sorted(TOP_KEYS-set(D))}"
    assert D["schema_version"] == 4, f"schema_version {D['schema_version']!r} != 4"
    windows = D["facts"]["windows"] + [D["facts"]["prev24"]] + D["facts"]["monthly"]
    assert len(D["facts"]["windows"]) == 4, "facts.windows is no longer the 24h/7d/30d/all quartet"
    for w in windows:
        assert set(w) == WINDOW_KEYS, \
            f"facts_window {w.get('label')!r} drifted: extra={sorted(set(w)-WINDOW_KEYS)} missing={sorted(WINDOW_KEYS-set(w))}"
        assert list(w["groups"]) == GROUP_KEYS, f"{w['label']}: group keys {list(w['groups'])}"
        # Six groups each round their own sum, so the total can drift up to
        # 6 x 0.005. Published per-group values stay honest (an independent
        # recompute matches them to the cent, which test_parity checks);
        # forcing exact closure instead is what broke that on 2026-09-18.
        gsum = sum(g["usd"] for g in w["groups"].values())
        assert abs(gsum - w["out_usd"]) <= 0.031, f"{w['label']}: group sums ${gsum:,.2f} != out_usd ${w['out_usd']:,.2f}"
        esum = sum(g["usd"] for k, g in w["groups"].items() if k != "ops")
        assert abs(esum - w["economy_out_usd"]) <= 0.031, f"{w['label']}: non-ops groups ${esum:,.2f} != economy ${w['economy_out_usd']:,.2f}"
    print(f"ok data.json: top-level exact, schema_version 4, {len(windows)} window entries exact, group sums close on out_usd")
    assert D["facts"]["group_keys"] == GROUP_KEYS and set(D["facts"]["group_labels"]) == set(GROUP_KEYS)
    fl = D["facts"]["float"]
    assert set(fl) == FLOAT_KEYS, f"facts.float keys drifted: {sorted(fl)}"
    assert "total outflow" in fl["basis"]
    cw = D["facts"]["creator_wallets"]
    assert set(cw["windows"]) == {"24h", "7d", "since_resume", "all"} and cw["default"] == "since_resume"
    for k, w in cw["windows"].items():
        want = CW_WINDOW_KEYS | ({"new_wallets"} if k in ("24h", "7d") else set())
        assert set(w) == want, f"creator_wallets[{k}] keys drifted: {sorted(w)}"
        assert len(w["top"]) <= 10 and all(set(t) == CW_TOP_KEYS for t in w["top"]), f"creator_wallets[{k}].top shape"
        assert all(len(t["first_seen"]) == 10 for t in w["top"]), "first_seen must be a DAY"
        if w["wallets"] < 10:
            assert w["median_usd"] is None, f"creator_wallets[{k}]: median published with n<10"
    print("ok data.json: facts.float (total-outflow basis) and facts.creator_wallets (4 windows, day-grain first_seen, median hidden <10)")
    assert set(D["infer"]) == INFER_KEYS, \
        f"infer drifted: extra={sorted(set(D['infer'])-INFER_KEYS)} missing={sorted(INFER_KEYS-set(D['infer']))}"
    assert set(D["infer"]["guard"]) == GUARD_KEYS, f"infer.guard drifted: {sorted(D['infer']['guard'])}"
    rv = D["facts"]["rewards_v2"]
    assert set(rv) == REWARDS_V2_KEYS, f"facts.rewards_v2 keys drifted: {sorted(rv)}"
    assert rv["resumed_utc"] == "2026-09-14T14:19Z" and set(rv["grid"]) == {"invoke", "equip"}
    for lp in D["infer"]["system_topup_public"]:
        assert set(lp) == {"cat", "since", "n", "usd", "last_seen"}, f"system_topup_public shape drifted: {lp}"
        assert lp["cat"] == "$1 system free top-up", lp["cat"]
    for rp in D["infer"]["retired_public"]:
        assert set(rp) == {"cat", "cutoff", "n", "usd", "last_seen"}, f"retired_public shape drifted: {rp}"
        assert rp["cat"] == "invoke_v1", f"retired_public cat {rp['cat']!r} != 'invoke_v1'"
    assert set(D["facts"]["pricing_provenance"]) == PROVENANCE_KEYS, \
        f"pricing_provenance keys drifted: {sorted(D['facts']['pricing_provenance'])}"
    assert D["facts"]["band_keys"] == BAND_KEYS, f"band_keys drifted: {D['facts']['band_keys']}"
    assert set(D["facts"]["band_labels"]) == set(BAND_KEYS)
    assert D["facts"]["band_labels"]["micro"].startswith("< $0.003"), D["facts"]["band_labels"]["micro"]
    for h in D["facts"]["hourly"]:
        assert set(h) == {"h", "MOCA", "MENTE", "g", "cw"} and set(h["g"]) <= set(GROUP_KEYS), h
    assert all(f.get("group") in GROUP_KEYS for f in D["infer"]["fine_table"]), "fine_table rows carry no group"
    assert D.get("stripe_snap") is None or not [k for k in D["stripe_snap"] if "subsidy" in k or "unbacked" in k]
    assert D.get("server") is None or not [k for k in D["server"] if k in ("subsidy_ratio", "ratio_weeks", "unbacked_7d")]
    print("ok data.json: infer keys exact, rewards_v2 / system_topup_public / retired_public / "
          "pricing_provenance shapes, hourly per-group counts, 13 band keys incl. b0005/b005")

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

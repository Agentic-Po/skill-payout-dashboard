#!/usr/bin/env python3
"""Redaction gate (Cycle-3 Loop 1): identities must never reach a public
artifact, and the --scan tripwire must actually fire when one does.

Four checks:
  1. data.json + index.html carry none of the known-leaked identity strings
     (custodian product, mind name, victim-naming mimic warning, audit-note
     fragments, employee names). The name/string constants are assembled from
     parts so THIS file is never itself a grep hit for what it polices.
  2. transfers_export.csv header has no counterparty_label (schema v2).
  3. check_publish.py --scan is green on the real tree.
  4. Seeding a fake identity label next to an 0x address into a copy of
     data.json makes scan() exit 1 — the tripwire proves red, not just green.

  python3 tests/test_redact.py     (no network, <5s)
"""
import csv, json, os, re, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# Assembled from parts — see docstring.
CUSTODIAN = "Fire" + "blocks"
MIND = "Game" + "master"
MIMIC_OF = "MIMIC" + " of"
AUDIT_FRAGS = ["DAT top" + " ups requested", "381," + "774"]
NAME_RES = [re.compile("ja" + "son" + r"\s+o" + "ng", re.I),
            re.compile("kat" + "herine" + r"\s+w" + "ebb", re.I)]
DENIED_SUBSTRINGS = [CUSTODIAN, MIND, MIMIC_OF] + AUDIT_FRAGS
# Creator Rewards v2 (2026-09-15): cap / sybil detector state and the legacy
# top-up ledger are calibration oracles — key names as EMITTED, never in any
# public artifact. No bare "4.00" substring test: it would match $14.00 and
# any rate ending in 4.00; the key-name assertions are the guard.
DETECTOR_KEYS = ["cap_table", "cap_hits", "cap_probe", "cap_state", "max_h60_units",
                 "max_clock_units", "hours_at_90pct", "fanout_hours", "grid_agreement",
                 "system_topup_ledger", "repeat_wallets",
                 # 2026-09-15 iteration: monitoring-status counts, the cap
                 # instant and every paid-vs-free basis left the public contract
                 "flagged_n", "monitored_n", "at_risk_usd", "cap_on_utc", "implied_user_spend",
                 "funding_split", "swarm_split", "subsidy_ratio", "ratio_weeks",
                 "pattern_monitor", "acct_map", "steward_", "mindset"]
# Word-level checks on ASSEMBLED prose (adversary X9): substrings that only
# ever appeared next to a monitoring status, a cap figure or a paid-vs-free
# claim. Assembled from parts so this file is never a hit for what it polices.
PROSE_DENIED = ["flagged for review", "wallets " + "monitored", " flagged ·", "monitored" + "</span>",
                "hourly cap " + "applies", "subsidy " + "ratio", "user-" + "funded", "unbacked",
                "gifted", "Implied " + "user spend", "top-up (" + "legacy)", "legacy " + "free top-up",
                "Legacy $1", "looks unusual", "under review", "account" + "s flagged",
                "revenue-" + "backed", "burn " + "growth", "reward " + "farm"]
PUBLIC_ARTIFACTS = ("data.json", "index.html", "legacy.html", "coupon_data.json",
                    "transfers_export.csv")


def _clean(rel):
    text = open(os.path.join(ROOT, rel), errors="replace").read()
    for s in DENIED_SUBSTRINGS:
        assert s not in text, f"{rel}: leaked identity string {s!r} present"
    for rx in NAME_RES:
        assert not rx.search(text), f"{rel}: leaked person name (pattern {rx.pattern!r})"
    print(f"ok {rel}: no leaked identity strings")


def main():
    # 1. public artifacts are clean
    for rel in ("data.json", "index.html"):
        _clean(rel)
    D = json.load(open(os.path.join(ROOT, "data.json")))
    assert D.get("schema_version") == 4, "data.json is not the v4 contract"
    print("ok data.json schema_version == 4")
    for rel in PUBLIC_ARTIFACTS:
        p = os.path.join(ROOT, rel)
        if not os.path.exists(p):
            continue
        text = open(p, errors="replace").read()
        hits = [k for k in DETECTOR_KEYS if k in text]
        assert not hits, f"{rel}: detector/ledger key(s) {hits} reached a public artifact"
    # the public rewards block carries no cap data of any kind
    rv = D["facts"].get("rewards_v2") or {}
    assert not [k for k in rv if "cap" in k], f"rewards_v2 carries cap data: {sorted(rv)}"
    for lp in D["infer"].get("system_topup_public") or []:
        assert "wallets" not in lp and "repeat" not in json.dumps(lp), f"system_topup_public leaks per-wallet data: {lp}"
    print(f"ok {len(DETECTOR_KEYS)} detector/ledger key names absent from every public artifact; rewards_v2 / system_topup_public carry no cap or per-wallet data")
    # word-level prose checks on every page a reader can open (assembled strings);
    # legacy.html is linked from the header, so the frozen view is policed too
    for rel in ("index.html", "data.json", "legacy.html"):
        text = open(os.path.join(ROOT, rel), errors="replace").read()
        hits = [w for w in PROSE_DENIED if w.lower() in text.lower()]
        assert not hits, f"{rel}: monitoring-status / cap / paid-vs-free prose present: {hits}"
    for rel in ("index.html", "data.json"):
        text = open(os.path.join(ROOT, rel), errors="replace").read()
        # 'legacy' survives ONLY as the link to the frozen legacy.html view
        stripped = text.replace("legacy.html", "").replace("legacy view (MOCA-only, old method)", "")
        n = len(re.findall("legacy", stripped, re.I))
        assert n == 0, f"{rel}: {n} 'legacy' mention(s) outside the legacy.html link"
        # the page never says 'creators' as a count of people — wallets only
        assert not re.search(r"\d\s+creators\b", text), f"{rel}: a count of 'creators' (should be creator wallets)"
    print(f"ok prose: {len(PROSE_DENIED)} status/cap/paid-vs-free phrases absent; 'legacy' only in the legacy.html link; counts say creator wallets")

    # 2. CSV header: counterparty stays, its label column is gone
    with open(os.path.join(ROOT, "transfers_export.csv"), newline="") as fh:
        hdr = next(csv.reader(fh))
    assert "counterparty" in hdr, "CSV lost the counterparty address column"
    assert "counterparty_label" not in hdr, "CSV still carries counterparty_label"
    print("ok transfers_export.csv header has no counterparty_label")

    # 3. the tripwire is green on the real tree
    p = subprocess.run([sys.executable, os.path.join(ROOT, "check_publish.py"), "--scan"],
                       capture_output=True, text=True, cwd=ROOT)
    assert p.returncode == 0, f"--scan is red on a clean tree:\n{p.stdout}{p.stderr}"
    print("ok check_publish --scan green on the real tree")

    # 4. and provably red on a seeded leak: a fake identity label next to an
    # 0x address (the exact reintroduction vector this loop closed).
    import check_publish as cp
    with tempfile.TemporaryDirectory() as td:
        seeded = json.load(open(os.path.join(ROOT, "data.json")))
        seeded.setdefault("registry", []).append(
            {"addr": "0x" + "ab" * 20, "label": "X (" + CUSTODIAN + ")"})
        json.dump(seeded, open(os.path.join(td, "data.json"), "w"))
        old = cp.HERE
        cp.HERE = td
        try:
            rc = cp.scan()
        finally:
            cp.HERE = old
        assert rc == 1, "seeded identity label did NOT trip --scan"
    print("ok seeded leak turns --scan red (exit 1)")

    print("test_redact: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

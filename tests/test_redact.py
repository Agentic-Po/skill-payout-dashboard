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
    assert D.get("schema_version") == 2, "data.json is not the v2 (redacted) contract"
    print("ok data.json schema_version == 2")

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

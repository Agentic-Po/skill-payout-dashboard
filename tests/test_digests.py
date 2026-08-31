#!/usr/bin/env python3
"""Closed-day digest ledger gate (Cycle-3 Loop 2, item 3).

Proves the ledger RED, not just green:

  1. Real tree verifies: recomputing every closed day from the on-disk
     shards + day_rates.json reproduces every sealed sha in
     day_digests.json (this is exactly what refresh.py will do next run —
     a failure here is a failure there).
  2. Tampering one sha in a COPY of the ledger makes enforce() raise
     DigestMismatch naming the day and both shas.
  3. Adding that day as a '## <day>' heading in a temp RESTATEMENTS.md
     turns the same tamper into a pass with a RESTATED line, and the
     ledger copy is updated to the recomputed sha.
  4. A sealed day whose rows vanish entirely also fails (repricing to
     zero is still repricing).

  python3 tests/test_digests.py     (no network, <30s)
"""
import contextlib
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import digests


def main():
    rows, today = digests.rows_from_disk(ROOT)
    records = digests.records_for_closed_days(rows, today)
    assert records, "no closed-day records — shards or data.json missing?"

    # 1. real ledger verifies against a full recompute
    led = digests.load_ledger(os.path.join(ROOT, "day_digests.json"))
    assert led, "day_digests.json is missing or empty — seed it (python3 digests.py --seed)"
    # enforce() leaves the most recent closed days unsealed for a 1-day
    # indexer-lag grace — those may legitimately be recomputed-but-unsealed
    in_grace = {d for d in records if digests._days_between(d, today) < 2}
    assert set(led) == set(records) - in_grace or set(led) == set(records), \
        f"ledger days != recomputed closed days: only-ledger={sorted(set(led)-set(records))} only-recomputed={sorted(set(records)-set(led)-in_grace)}"
    for day, rec in records.items():
        if day not in led:
            continue          # unsealed grace day
        got = digests.day_sha(rec)
        assert led[day]["sha"] == got, f"{day}: sealed {led[day]['sha']} != recomputed {got}"
        assert led[day].get("first_written_iso"), f"{day}: no first_written_iso"
    print(f"ok ledger verifies: {len(led)} sealed days match "
          f"({len(records) - len(led)} in sealing grace)")

    victim = sorted(records)[len(records) // 2]
    with tempfile.TemporaryDirectory() as td:
        led_path = os.path.join(td, "day_digests.json")
        rst_path = os.path.join(td, "RESTATEMENTS.md")

        # 2. tampered sha -> DigestMismatch with the day and both values
        bad = {d: dict(e) for d, e in led.items()}
        bad[victim]["sha"] = "0" * 64
        json.dump(bad, open(led_path, "w"))
        try:
            digests.enforce(records, ledger_path=led_path, restatements_path=rst_path,
                            now_iso="2026-08-30T00:00:00Z")
            raise AssertionError("tampered ledger did NOT fail")
        except digests.DigestMismatch as e:
            msg = str(e)
            assert victim in msg and "0" * 64 in msg and digests.day_sha(records[victim]) in msg, \
                f"mismatch message lacks day/both shas: {msg}"
            print(f"ok tampered sha for {victim} -> DigestMismatch (both values printed)")

        # 3. same tamper + RESTATEMENTS.md heading -> passes, RESTATED line, sha updated
        json.dump(bad, open(led_path, "w"))
        open(rst_path, "w").write(f"# Restatements\n\n## {victim}\n\ntest restatement\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            digests.enforce(records, ledger_path=led_path, restatements_path=rst_path,
                            now_iso="2026-08-30T00:00:00Z")
        out = buf.getvalue()
        assert f"RESTATED {victim}" in out, f"no RESTATED line:\n{out}"
        after = json.load(open(led_path))
        assert after[victim]["sha"] == digests.day_sha(records[victim]), "sha not updated on restate"
        assert after[victim].get("restated_iso"), "restated day carries no restated_iso"
        print(f"ok restated {victim}: passes with RESTATED line, seal updated")

        # 4. sealed day with no rows this run -> fail (repricing to zero)
        json.dump({d: dict(e) for d, e in led.items()}, open(led_path, "w"))
        os.remove(rst_path)
        shrunk = {d: r for d, r in records.items() if d != victim}
        try:
            digests.enforce(shrunk, ledger_path=led_path, restatements_path=rst_path,
                            now_iso="2026-08-30T00:00:00Z")
            raise AssertionError("vanished sealed day did NOT fail")
        except digests.DigestMismatch as e:
            assert victim in str(e)
            print(f"ok vanished sealed day {victim} -> DigestMismatch")

    print("test_digests: PASS")


if __name__ == "__main__":
    main()

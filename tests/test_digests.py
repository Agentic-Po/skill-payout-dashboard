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
    # Creator Rewards v2 (2026-09-15): the era-aware classifier must be
    # digest-neutral — a dry enforce() on a COPY of the real ledger with the
    # real RESTATEMENTS.md prints zero RESTATED lines, and the 09-14 note in
    # RESTATEMENTS.md is NOT a standing '## YYYY-MM-DD' approval.
    approved = digests.parse_restatements()
    assert "2026-09-14" not in approved, "RESTATEMENTS.md carries a standing approval for 2026-09-14"
    with tempfile.TemporaryDirectory() as td:
        led_copy = os.path.join(td, "day_digests.json")
        json.dump(led, open(led_copy, "w"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            digests.enforce(records, ledger_path=led_copy, now_iso=today + "T00:00:00Z")
        assert "RESTATED" not in buf.getvalue(), f"era-aware classifier restated a sealed day:\n{buf.getvalue()}"
    print(f"ok zero RESTATED lines on a dry enforce ({len(approved)} approved restatement(s), none for 2026-09-14)")

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
        exact = {"day": victim, "old_sha": "0" * 64,
                 "new_sha": digests.day_sha(records[victim])}
        open(rst_path, "w").write(f"# Restatements\n\n## {victim}\n\n"
                                  "```digest-transition\n" + json.dumps(exact) + "\n```\n")
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

    test_exact_and_coverage()
    print("test_digests: PASS")


def test_exact_and_coverage():
    day = "2026-10-08"
    old = {"day": day, "out_tx": 6697, "out_usd": 26062.52,
           "economy_out_usd": 26062.52, "ops_out_usd": 0.0,
           "rates": {"MOCA": 0.0092414}}
    new = {**old, "out_tx": 10950, "out_usd": 37470.74, "economy_out_usd": 37470.74}
    authorization = digests.parse_exact_restatements()[day]
    assert authorization["old_sha"] == digests.day_sha(old)
    assert authorization["new_sha"] == digests.day_sha(new)
    assert day not in digests.parse_restatements(), "exact day acquired blanket approval"

    # Authenticate the documented new SHA against actual verified pending rows,
    # not merely an embedded duplicate record.
    import shards, public_scan
    pending_dir = os.path.join(ROOT, "pending_scans")
    public_scan.validate_store(pending_dir)
    data = json.load(open(os.path.join(ROOT, "data.json")))
    desc = public_scan.descriptor(data["scope"]["wallet"], "from", data["scope"]["tokens"].values())
    sid = public_scan.scan_id(desc)
    manifest = json.load(open(os.path.join(pending_dir, sid + ".json")))
    cached = shards.load(os.path.join(ROOT, "transfers"))
    by_id = {(r["transaction_hash"].lower(), int(r["log_index"])): r for r in cached}
    for part in manifest["chunks"]:
        for row in json.load(open(os.path.join(pending_dir, sid, part["file"]))):
            by_id.setdefault((row["transaction_hash"].lower(), int(row["log_index"])), row)
    original_load = shards.load
    try:
        shards.load = lambda path: list(by_id.values())
        canonical, today = digests.rows_from_disk(ROOT)
    finally:
        shards.load = original_load
    rec = digests.records_for_closed_days(canonical, today)[day]
    assert rec == new and digests.day_sha(rec) == authorization["new_sha"]

    with tempfile.TemporaryDirectory() as td:
        lp = os.path.join(td, "ledger.json")
        rp = os.path.join(td, "audit.md")
        def audit(entry=authorization):
            return f"## {day}\n\n```digest-transition\n" + json.dumps(entry) + "\n```\n"
        def run(record=new, stored=None, text=None, allow=True):
            json.dump({day: {"sha": stored or authorization["old_sha"],
                            "first_written_iso": "2026-10-10T00:50:55Z"}}, open(lp, "w"))
            open(rp, "w").write(audit() if text is None else text)
            return digests.enforce({day: record}, ledger_path=lp, restatements_path=rp,
                                   now_iso="2026-10-10T14:00:00Z", allow_new_seals=allow)
        def blocked(**kwargs):
            try:
                run(**kwargs)
            except digests.DigestMismatch:
                return
            raise AssertionError("unauthorized digest transition passed")
        accepted = run()
        assert accepted[day]["sha"] == authorization["new_sha"]
        assert accepted[day]["previous_sha"] == authorization["old_sha"]
        # Once applied, old or any different future aggregate cannot reseal.
        blocked(record=old, stored=authorization["new_sha"])
        blocked(stored="a" * 64)
        for field, value in [("out_tx", 10951), ("out_usd", 37470.75),
                             ("rates", {"MOCA": 0.0092415})]:
            blocked(record={**new, field: value})
        for text in [f"## {day}\n", audit().replace("new_sha", "replacement"),
                     audit()[:-5], audit().replace(authorization["old_sha"], "b" * 64),
                     audit().replace(authorization["new_sha"], "c" * 64),
                     audit().replace('"day":', '"day": "2026-10-08", "day":'),
                     audit() + f"\n## {day}\n"]:
            blocked(text=text)
        # Existing seals remain guarded while partial. New seals await proof.
        blocked(record={**new, "out_tx": 1}, allow=False)
        json.dump({}, open(lp, "w")); open(rp, "w").write("")
        assert digests.enforce({day: new}, ledger_path=lp, restatements_path=rp,
                              now_iso="2026-10-10T14:00:00Z", allow_new_seals=False) == {}
        # Existing API callers retain the default behavior (offline compatibility).
        assert day in digests.enforce({day: new}, ledger_path=lp, restatements_path=rp,
                                     now_iso="2026-10-10T14:00:00Z")
        # Historical 08/22 authorization retains its old semantics.
        legacy = "2026-08-22"
        json.dump({legacy: {"sha": "a" * 64}}, open(lp, "w"))
        open(rp, "w").write(f"## {legacy}\n")
        assert digests.enforce({legacy: new}, ledger_path=lp, restatements_path=rp)[legacy]["sha"] == digests.day_sha(new)
        # The producer gates new seals on its own pair, not aggregate subsidiaries.
        import ast
        tree = ast.parse(open(os.path.join(ROOT, "refresh.py")).read())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == "enforce"]
        assert len(calls) == 1
        kw = next(k for k in calls[0].keywords if k.arg == "allow_new_seals")
        assert ast.unparse(kw.value) == "_digests.new_seal_permission(OFFLINE, data_complete, PREV)"
        # Execute the actual flag initializer and enforce argument in a
        # minimal production namespace, so an undefined flag cannot pass AST QA.
        import types
        flag_assignment = next(n for n in tree.body if isinstance(n, ast.Assign)
                               and any(isinstance(t, ast.Name) and t.id == "OFFLINE" for t in n.targets))
        for offline in (False, True):
            for own in (False, True):
                for saved in (None, False, 1, True):
                    previous = {"scope": {"complete": True,
                                "source_coverage": {"treasury_complete": saved}}}
                    ns = {"sys": types.SimpleNamespace(argv=["refresh.py"] + (["--offline"] if offline else [])),
                          "_digests": digests, "data_complete": own, "PREV": previous}
                    exec(compile(ast.Module(body=[flag_assignment], type_ignores=[]), "refresh.py", "exec"), ns)
                    got = eval(compile(ast.Expression(kw.value), "refresh.py", "eval"), ns)
                    assert got == ((saved is True) if offline else own)
        for legacy_scope in ({}, {"complete": True}, {"complete": False}):
            assert not digests.new_seal_permission(True, True, {"scope": legacy_scope})
        for own in (None, False, 1, True):
            prior = {"scope": {"complete": True,
                     "source_coverage": {"treasury_complete": own}}}
            assert digests.new_seal_permission(True, True, prior) == (own is True)
            assert digests.new_seal_permission(False, own, prior) == (own is True)
        # Offline financial defaultTrue is not evidence: execute actual seal
        # gating with a legacy-complete snapshot and verify no file is banked.
        json.dump({}, open(lp, "w")); open(rp, "w").write("")
        permission = digests.new_seal_permission(True, True, {"scope": {"complete": True}})
        assert digests.enforce({day: new}, ledger_path=lp, restatements_path=rp,
                              now_iso="2026-10-10T14:00:00Z", allow_new_seals=permission) == {}
    # Exercise the actual CLI entry function with disk evidence and real
    # ledger writes. Row acquisition is synthetic; permission is never mocked.
    with tempfile.TemporaryDirectory() as td:
        original_rows = digests.rows_from_disk
        row = {"ts": day + "T12:00:00", "tok": "MOCA", "rate": 1,
               "usd": 2, "cat": "economy", "tx": "test", "li": 0}
        digests.rows_from_disk = lambda root: ([row], "2026-10-10")
        try:
            for flag in (False, None, 1, True):
                coverage = {} if flag is None else {"treasury_complete": flag}
                json.dump({"scope": {"complete": False, "source_coverage": coverage}},
                          open(os.path.join(td, "data.json"), "w"))
                lp = os.path.join(td, "day_digests.json")
                if os.path.exists(lp): os.remove(lp)
                result = digests.seed_from_disk(td, "2026-10-10T14:00:00Z")
                assert bool(result) == (flag is True)
            json.dump({"scope": {"source_coverage": {"treasury_complete": False}}},
                      open(os.path.join(td, "data.json"), "w"))
            json.dump({day: {"sha": "a" * 64}}, open(lp, "w"))
            try:
                digests.seed_from_disk(td, "2026-10-10T14:00:00Z")
                raise AssertionError("partial CLI bypassed existing seal mismatch")
            except digests.DigestMismatch:
                pass
        finally:
            digests.rows_from_disk = original_rows
        cli = ast.parse(open(os.path.join(ROOT, "digests.py")).read())
        main_if = next(n for n in cli.body if isinstance(n, ast.If))
        assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == "seed_from_disk" for n in ast.walk(main_if))
    print("ok exact one-time correction, canonical evidence, mutation guards, partial/CLI sealing and legacy compatibility")


if __name__ == "__main__":
    main()

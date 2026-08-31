#!/usr/bin/env python3
"""The publish allowlist must not be widened by where a file happens to sit.

Two vectors closed on 2026-08-30 after QA, both regression-tested here:

  1. fnmatch("*.py") does NOT treat "/" specially, so an unanchored "*.py"
     allowlisted a .py at ANY depth — `.creds_probe/keys.py` staged silently.
  2. Catalog dataset entries are directories ("transfers/"). Prefix-matching
     them allowlisted every new file under the tree regardless of extension,
     and stage() emitted the bare directory, which `git add transfers/` then
     expanded on its own — reintroducing exactly the `git add -A` behaviour
     the allowlist exists to remove.

Both seeded files must be ABSENT from the stage list and PRESENT in the
warnings. Nothing is committed and the seeds are removed in a finally block.

  python3 tests/test_stage.py     (no network, instant)
"""
import os, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

NESTED_PY = os.path.join(ROOT, ".creds_probe", "keys.py")
STRAY = os.path.join(ROOT, "transfers", "LEAK_probe.json")
COUPON_STRAY = os.path.join(ROOT, "coupon_out", "LEAK_probe.json")
# Every dataset directory in the catalog must be staged file-by-file. Listed
# by name so a new dataset dir that stops being staged is a red test, not a
# silently unpublished ledger.
SHARD_DIRS = ("transfers/", "transfers_in/", "cognition_in/",
              "coupon_out/", "coupon_in/")


def _stage():
    p = subprocess.run([sys.executable, os.path.join(ROOT, "check_publish.py"), "--stage"],
                       capture_output=True, text=True, cwd=ROOT)
    assert p.returncode == 0, f"--stage exited {p.returncode}: {p.stderr}"
    return p.stdout.splitlines(), p.stderr


def main():
    seeds = (NESTED_PY, STRAY, COUPON_STRAY)
    assert not any(os.path.exists(p) for p in seeds), \
        "probe files already exist — refusing to clobber"
    os.makedirs(os.path.dirname(NESTED_PY), exist_ok=True)
    open(NESTED_PY, "w").write("TOKEN = 1\n")
    open(STRAY, "w").write('{"leak": 1}\n')
    open(COUPON_STRAY, "w").write('{"leak": 1}\n')
    try:
        files, warns = _stage()
        for probe in (".creds_probe/keys.py", "transfers/LEAK_probe.json",
                      "coupon_out/LEAK_probe.json"):
            assert probe not in files, f"{probe} was STAGED — allowlist is too wide"
            assert probe in warns, f"{probe} staged-or-not was not WARNED about"
            print(f"ok {probe}: not staged, warned")
        # No bare directory may ever be emitted: git would expand it past us.
        bare = [f for f in files if f.endswith("/")]
        assert not bare, f"stage() emitted directories: {bare}"
        print("ok stage list contains no bare directories")
        # And every dataset's real shards are still there, as individual files.
        for d in SHARD_DIRS:
            shard = [f for f in files if f.startswith(d) and f.endswith(".json")]
            assert shard, f"no {d} shard files in the stage list"
            print(f"ok {len(shard)} {d} shard file(s) staged individually")
        # The coupon page's own artifacts must be staged too — a new file that
        # is never committed is the failure mode PUBLISH_EXTRA exists to stop.
        for f in ("coupon.html", "template_coupon.html", "coupon_data.json"):
            assert f in files, f"{f} is not in the stage list — add it to PUBLISH_EXTRA"
        print("ok coupon.html / template_coupon.html / coupon_data.json staged")
    finally:
        shutil.rmtree(os.path.dirname(NESTED_PY), ignore_errors=True)
        for p in (STRAY, COUPON_STRAY):
            if os.path.exists(p):
                os.remove(p)
    print("test_stage: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

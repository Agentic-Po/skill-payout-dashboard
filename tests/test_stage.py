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


def _stage():
    p = subprocess.run([sys.executable, os.path.join(ROOT, "check_publish.py"), "--stage"],
                       capture_output=True, text=True, cwd=ROOT)
    assert p.returncode == 0, f"--stage exited {p.returncode}: {p.stderr}"
    return p.stdout.splitlines(), p.stderr


def main():
    assert not os.path.exists(NESTED_PY) and not os.path.exists(STRAY), \
        "probe files already exist — refusing to clobber"
    os.makedirs(os.path.dirname(NESTED_PY), exist_ok=True)
    open(NESTED_PY, "w").write("TOKEN = 1\n")
    open(STRAY, "w").write('{"leak": 1}\n')
    try:
        files, warns = _stage()
        for probe in (".creds_probe/keys.py", "transfers/LEAK_probe.json"):
            assert probe not in files, f"{probe} was STAGED — allowlist is too wide"
            assert probe in warns, f"{probe} staged-or-not was not WARNED about"
            print(f"ok {probe}: not staged, warned")
        # No bare directory may ever be emitted: git would expand it past us.
        bare = [f for f in files if f.endswith("/")]
        assert not bare, f"stage() emitted directories: {bare}"
        print("ok stage list contains no bare directories")
        # And the real shards are still there, as individual files.
        shard = [f for f in files if f.startswith("transfers/") and f.endswith(".json")]
        assert shard, "no transfers/ shard files in the stage list"
        print(f"ok {len(shard)} transfers/ shard file(s) staged individually")
    finally:
        shutil.rmtree(os.path.dirname(NESTED_PY), ignore_errors=True)
        if os.path.exists(STRAY):
            os.remove(STRAY)
    print("test_stage: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

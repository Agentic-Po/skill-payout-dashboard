#!/usr/bin/env python3
"""Encrypt / decrypt the private state carried in the Actions cache (2026-09-27).

Any workflow in this repo can restore the `alert-state-*` cache, so the files
it carries — detector state, the per-wallet guard table, the private log —
are stored ENCRYPTED with the repo secret STATE_KEY. The workflows do:

    actions/cache/restore  (only the .enc files)
    python3 tools/state_crypt.py decrypt     .enc -> plaintext, or FAIL
    ... the job ...
    python3 tools/state_crypt.py encrypt     plaintext -> .enc, or FAIL
    actions/cache/save     (only the .enc files; skipped unless decrypt passed)

Format: MAGIC + HMAC-SHA256(ciphertext) + `openssl enc -aes-256-cbc -pbkdf2
-salt` output. openssl ships on ubuntu-latest; hmac/hashlib are stdlib — no
new dependencies. The MAC (encrypt-then-MAC, key derived from STATE_KEY with
PBKDF2) makes a wrong key or a corrupted blob fail deterministically instead
of relying on CBC padding luck; the plaintext is also required to be JSON.

Failure rules (all exit 1 with an ::error:: annotation, no numbers):
  * STATE_KEY unset/empty            -> never silently save plaintext
  * any .enc fails MAC/decrypt/JSON  -> NOTHING is written (all-or-nothing),
                                        so state is never replaced by garbage
                                        or {}; the workflow skips the save.
Migration: a .enc absent but its plaintext present (the one-time legacy
plaintext cache restore) -> the plaintext is used as-is and the next
`encrypt` stores it encrypted.

  python3 tools/state_crypt.py encrypt|decrypt|files [--root DIR]
"""
import hashlib
import hmac
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# EXACTLY the files the Actions cache carries (as FILE + ".enc").
# tests/test_state_crypt.py holds every workflow's cache path list to this.
FILES = ("alert_state.json", "guard_private.json", "private_log.json")
# the pre-2026-09-27 PLAINTEXT cache path list — only for the one-time
# migration restore step (its cache "version" is a hash of this list)
LEGACY_FILES = ("alert_state.json", "guard_private.json")
MAGIC = b"SPDSTATE1\n"
ITER = 200_000
_ENV = "STATE_KEY"


class CryptError(Exception):
    pass


def _key():
    k = os.environ.get(_ENV, "")
    if not k.strip():
        raise CryptError(f"{_ENV} is not set — refusing to handle private state without it. "
                         f"Set the repo secret {_ENV} (e.g. `openssl rand -base64 48`) and pass it "
                         f"to this step as env.{_ENV}.")
    return k


def _mac_key(key):
    return hashlib.pbkdf2_hmac("sha256", key.encode(), b"spd-state-mac-v1", ITER)


def _openssl(args, data, key):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "SPD_STATE_PASS": key}
    p = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", str(ITER), "-salt",
                        "-pass", "env:SPD_STATE_PASS"] + args,
                       input=data, capture_output=True, env=env)
    if p.returncode != 0:
        raise CryptError("openssl failed: " + p.stderr.decode(errors="replace").strip()[:200])
    return p.stdout


def encrypt_bytes(data, key):
    ct = _openssl([], data, key)
    return MAGIC + hmac.new(_mac_key(key), ct, hashlib.sha256).digest() + ct


def decrypt_bytes(blob, key):
    if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + 32:
        raise CryptError("not a state_crypt blob (bad header)")
    tag, ct = blob[len(MAGIC):len(MAGIC) + 32], blob[len(MAGIC) + 32:]
    if not hmac.compare_digest(tag, hmac.new(_mac_key(key), ct, hashlib.sha256).digest()):
        raise CryptError("authentication failed — wrong STATE_KEY or corrupted cache")
    pt = _openssl(["-d"], ct, key)
    try:
        json.loads(pt)
    except ValueError:
        raise CryptError("decrypted payload is not JSON")
    return pt


def _atomic_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def encrypt(root=ROOT):
    key = _key()
    done = []
    for f in FILES:
        p = os.path.join(root, f)
        if os.path.exists(p):
            with open(p, "rb") as fh:
                data = fh.read()
            try:
                json.loads(data)
            except ValueError:
                # state.load() already degrades a corrupt file to {}; encrypting
                # it would make every later decrypt fail. Drop it (cold next run
                # — the documented worst case: one duplicate alert).
                print(f"::warning::state_crypt: {f} is not valid JSON — not saved (cold next run)")
                if os.path.exists(p + ".enc"):
                    os.remove(p + ".enc")
                continue
            _atomic_write(p + ".enc", encrypt_bytes(data, key))
            done.append(f)
        elif os.path.exists(p + ".enc"):
            # plaintext gone this run: never re-save a stale restored blob
            os.remove(p + ".enc")
    print(f"state_crypt: encrypted {', '.join(done) or 'nothing (no state files yet)'}")
    return done


def decrypt(root=ROOT):
    key = _key()
    out, migrated, cold = {}, [], []
    for f in FILES:
        p = os.path.join(root, f)
        if os.path.exists(p + ".enc"):
            with open(p + ".enc", "rb") as fh:
                try:
                    out[f] = decrypt_bytes(fh.read(), key)
                except CryptError as e:
                    raise CryptError(f"{f}.enc: {e} — no state file was written")
        elif os.path.exists(p):
            migrated.append(f)
        else:
            cold.append(f)
    for f, pt in out.items():          # all-or-nothing: only after every blob verified
        _atomic_write(os.path.join(root, f), pt)
    print("state_crypt: decrypted " + (", ".join(out) or "nothing")
          + (f" · migrating legacy plaintext {', '.join(migrated)} (saved encrypted at job end)" if migrated else "")
          + (f" · cold (absent) {', '.join(cold)}" if cold else ""))
    return {"decrypted": list(out), "migrated": migrated, "cold": cold}


def main(argv):
    root = ROOT
    if "--root" in argv:
        root = os.path.abspath(argv[argv.index("--root") + 1])
    cmd = argv[0] if argv else ""
    try:
        if cmd == "encrypt":
            encrypt(root)
        elif cmd == "decrypt":
            decrypt(root)
        elif cmd == "files":
            print("\n".join(f + ".enc" for f in FILES))
        else:
            print(__doc__)
            return 2
    except CryptError as e:
        print(f"::error::state_crypt {cmd}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

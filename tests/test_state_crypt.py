#!/usr/bin/env python3
"""The Actions-cache state is encrypted end to end (2026-09-27).

  (a) roundtrip: encrypt -> drop plaintext -> decrypt gives the same bytes
  (b) wrong key: exit 1, ::error::, and NO state file written or overwritten
  (c) corrupted blob / one bad blob among good ones: nothing written at all
  (d) missing / empty STATE_KEY: encrypt and decrypt both exit 1, no .enc made
  (e) migration: plaintext present and no .enc -> used as-is, then encrypted
  (f) a corrupt-JSON plaintext is not encrypted (it would brick every decrypt)
  (g) the workflows: refresh/daily/weekly restore and save EXACTLY the .enc
      of state_crypt.FILES, the legacy migration restore uses the old
      plaintext list only when the encrypted restore missed, the implicit
      actions/cache@ (post-step save) is gone, decrypt -> encrypt -> save are
      ordered and gated, and STATE_KEY comes from the secret
  (h) the .enc files and private_log.json are gitignored and never staged

  python3 tests/test_state_crypt.py     (needs `openssl` on PATH; no network)
"""
import json, os, re, shutil, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import state_crypt  # noqa: E402

TOOL = os.path.join(ROOT, "tools", "state_crypt.py")
fails = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def run(cmd, root, key):
    env = {k: v for k, v in os.environ.items() if k != "STATE_KEY"}
    if key is not None:
        env["STATE_KEY"] = key
    p = subprocess.run([sys.executable, TOOL, cmd, "--root", root], capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


SAMPLE = {"alert_state.json": json.dumps({"seen": ["0xabc:1"], "cap_probe": {"rows": 7}}).encode(),
          "guard_private.json": json.dumps({"cap_table": [{"addr": "0x1", "n": 3}]}).encode(),
          "private_log.json": json.dumps([{"ts": "t", "src": "alerts", "line": "x"}]).encode()}
K1, K2 = "correct horse battery staple " * 2, "wrong key"


def seed(d, files=SAMPLE):
    for f, b in files.items():
        open(os.path.join(d, f), "wb").write(b)


def read(d, f):
    return open(os.path.join(d, f), "rb").read()


with tempfile.TemporaryDirectory() as d:
    # (a) roundtrip
    seed(d)
    rc, out = run("encrypt", d, K1)
    check(rc == 0 and all(os.path.exists(os.path.join(d, f + ".enc")) for f in SAMPLE), "encrypt writes every .enc")
    check(all(SAMPLE[f] not in read(d, f + ".enc") for f in SAMPLE),
          "ciphertext does not contain the plaintext")
    check(b"cap_table" not in read(d, "guard_private.json.enc"), "no plaintext key names in the blob")
    for f in SAMPLE:
        os.remove(os.path.join(d, f))
    rc, out = run("decrypt", d, K1)
    check(rc == 0 and all(read(d, f) == SAMPLE[f] for f in SAMPLE), "decrypt restores identical bytes")

    # (b) wrong key: fails, writes nothing, overwrites nothing
    for f in SAMPLE:
        os.remove(os.path.join(d, f))
    rc, out = run("decrypt", d, K2)
    check(rc == 1 and "::error::" in out, "wrong key exits 1 with an ::error:: annotation")
    check(not any(os.path.exists(os.path.join(d, f)) for f in SAMPLE), "wrong key writes no state file")
    open(os.path.join(d, "alert_state.json"), "w").write('{"keep": 1}')
    rc, out = run("decrypt", d, K2)
    check(rc == 1 and read(d, "alert_state.json") == b'{"keep": 1}', "wrong key leaves existing state untouched")
    check(not re.search(r"\d", out.split("::error::", 1)[-1]),
          "the failure annotation carries no numbers")

    # (c) one corrupted blob among good ones -> all-or-nothing
    os.remove(os.path.join(d, "alert_state.json"))
    blob = bytearray(read(d, "private_log.json.enc"))
    blob[-5] ^= 0x01
    open(os.path.join(d, "private_log.json.enc"), "wb").write(bytes(blob))
    rc, out = run("decrypt", d, K1)
    check(rc == 1 and not any(os.path.exists(os.path.join(d, f)) for f in SAMPLE),
          "a single corrupted blob fails the decrypt and writes NO file (all-or-nothing)")
    open(os.path.join(d, "alert_state.json.enc"), "wb").write(b"garbage")
    rc, out = run("decrypt", d, K1)
    check(rc == 1, "a non-state_crypt blob fails")

with tempfile.TemporaryDirectory() as d:
    # (d) missing / empty key
    seed(d)
    for key in (None, "", "   "):
        rc, out = run("encrypt", d, key)
        check(rc == 1 and "STATE_KEY" in out and "::error::" in out
              and not any(os.path.exists(os.path.join(d, f + ".enc")) for f in SAMPLE),
              f"encrypt with STATE_KEY={key!r} fails loudly and writes no .enc (never plaintext)")
        rc, out = run("decrypt", d, key)
        check(rc == 1 and "STATE_KEY" in out, f"decrypt with STATE_KEY={key!r} fails loudly")

    # (e) migration: legacy plaintext restore, no .enc
    legacy = {f: SAMPLE[f] for f in state_crypt.LEGACY_FILES}
    for f in SAMPLE:
        os.remove(os.path.join(d, f))
    seed(d, legacy)
    rc, out = run("decrypt", d, K1)
    check(rc == 0 and "migrating legacy plaintext" in out
          and all(read(d, f) == legacy[f] for f in legacy), "plaintext without .enc is used as-is (migration)")
    rc, out = run("encrypt", d, K1)
    check(rc == 0 and all(os.path.exists(os.path.join(d, f + ".enc")) for f in legacy),
          "migrated plaintext is saved encrypted")
    for f in legacy:
        os.remove(os.path.join(d, f))
    rc, out = run("decrypt", d, K1)
    check(rc == 0 and all(read(d, f) == legacy[f] for f in legacy), "migrated state decrypts back")

with tempfile.TemporaryDirectory() as d:
    # (f) corrupt JSON plaintext is dropped, not encrypted
    open(os.path.join(d, "alert_state.json"), "w").write('{"truncated": ')
    open(os.path.join(d, "alert_state.json.enc"), "wb").write(b"stale")
    rc, out = run("encrypt", d, K1)
    check(rc == 0 and not os.path.exists(os.path.join(d, "alert_state.json.enc")),
          "corrupt-JSON plaintext is not encrypted and its stale .enc is removed (cold next run)")
    rc, out = run("decrypt", d, K1)
    check(rc == 0, "…and the next decrypt is a clean cold start")

# (g) workflows
WANT = [f + ".enc" for f in state_crypt.FILES]


def steps(text):
    """Top-level job steps as raw text chunks (6-space `- ` items)."""
    parts = re.split(r"(?m)^      - ", text)
    return ["- " + p for p in parts[1:]]


def field(step, name):
    m = re.search(rf"(?m)^[- ] *{name}: (.*)$", step)
    return m.group(1).strip() if m else None


def paths(step):
    m = re.search(r"(?m)^( +)path: \|\n((?:\1  .*\n?)+)", step)
    return [ln.strip() for ln in m.group(2).splitlines()] if m else None


for wf in ("refresh", "daily", "weekly"):
    text = open(os.path.join(ROOT, ".github", "workflows", wf + ".yml")).read()
    st = steps(text)
    check("actions/cache@" not in text, f"{wf}.yml: implicit actions/cache@ (post-step save) is gone")
    rest = [s for s in st if (field(s, "uses") or "").startswith("actions/cache/restore@")]
    save = [s for s in st if (field(s, "uses") or "").startswith("actions/cache/save@")]
    check(len(rest) == 2 and len(save) == 1, f"{wf}.yml: two restores (encrypted + legacy) and one save")
    if len(rest) != 2 or len(save) != 1:
        continue
    enc_r, leg_r = rest
    check(paths(enc_r) == WANT and field(enc_r, "restore-keys") == "alert-state-enc-",
          f"{wf}.yml: encrypted restore carries exactly {WANT}")
    check(paths(save[0]) == WANT and field(save[0], "key") == "alert-state-enc-${{ github.run_id }}",
          f"{wf}.yml: save stores ONLY the .enc files")
    check(paths(leg_r) == list(state_crypt.LEGACY_FILES)
          and field(leg_r, "if") == "steps.restore.outputs.cache-matched-key == ''"
          and field(enc_r, "id") == "restore",
          f"{wf}.yml: legacy plaintext restore only when the encrypted restore missed")
    idx = {n: next((i for i, s in enumerate(st) if field(s, "id") == n), None) for n in ("decrypt", "encrypt")}
    i_save, i_leg = st.index(save[0]), st.index(leg_r)
    check(None not in idx.values() and i_leg < idx["decrypt"] < idx["encrypt"] < i_save and i_save == len(st) - 1,
          f"{wf}.yml: restore -> decrypt -> … -> encrypt -> save (save is the last step)")
    if None in idx.values():
        continue
    dec, enc = st[idx["decrypt"]], st[idx["encrypt"]]
    check("tools/state_crypt.py decrypt" in dec and "tools/state_crypt.py encrypt" in enc
          and "if:" not in dec, f"{wf}.yml: decrypt/encrypt steps call state_crypt; decrypt is unconditional")
    check(all("STATE_KEY: ${{ secrets.STATE_KEY }}" in s for s in (dec, enc)), f"{wf}.yml: STATE_KEY from the secret")
    check(field(enc, "if") == "always() && steps.decrypt.outcome == 'success'"
          and field(save[0], "if") == "always() && steps.encrypt.outcome == 'success'",
          f"{wf}.yml: encrypt/save run always() but only after a successful decrypt/encrypt")

# (h) never committed
ign = subprocess.run(["git", "check-ignore"] + WANT + ["private_log.json"], cwd=ROOT,
                     capture_output=True, text=True)
check(sorted(ign.stdout.split()) == sorted(WANT + ["private_log.json"]),
      ".enc files and private_log.json are gitignored")
seeds = [os.path.join(ROOT, f) for f in WANT + ["private_log.json"]]
fresh = [p for p in seeds if not os.path.exists(p)]
for p in fresh:
    open(p, "w").write("{}")
try:
    p = subprocess.run([sys.executable, os.path.join(ROOT, "check_publish.py"), "--stage"],
                       capture_output=True, text=True, cwd=ROOT)
    staged = p.stdout.splitlines()
    check(p.returncode == 0 and not any(f.endswith(".enc") or f == "private_log.json" for f in staged),
          "check_publish --stage never lists a .enc file or private_log.json")
finally:
    for p in fresh:
        os.remove(p)

if fails:
    print(f"\nFAILED {len(fails)} check(s)")
    sys.exit(1)
print("\nstate_crypt: all checks passed")

#!/usr/bin/env python3
"""Public Actions logs carry no monitoring detail (2026-09-27).

Anyone can read this public repo's Actions logs. The README invariant is that
monitoring status/counts and detector internals are private (digest-only), so
inside Actions (GITHUB_ACTIONS=true) the detector-side entry points print only
number-free status lines; the detail goes to its private destination
(digest / state.warn / guard_private.json) and, for lines that used to exist
only in the log, to private_log.json (encrypted Actions cache).

Runs the entry points the workflows run, in a throwaway copy of the tree with
no Telegram credentials (alerts/notify in --dry-run; ledger_watch needs the
network and is covered statically):

  (a) GITHUB_ACTIONS=true: stdout+stderr (and GITHUB_OUTPUT) contain none of
      the deny list; ::error::/::warning:: annotations carry no digits
  (b) self-test, same runs WITHOUT GITHUB_ACTIONS: the detail IS printed —
      proves a switch, not a deletion
  (c) the withheld detail landed in private_log.json (routed, not dropped)
  (d) static: only privlog.py reads GITHUB_ACTIONS; no bare print() in the
      detector modules names a deny-listed term

  python3 tests/test_quiet_logs.py      (no network, ~15 s)
"""
import ast, glob, json, os, re, shutil, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRIVATE = {"alert_state.json", "guard_private.json", "private_log.json", "private_labels.json",
           "wallet_mind_map.csv"}
DENY = ["bleed", "repeat share", "crossed", "cap probe", "creators 1h", " units", "runaway", "last12",
        "top3", "first-ever", "at cap", "tier warn", "banked", "flagged", "monitored", "warn queued",
        "warn [", "consecutive failure", "promote at", "fences:", "edge:", "anomaly pass",
        "private labels", "mind wallets", "run duration", "creator rewards", "lag ", "min old"]
DENY_RE = [re.compile(r"heartbeat[^\n]*\d")]
# lines that must appear locally (the switch works) and in private_log.json
DETAIL = ["fences:", "edge: first-ever wallets", "creator rewards", "cap probe:", "grant bleed (private)",
          "cap detector heartbeat:", "private anomaly pass"]
DETECTOR_MODULES = ["alerts.py", "cap_detect.py", "fences.py", "sanity.py", "notify.py", "alive_check.py",
                    "ledger_watch.py", "state.py", "refresh.py"]
# reviewed static exemptions: public crawl/data-quality prints that merely
# share a word with the deny list (module, phrase in the print)
STATIC_OK = {("refresh.py", "newest banked block")}
fails = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def make_tree(d):
    for name in os.listdir(ROOT):
        src = os.path.join(ROOT, name)
        if name in PRIVATE or name.endswith(".enc") or name.startswith(".") or name == "__pycache__":
            continue
        if os.path.isdir(src):
            os.symlink(src, os.path.join(d, name))       # shards: read-only here
        else:
            shutil.copy2(src, os.path.join(d, name))
    open(os.path.join(d, "guard_private.json"), "w").write("{}")


def run_all(d, actions):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("TELEGRAM_", "LEDGER_", "GITHUB_", "STATE_KEY"))}
    gh_out = os.path.join(d, "gh_output.txt")
    if actions:
        env["GITHUB_ACTIONS"] = "true"
        env["GITHUB_OUTPUT"] = gh_out
    out = []
    for cmd in (["sanity.py", "--offline"], ["alerts.py", "--dry-run"], ["notify.py", "hourly", "--dry-run"],
                ["alive_check.py"]):
        p = subprocess.run([sys.executable] + cmd, cwd=d, env=env, capture_output=True, text=True, timeout=300)
        check(p.returncode == 0, f"{'actions' if actions else 'local'}: {' '.join(cmd)} exits 0"
              + ("" if p.returncode == 0 else f" (rc {p.returncode}: {(p.stdout + p.stderr)[-400:]})"))
        out.append(p.stdout + p.stderr)
    # a dead send path + stale heartbeat: the annotations must still be number-free
    st_path = os.path.join(d, "alert_state.json")
    st = json.load(open(st_path))
    st["send_health"] = {"alerts": {"consec_fail": 5, "sent": ["2026-09-01T00:00"], "failed": []}}
    st["cap_probe"] = {**(st.get("cap_probe") or {}), "ts": "2026-09-01T00:00"}
    json.dump(st, open(st_path, "w"))
    p = subprocess.run([sys.executable, "alive_check.py"], cwd=d, env=env, capture_output=True, text=True)
    check(p.returncode == 1, f"{'actions' if actions else 'local'}: alive_check goes red on a dead send path")
    out.append(p.stdout + p.stderr)
    if actions and os.path.exists(gh_out):
        out.append(open(gh_out).read())
    return "\n".join(out)


def leaks(text):
    low = text.lower()
    hits = [t for t in DENY if t in low] + [r.pattern for r in DENY_RE if r.search(low)]
    for ln in text.splitlines():
        if ln.startswith(("::error::", "::warning::")) and not ln.startswith("::error::BLOCK") \
                and re.search(r"\d", ln):
            hits.append("annotation with digits: " + ln[:120])
    return hits


with tempfile.TemporaryDirectory() as d:
    make_tree(d)
    public = run_all(d, actions=True)
    hits = leaks(public)
    check(not hits, "actions: public log carries no monitoring detail" + (f" — leaked: {hits}" if hits else ""))
    check("alerts: detectors ran" in public and "SANITY:" in public and "alert liveness:" in public,
          "actions: number-free status lines are still printed")
    try:
        banked = "\n".join(e["line"] for e in json.load(open(os.path.join(d, "private_log.json"))))
    except (FileNotFoundError, ValueError):
        banked = ""
    missing = [t for t in DETAIL if t not in banked]
    check(not missing, "actions: withheld detail is routed to private_log.json"
          + (f" — missing {missing}" if missing else ""))

with tempfile.TemporaryDirectory() as d:
    make_tree(d)
    local = run_all(d, actions=False)
    missing = [t for t in DETAIL if t not in local]
    check(not missing, "local: full detail still printed (switch, not deletion)"
          + (f" — missing {missing}" if missing else ""))
    check(not os.path.exists(os.path.join(d, "private_log.json")), "local: nothing banked to private_log.json")

# (d) static
for path in sorted(glob.glob(os.path.join(ROOT, "*.py")) + glob.glob(os.path.join(ROOT, "tools", "*.py"))):
    name = os.path.relpath(path, ROOT)
    if name != "privlog.py" and "GITHUB_ACTIONS" in open(path).read():
        fails.append(f"{name} reads GITHUB_ACTIONS — use privlog.private_print (one switch)")
        print(f"FAIL {name} reads GITHUB_ACTIONS")
for name in DETECTOR_MODULES:
    src = open(os.path.join(ROOT, name)).read()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print":
            seg = (ast.get_source_segment(src, n) or "").lower()
            bad = [t for t in DENY if t in seg]
            if bad and not any(m == name and ph in seg for m, ph in STATIC_OK):
                fails.append(f"{name}:{n.lineno} bare print() names {bad} — use private_print")
                print(f"FAIL {name}:{n.lineno} bare print() names {bad}")
check(True, "static: one switch (privlog), no deny-listed bare print() in the detector modules")

if fails:
    print(f"\nFAILED {len(fails)} check(s)")
    sys.exit(1)
print("\nquiet logs: all checks passed")

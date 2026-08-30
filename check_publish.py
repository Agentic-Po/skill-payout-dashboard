#!/usr/bin/env python3
"""What may be published, and proof that nothing else got in.

`git add -A` closed the new-state-file outage class (2026-07-19) by staging
everything — which is also how a new private file gets published the first
time someone forgets a .gitignore line. --stage replaces it with an EXPLICIT
allowlist that still notices new files: anything in the working tree that is
neither ignored nor listed gets a loud warning, so the file is seen instead of
silently published or silently dropped.

  python3 check_publish.py --stage   newline-separated list for `git add`
  python3 check_publish.py --scan    leak tripwire; nonzero = do not publish

The allowlist is derived from `git ls-files` and kept explicit in code on
purpose: a pattern nobody typed is a pattern nobody reviewed.
"""
import fnmatch, json, os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))

# Explicit publish allowlist (glob patterns, repo-relative). Everything the
# repo tracked as of 2026-08-30 plus this change's new files.
PUBLISH_EXTRA = [
    "index.html", "legacy.html", "dashboard.html", "template.html", "template_legacy.html",
    "*.py", "tests/*.py",
    ".github/workflows/*.yml",
    ".gitignore", "Makefile",
    "README.md", "DATASETS.md", "HEARTBEAT.md", "RUNBOOK-deadman.md",
    "catalog.json", "data.json", "day_rates.json", "stats_history.json",
    "inflow_labels.json", "stripe_snapshot.json", "posthog_cache.json",
    "swarm_era.json", "swarm_prices.json",
    "transfers_export.csv",
]

# Field names that must never reach a public artifact. Per-wallet detector
# signals are a calibration oracle; the last two are identity leaks.
DENIED = [r'"ent"\s*:', r'"acf"\s*:', r'"burst"\s*:', r'"flags"\s*:',
          r"retired_ledger", r"@gmail", r"@animoca"]

# Every public/derived artifact gets the denied-field scan.
DENIED_TARGETS = ["index.html", "legacy.html", "data.json", "catalog.json",
                  "DATASETS.md", "README.md", "stats_history.json"]


def _git(*args):
    return subprocess.run(["git", "-C", HERE] + list(args),
                          capture_output=True, text=True, check=True).stdout.splitlines()


def _catalog_paths():
    """Public dataset paths from catalog.json. Private entries carry no path
    by construction, so they cannot be staged through this door."""
    p = os.path.join(HERE, "catalog.json")
    if not os.path.exists(p):
        return []
    return [q for e in json.load(open(p)) if e.get("public") for q in e.get("path", [])]


def _allowed(rel, extra_paths):
    if rel in extra_paths:
        return True
    for pat in extra_paths:
        if pat.endswith("/") and rel.startswith(pat):
            return True
    return any(fnmatch.fnmatch(rel, pat) for pat in PUBLISH_EXTRA)


def stage():
    tracked = _git("ls-files")
    untracked = _git("ls-files", "--others", "--exclude-standard")
    extra = _catalog_paths()
    files, unlisted = list(tracked), []
    for rel in untracked:
        (files if _allowed(rel, extra) else unlisted).append(rel)
    # Directory datasets are staged as directories so NEW monthly shards are
    # picked up without this file knowing the month names.
    for p in extra:
        if p.endswith("/") and p not in files:
            files.append(p)
    for rel in sorted(unlisted):
        print(f"WARNING: {rel} is neither ignored nor in the publish allowlist "
              f"— not staged. Add it to PUBLISH_EXTRA or .gitignore.", file=sys.stderr)
    print("\n".join(dict.fromkeys(files)))
    return 0


def _registry_addrs():
    d = json.load(open(os.path.join(HERE, "data.json")))
    return {r["addr"].lower() for r in d.get("registry", [])}, json.dumps(d.get("registry", []))


def _guard_addrs():
    p = os.path.join(HERE, "guard_private.json")
    if not os.path.exists(p):
        return set()
    g = json.load(open(p))
    return {r["addr"].lower() for r in g.get("rows", []) if r.get("addr", "").startswith("0x")}


def scan():
    bad = []
    for rel in DENIED_TARGETS:
        p = os.path.join(HERE, rel)
        if not os.path.exists(p):
            continue
        text = open(p, errors="replace").read()
        for pat in DENIED:
            if re.search(pat, text):
                bad.append(f"denied field {pat!r} found in {rel}")
    # Address scan: DERIVED/AGGREGATE artifacts only. Raw row exports
    # (transfers_export.csv, the shards) legitimately contain every recipient,
    # including flagged ones — the payout itself is public on-chain. What must
    # never happen is a flagged-but-unregistered wallet being singled out in an
    # aggregate the page presents as "who matters here".
    reg_addrs, reg_text = _registry_addrs()
    targets = [("data.json:registry", reg_text)]
    for rel in ("DATASETS.md", "README.md", "catalog.json"):
        p = os.path.join(HERE, rel)
        if os.path.exists(p):
            targets.append((rel, open(p, errors="replace").read()))
    for a in _guard_addrs() - reg_addrs:
        for name, text in targets:
            if a in text.lower():
                bad.append(f"guard_private address {a} surfaced in {name}")
    for b in bad:
        print(f"::error::{b}")
    print("check_publish --scan:", "FAIL" if bad else "ok")
    return 1 if bad else 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "--stage":
        raise SystemExit(stage())
    if mode == "--scan":
        raise SystemExit(scan())
    raise SystemExit("usage: check_publish.py --stage|--scan")

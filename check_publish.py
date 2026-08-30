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
purpose: a pattern nobody typed is a pattern nobody reviewed. Globs are
anchored: `*.py` matches ROOT-level python only (fnmatch does not treat "/"
specially, so an unanchored `*.py` would silently allowlist
`.creds_probe/keys.py`), and dataset directories are expanded to their
concrete files — stage() never emits a bare directory, because `git add
transfers/` sweeps in every file under the tree whether or not the allowlist
approved it, which is the `add -A` failure mode this file exists to close.

WHAT THE ADDRESS SCAN IS FOR (revised 2026-08-30 after QA).
The earlier rule was "a guard_private address must not appear in a public
artifact". Applied to index.html / legacy.html / data.json as a whole that
rule is WRONG, and loudly so: `facts.top_recipients` and `infer.creators` rank
counterparties by USD received, which is an on-chain fact anyone can recompute
from the very shards this repo publishes. Redacting a wallet from that ranking
would not hide anything — and 18 of the 411 monitored wallets are in it purely
because they receive a lot of MOCA.

The real secret is not WHICH addresses exist, it is WHICH ADDRESSES WE ARE
WATCHING AND WHAT THE DETECTOR THINKS OF THEM. guard_private.json's per-wallet
`ent`/`acf`/`burst`/`flags`/`status` row IS the calibration oracle; the address
alone is not. So the invariant enforced here is:

  no public artifact may reveal an address's MONITORING STATUS.

Two complementary gates implement it:
  (a) address scan, on CURATED surfaces only — DATASETS.md, README.md,
      catalog.json and data.json's registry block. Those are hand- or
      narrative-generated: an address appearing there implies somebody chose
      to single it out, which is itself curation context. Exemptions come from
      publish_allow_addrs.txt, a hand-maintained reviewed file — NOT from
      data.json:registry, which refresh.py auto-extends with top recipients
      and would therefore let a wallet exempt itself.
  (b) status-adjacency scan, on EVERY public artifact including index.html,
      legacy.html, the whole of data.json and transfers_export.csv: an address
      appearing near a detector field or a review/flagged status token is a
      leak of monitoring status regardless of which file it lands in.
"""
import fnmatch, json, os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))

# Explicit publish allowlist (glob patterns, repo-relative). Everything the
# repo tracked as of 2026-08-30 plus this change's new files. Patterns are
# matched against the whole repo-relative path, so a pattern without a "/"
# only ever matches a ROOT-level file.
PUBLISH_EXTRA = [
    "index.html", "legacy.html", "dashboard.html", "template.html", "template_legacy.html",
    "*.py", "tests/*.py",
    ".github/workflows/*.yml",
    ".gitignore", "Makefile",
    "README.md", "DATASETS.md",
    "CONSUMERS.md", "HEARTBEAT.md", "RUNBOOK-deadman.md",
    "catalog.json", "data.json", "day_rates.json", "stats_history.json",
    "stripe_snapshot.json", "posthog_cache.json",
    "swarm_era.json", "swarm_prices.json",
    "transfers_export.csv", "publish_allow_addrs.txt",
]

# Field names that must never reach a public artifact. Per-wallet detector
# signals are a calibration oracle; the last two are identity leaks. Key
# position is matched with optional quoting: index.html is a rendered
# template, so `{ent:0.42}` and `{'ent':0.42}` are as much a leak as the
# double-quoted JSON form.
def _key(k):
    return r'(?<![A-Za-z0-9_])["\']?' + k + r'["\']?\s*:'


DENIED = [_key("ent"), _key("acf"), _key("burst"), _key("flags"),
          r"retired_ledger", r"@gmail", r"@animoca",
          # schema v2: the CSV label column is gone and must stay gone.
          r"counterparty_label"]

# Known-leaked person names (Cycle-3 Loop 1). Case-insensitive, assembled
# from parts so this scanner file is never itself a grep hit for the names it
# polices. Specific names only — NO general identity token lists here (a
# vocabulary scan would deadlock CI on legitimate prose).
LEAKED_NAME_RES = [re.compile("ja" + "son" + r"\s+o" + "ng", re.I),
                   re.compile("kat" + "herine" + r"\s+w" + "ebb", re.I)]

# Every public/derived artifact gets the denied-field scan.
DENIED_TARGETS = ["index.html", "legacy.html", "data.json", "catalog.json",
                  "DATASETS.md", "README.md", "stats_history.json",
                  "transfers_export.csv"]

# Tokens that betray monitoring status when they sit next to an address.
STATUS_TOKENS = [r'["\']?ent["\']?\s*:', r'["\']?acf["\']?\s*:',
                 r'["\']?burst["\']?\s*:', r'["\']?flags["\']?\s*:',
                 r'["\']?status["\']?\s*:\s*["\']?review', r"retired_ledger",
                 r"flagged"]
STATUS_RE = re.compile("|".join(STATUS_TOKENS), re.I)
ADDR_RE = re.compile(r"0x[0-9a-fA-F]{40}")
# One JSON/JS object with no nested braces: the tightest "same record as"
# bracket available without parsing every artifact format.
OBJ_RE = re.compile(r"\{[^{}]{0,4000}\}")
NEAR = 200      # chars, for non-object formats (CSV rows, HTML text)


# ---- structural walk of data.json (added 2026-08-30, council item 5) ----
# The regex gates above read the artifacts as TEXT. This one parses data.json
# and rejects detector-shaped keys by STRUCTURE, so a future refresh.py that
# starts emitting a per-day or per-wallet detector field is caught even if its
# text form dodges the regexes.
#
# EXACT key names, never substrings. The substring form of this rule is a trap:
# "ent" is inside "top_recipients", "tol" is inside "total_mente", "flagged" is
# inside the legitimate public aggregate "flagged_n" — a substring walk would
# fail every clean build on data we deliberately publish.
ORACLE_KEYS = {"ent", "acf", "burst", "tol", "flagged", "flags", "status"}

# Reviewed exact paths where one of the above names is NOT a monitoring
# verdict. Each entry is a deliberate, human-reviewed exemption; a new path
# does not get added here without knowing what it holds.
#   server.diverge_meta.status — narrative status of an OPEN ITEM (the
#     PostHog/Stripe reconciliation blocker), no wallet and no detector value.
ORACLE_KEY_ALLOW = {"server.diverge_meta.status"}


def _oracle_keys(obj, path="", hits=None):
    """Exact-name detector keys anywhere in the parsed payload.

    Per-day rows (facts.daily[]) and per-wallet rows (infer.creators[],
    facts.top_recipients[]) are the contexts that matter, but the walk is
    deliberately whole-document: a detector field is oracle-class wherever it
    lands, and an allowlist of reviewed paths is cheaper to audit than a
    heuristic for what counts as a "row"."""
    if hits is None:
        hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if k in ORACLE_KEYS and p not in ORACLE_KEY_ALLOW:
                hits.append(f"detector-shaped key {k!r} at data.json:{p}")
            _oracle_keys(v, p, hits)
    elif isinstance(obj, list):
        for v in obj:
            # list index is not part of the path: rows are interchangeable and
            # an index-bearing allowlist would rot on the next refresh
            _oracle_keys(v, path + "[]", hits)
    return hits


# ---- structural label tripwire (Cycle-3 Loop 1, 2026-08-30) ----
# Identity labels moved private in schema v2. From here on, ANY non-empty
# label/role/note/counterparty_label value sharing a record with an
# 0x-address in data.json/catalog.json is a leak, UNLESS it is one of the
# reviewed strings below. Two reviewed classes:
#
#   1. The four flow-chart wallet names Po deliberately publishes (owner
#      decision — treasury, collector, rebate, gas funder). Exact role
#      strings, so a reworded role is a reviewed change here too.
#   2. The structural placeholder vocabulary refresh.py emits — identity-free
#      BY CONSTRUCTION (letters, counters, token/contract descriptions; no
#      owners, teams, employee names or custodian products).
#
# Adding a string here is the ONLY way to publish a new label next to an
# address; that is the point.
FLOWCHART_WALLET_LABELS = {
    "Treasury Distribution wallet — the subject of this dashboard",
    "Cognition Credits collector — minds pay MENTE here per request; recycled to treasury until 2026-06-18, now swept to the holding wallet below",
    "Cognition Credits collector — also the original SWARM-era treasury+collector hub (pre-Apr 2026)",
    "Minds Rebate wallet — receives the daily 40% MENTE sweep from the collector since 2026-06-19; DATops swaps its MENTE to MOCA on a weekly cadence",
    "Gas funder — sends ETH slivers so cognition spends are gasless for users",
    "gas funder (sends ETH to mind wallets for cognition spends)",
}
STRUCTURAL_LABELS = {
    "creator wallet",
    "known mimic — do not copy",
    "MENTE token contract — the current cognition credit",
    "MOCA token contract — counted by USD value, auto-swaps to MENTE",
    "SWARM token contract — generation-1 credit (Ethoswarm), migrated ~Apr 2026",
    "SWARM token contract (original Cognition Credit token, migrated to MENTE ~Apr 2026)",
    "AgentIdentity registry — ERC-8004 era (historic, economically inert)",
    "AgentIdentity registry (historic, ERC-8004 era)",
    "registration-era gas funder (historic)",
    "Registration-era gas funder (historic)",
    "MENTE price-oracle pool — base leg",
    "MOCA/MENTE pool — LP'd by treasury; price oracle",
    "EIP-7702 delegator implementation the treasury EOA delegates to",
    "Swap counterparty — took 72k MENTE, returned 112k MOCA; venue unconfirmed",
}
STRUCTURAL_LABEL_RES = [
    re.compile(r"^Funding wallet [A-Z]{1,2}$"),
    re.compile(r"^Funding wallet [A-Z]{1,2} — manual MOCA top-ups$"),
    re.compile(r"^Funding wallet [A-Z]{1,2} — primary MENTE funder$"),
    re.compile(r"^Funding wallet [A-Z]{1,2} — early MENTE funder \(Apr(–May)? 2026\)$"),
    re.compile(r"^(Top recipient|Inflow source) — \$[\d,]+ over [\d,]+ transfers · unlabeled$"),
]
LABEL_KEYS = ("label", "role", "note", "counterparty_label")
# Reviewed path exemptions (same discipline as ORACLE_KEY_ALLOW):
#   scope.note — methodology prose next to scope.wallet; identity-free after
#     the v2 redaction, and still covered by the text scans above.
LABEL_KEY_ALLOW = {"scope.note"}


def _label_ok(v):
    return (v in FLOWCHART_WALLET_LABELS or v in STRUCTURAL_LABELS
            or any(rx.match(v) for rx in STRUCTURAL_LABEL_RES))


def _label_leaks(obj, src, path="", hits=None):
    """Non-empty label-class value co-resident (same record) with an address."""
    if hits is None:
        hits = []
    if isinstance(obj, dict):
        has_addr = any(isinstance(v, str) and ADDR_RE.search(v) for v in obj.values())
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if (has_addr and k in LABEL_KEYS and isinstance(v, str) and v.strip()
                    and p not in LABEL_KEY_ALLOW and not _label_ok(v)):
                hits.append(f"unreviewed {k!r} next to an address at {src}:{p}: {v[:80]!r}")
            _label_leaks(v, src, p, hits)
    elif isinstance(obj, list):
        for v in obj:
            _label_leaks(v, src, path + "[]", hits)
    return hits


def _retired_leak(texts):
    """Retired-straggler tx hashes / entries[] structure must never publish.

    Scoped per the 2026-08-30 QA finding: NOT a blanket address ban. Straggler
    recipients are ordinary payout recipients that legitimately appear in
    top_recipients and the registry, so banning their bare addresses would
    false-positive on clean builds. What must not leak is the per-entry
    identification — the tx hash, and the entries[] list itself."""
    p = os.path.join(HERE, "guard_private.json")
    if not os.path.exists(p):
        return []
    led = (json.load(open(p)) or {}).get("retired_ledger") or {}
    hashes = {h.get("tx", "").lower() for v in led.values()
              for h in v.get("entries", []) if h.get("tx")}
    hits = []
    for name, text in texts:
        low = text.lower()
        for h in hashes:
            if h and h in low:
                hits.append(f"retired-straggler tx {h[:12]}… surfaced in {name}")
        if re.search(r'["\']?entries["\']?\s*:', text):
            hits.append(f"retired_ledger entries[] structure surfaced in {name}")
    return hits


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


# A monthly shard, and nothing else, may appear inside a dataset directory.
# Same regex shards.load() uses to decide what to READ, so the publish
# surface and the read surface cannot diverge: a file the pipeline would
# ignore is a file the pipeline must not publish either.
SHARD_RE = re.compile(r"\d{4}-\d{2}\.json")


def _expand(paths):
    """Directory dataset entries -> their concrete shard files. Never returns
    a directory: `git add <dir>` would outrun this allowlist, staging whatever
    else has appeared under the tree."""
    out = []
    for p in paths:
        if not p.endswith("/"):
            out.append(p)
            continue
        d = os.path.join(HERE, p)
        if not os.path.isdir(d):
            continue
        out += sorted(p + f for f in os.listdir(d)
                      if os.path.isfile(os.path.join(d, f)) and SHARD_RE.fullmatch(f))
    return out


def _match(rel, pat):
    """fnmatch with "/" made significant: fnmatch's "*" happily crosses
    directory separators, which is how `*.py` allowlisted `.creds_probe/
    keys.py`. A pattern matches only at its own depth, segment by segment."""
    r, p = rel.split("/"), pat.split("/")
    return len(r) == len(p) and all(fnmatch.fnmatch(a, b) for a, b in zip(r, p))


def _allowed(rel, extra_files):
    """extra_files is the EXPANDED catalog file list — exact matches only, so
    a new non-dataset file dropped into transfers/ is not auto-allowed."""
    if rel in extra_files:
        return True
    return any(_match(rel, pat) for pat in PUBLISH_EXTRA)


def stage():
    tracked = _git("ls-files")
    untracked = _git("ls-files", "--others", "--exclude-standard")
    extra = _expand(_catalog_paths())
    files, unlisted = list(tracked), []
    for rel in untracked:
        (files if _allowed(rel, extra) else unlisted).append(rel)
    # New monthly shards are picked up because the catalog's directory entries
    # are expanded to real filenames above — as FILES, one per line.
    for p in extra:
        if p not in files and os.path.exists(os.path.join(HERE, p)):
            files.append(p)
    for rel in sorted(unlisted):
        print(f"WARNING: {rel} is neither ignored nor in the publish allowlist "
              f"— not staged. Add it to PUBLISH_EXTRA or .gitignore.", file=sys.stderr)
    print("\n".join(dict.fromkeys(files)))
    return 0


def _allow_addrs():
    """Hand-maintained reviewed exemptions. Deliberately NOT data.json:
    refresh.py auto-appends top recipients to the registry, so using it would
    let an address exempt itself from the artifact being scanned."""
    p = os.path.join(HERE, "publish_allow_addrs.txt")
    if not os.path.exists(p):
        return set()
    out = set()
    for line in open(p):
        line = line.split("#", 1)[0].strip().lower()
        if line.startswith("0x"):
            out.add(line)
    return out


def _registry_text():
    d = json.load(open(os.path.join(HERE, "data.json")))
    return json.dumps(d.get("registry", []))


def _guard_addrs():
    p = os.path.join(HERE, "guard_private.json")
    if not os.path.exists(p):
        return set()
    g = json.load(open(p))
    return {r["addr"].lower() for r in g.get("rows", []) if r.get("addr", "").startswith("0x")}


def _status_adjacent(rel, text):
    """Any address sharing a record — or 200 chars — with a status token."""
    hits = []
    seen = set()
    for m in OBJ_RE.finditer(text):
        seg = m.group(0)
        if STATUS_RE.search(seg):
            for a in ADDR_RE.findall(seg):
                if a.lower() not in seen:
                    seen.add(a.lower())
                    hits.append(f"monitoring status adjacent to {a} in {rel} (same record)")
    for m in STATUS_RE.finditer(text):
        window = text[max(0, m.start() - NEAR): m.end() + NEAR]
        for a in ADDR_RE.findall(window):
            if a.lower() not in seen:
                seen.add(a.lower())
                hits.append(f"monitoring status adjacent to {a} in {rel} (within {NEAR} chars)")
    return hits


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
        for rx in LEAKED_NAME_RES:
            if rx.search(text):
                bad.append(f"known-leaked person name (pattern {rx.pattern!r}) found in {rel}")
        # (b) status adjacency — on EVERY public artifact, including the raw
        # export and the rendered pages, where a plain address is fine but a
        # detector verdict next to one is not.
        bad += _status_adjacent(rel, text)
    # (a) Address scan: CURATED surfaces only. See the module docstring — an
    # address in a ranked aggregate is an on-chain fact, an address in a
    # hand-written doc or in the catalog is somebody singling it out.
    targets = [("data.json:registry", _registry_text())]
    for rel in ("DATASETS.md", "README.md", "catalog.json", "CONSUMERS.md"):
        p = os.path.join(HERE, rel)
        if os.path.exists(p):
            targets.append((rel, open(p, errors="replace").read()))
    for a in _guard_addrs() - _allow_addrs():
        for name, text in targets:
            if a in text.lower():
                bad.append(f"guard_private address {a} surfaced in {name}")
    # (c) structural walks — exact detector key names, and the label tripwire
    # (any unreviewed label/role/note/counterparty_label next to an address),
    # by parse. data.json and catalog.json are the two parseable public
    # surfaces; index.html embeds a strict superset of data.json.
    _dj = os.path.join(HERE, "data.json")
    if os.path.exists(_dj):
        try:
            _parsed = json.load(open(_dj))
            bad += _oracle_keys(_parsed)
            bad += _label_leaks(_parsed, "data.json")
        except json.JSONDecodeError as e:
            bad.append(f"data.json did not parse for the structural scan: {e}")
    _cj = os.path.join(HERE, "catalog.json")
    if os.path.exists(_cj):
        try:
            bad += _label_leaks(json.load(open(_cj)), "catalog.json")
        except json.JSONDecodeError as e:
            bad.append(f"catalog.json did not parse for the structural scan: {e}")
    # (d) retired-straggler per-entry detail — tx hashes and entries[] only.
    _rt = []
    for rel in ("data.json", "index.html"):
        p = os.path.join(HERE, rel)
        if os.path.exists(p):
            _rt.append((rel, open(p, errors="replace").read()))
    bad += _retired_leak(_rt)
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

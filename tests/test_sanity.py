#!/usr/bin/env python3
"""Sanity gate (sanity.py) — the monitor-of-the-monitor must itself be
proven, tier by tier, against seeded copies of the real tree. Offline, < 20 s.

  (a) the clean tree passes: exit 0, zero BLOCK lines
  (b) a seeded 2x price (the Sep 15 incident shape) BLOCKS: the day rate
      disagrees with its market close beyond MARKET_AGREE and, because the
      published USD was computed at 1x, the USD totals disagree too
  (c) one dropped raw row BLOCKS on the EXACT tier (row count / raw total)
  (d) a one-cent USD drift (the Sep 18-20 incident shape) only LOGS:
      exit 0, one "logged drift" more than the clean run, no BLOCK
  (e) the WARN queue (state.py) cools down per key and drains once
  (f) the runway alert (cap_detect.runway_check) is silent at today's ~9.7 d,
      fires once below 7 d, again below 3 d, and re-arms on recovery
  (g) the private anomaly pass writes only guard_private.json, never a public
      artifact, and its keys are on check_publish's denied list

Each seeded case runs in a temp copy: data.json / day_rates.json /
day_digests.json / transfers are copied (shards are mutated in (c)), the
rest is symlinked. Nothing in the checkout is touched.

  python3 tests/test_sanity.py
"""
import json, os, shutil, subprocess, sys, tempfile
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

COPY = ("data.json", "day_rates.json", "day_digests.json", "DATASETS.md")
LINK = ("transfers_in",)


def _tree():
    d = tempfile.mkdtemp(prefix="sanity_")
    for f in COPY:
        if os.path.exists(os.path.join(ROOT, f)):
            shutil.copy(os.path.join(ROOT, f), d)
    shutil.copytree(os.path.join(ROOT, "transfers"), os.path.join(d, "transfers"))
    for f in LINK:
        os.symlink(os.path.join(ROOT, f), os.path.join(d, f))
    return d


def _run(root):
    p = subprocess.run([sys.executable, os.path.join(ROOT, "sanity.py"), "--offline", "--root", root],
                       capture_output=True, text=True, cwd=ROOT)
    out = p.stdout.splitlines()
    summary = next((ln for ln in out if ln.startswith("SANITY:")), "")
    blocks = [ln for ln in out if ln.startswith("::error::")]
    logged = int(summary.split("logged drift")[0].split(",")[-1].strip()) if summary else -1
    return p.returncode, summary, blocks, logged, p.stderr


def main():
    D = json.load(open(os.path.join(ROOT, "data.json")))
    gen = datetime.fromisoformat(D["scope"]["generated_iso"].rstrip("Z"))

    # (a) clean
    t = _tree()
    try:
        rc, summary, blocks, logged_clean, err = _run(t)
        assert rc == 0 and not blocks, f"clean tree blocked: rc={rc} {blocks[:3]} {err[-400:]}"
        assert summary.startswith("SANITY:") and "exact ok" in summary, summary
        assert not os.path.exists(os.path.join(t, "guard_private.json")), "gate created a private file from nothing"
        print(f"ok (a) clean tree passes: {summary}")
    finally:
        shutil.rmtree(t, ignore_errors=True)

    # (b) seeded 2x price on the last closed day that has a market close
    t = _tree()
    try:
        dr = json.load(open(os.path.join(t, "day_rates.json")))
        mkt = dr["market_rates"]["MOCA"]
        day = max(d for d in dr["day_rates"]["MOCA"] if d in mkt)
        dr["day_rates"]["MOCA"][day] = round(mkt[day] * 2, 10)
        json.dump(dr, open(os.path.join(t, "day_rates.json"), "w"))
        rc, summary, blocks, _, err = _run(t)
        assert rc == 1 and blocks, f"2x price did not block: {summary} {err[-400:]}"
        price = [b for b in blocks if f"MOCA day rate {day} vs market close" in b]
        assert price, f"no price block for {day}: {blocks[:5]}"
        assert any("out_usd" in b for b in blocks), "USD totals did not block under a 2x day rate"
        print(f"ok (b) seeded 2x MOCA rate on {day} blocks: {price[0][9:90]}…")
    finally:
        shutil.rmtree(t, ignore_errors=True)

    # (c) one dropped row inside the 24h window
    t = _tree()
    try:
        month = gen.strftime("%Y-%m")
        sp = os.path.join(t, "transfers", f"{month}.json")
        rows = json.load(open(sp))
        cut = (gen - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
        idx = next(i for i, r in enumerate(rows) if r["timestamp"][:19] > cut)
        dropped = rows.pop(idx)
        json.dump(rows, open(sp, "w"))
        rc, summary, blocks, _, err = _run(t)
        assert rc == 1, f"dropped row did not block: {summary} {err[-400:]}"
        assert any("24h out_tx" in b for b in blocks), f"row count did not block: {blocks[:5]}"
        assert any("all history out_tx" in b for b in blocks)
        print(f"ok (c) dropped row {dropped['transaction_hash'][:12]}… blocks on EXACT row counts "
              f"({len(blocks)} block lines)")
    finally:
        shutil.rmtree(t, ignore_errors=True)

    # (d) one-cent USD drift on the published 24h window
    t = _tree()
    try:
        D2 = json.load(open(os.path.join(t, "data.json")))
        w = D2["facts"]["windows"][0]
        w["out_usd"] = round(w["out_usd"] + 0.01, 2)
        json.dump(D2, open(os.path.join(t, "data.json"), "w"))
        rc, summary, blocks, logged, err = _run(t)
        assert rc == 0 and not blocks, f"one-cent drift blocked: {blocks[:3]} {err[-400:]}"
        assert logged >= logged_clean, f"cent drift not logged: {logged} vs clean {logged_clean}"
        print(f"ok (d) one-cent 24h out_usd drift: exit 0, {logged} logged drift line(s), no BLOCK")
    finally:
        shutil.rmtree(t, ignore_errors=True)

    # (e) WARN queue semantics on a throwaway state file
    import state
    real_path = state.PATH
    state.PATH = os.path.join(tempfile.mkdtemp(prefix="warnq_"), "alert_state.json")
    try:
        n = datetime(2026, 9, 21, 1, 0)
        assert state.warn("k", "first", n)
        assert not state.warn("k", "again", n + timedelta(hours=5)), "same key inside 6 h was queued"
        assert state.warn("j", "other", n)
        assert [w["key"] for w in state.pending_warns(n + timedelta(hours=1))] == ["k", "j"]
        state.mark_warns_sent(["k"], n + timedelta(hours=1))
        assert [w["key"] for w in state.pending_warns(n + timedelta(hours=1))] == ["j"]
        assert state.warn("k", "later", n + timedelta(hours=7)), "key did not re-arm after the cooldown"
        assert state.pending_warns(n + timedelta(days=3)) == [], "expired WARNs still pending"
        print("ok (e) WARN queue: 6 h per-key cooldown, drains once, expires after 24 h")
    finally:
        shutil.rmtree(os.path.dirname(state.PATH), ignore_errors=True)
        state.PATH = real_path

    # (f) runway edge alert
    import cap_detect as cd
    drv = {"group": "credit_grants", "label": "Credit grants", "share_pct": 91.4}
    st = {}
    n = datetime(2026, 9, 21, 1, 0)
    secs, st = cd.runway_check(9.7, 30265.0, drv, st, n)
    assert not secs, f"runway fired at 9.7 d: {secs}"
    secs, st = cd.runway_check(6.9, 21000.0, drv, st, n)
    assert len(secs) == 1 and "🔴" in secs[0][1] and "Credit grants" in secs[0][1] and "$21,000" in secs[0][1], secs
    secs, st = cd.runway_check(6.5, 20000.0, drv, st, n + timedelta(hours=2))
    assert not secs, "runway <7 re-fired while still below"
    secs, st = cd.runway_check(2.9, 9000.0, drv, st, n + timedelta(hours=3))
    assert len(secs) == 1 and "3 days" in secs[0][1], secs
    secs, st = cd.runway_check(7.5, 23000.0, drv, st, n + timedelta(hours=4))
    assert not secs and not st["runway"]["lt7"]["below"] and not st["runway"]["lt3"]["below"], st
    secs, st = cd.runway_check(6.0, 18000.0, drv, st, n + timedelta(hours=5))
    assert not secs, "runway <7 re-fired inside its 24 h cooldown"
    secs, st = cd.runway_check(6.0, 18000.0, drv, st, n + timedelta(hours=26))
    assert len(secs) == 1, "runway <7 did not re-fire after recovery + cooldown"
    print("ok (f) runway alert: silent at 9.7 d, fires <7 and <3, re-arms on recovery, 24 h cooldown")

    # (g) private-only anomaly pass; denied on every public surface
    import sanity, check_publish
    rows = [{"ts": "2026-09-20T10:00:00", "tok": "MOCA", "val": 1.0, "usd": 3.0, "to": "0x" + "a" * 40,
             "grp": "credit_grants"}] * 3 + \
           [{"ts": "2026-09-19T10:00:00", "tok": "MOCA", "val": 1.0, "usd": 3.0, "to": "0x" + "b" * 40,
             "grp": "credit_grants"}]
    rep, series = sanity.anomaly_pass(rows, gen)
    assert rep["repeat_wallets"] == 1 and rep["buckets"]["2-5"]["wallets"] == 1 and rep["heaviest_grants"] == 3
    assert rep["repeat_share_pct"] == 75.0 and len(series) == 2 and series[-1]["wallets"] == 1
    for k in ("repeat_grants", "daily_grant_wallets", "heaviest_grants"):
        assert k in check_publish.ORACLE_KEYS, f"{k} not on the structural denied list"
        assert any(k in pat for pat in check_publish.DENIED), f"{k} not on the text denied list"
    for rel in ("data.json", "index.html", "coupon_data.json"):
        p = os.path.join(ROOT, rel)
        if os.path.exists(p):
            assert "repeat_grants" not in open(p, errors="replace").read(), f"repeat_grants leaked into {rel}"
    print("ok (g) anomaly pass: buckets/top-20 private, keys denied on every public surface")

    print("test_sanity: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

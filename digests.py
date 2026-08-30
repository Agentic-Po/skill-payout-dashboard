#!/usr/bin/env python3
"""Closed-day digest ledger — "closed days never reprice", mechanically.

Cycle-3 Loop 2, item 3. The README has always CLAIMED that day-pinned rates
make closed days immutable; nothing enforced it. This module seals every
closed UTC day's outflow aggregates under a sha256 and hard-fails the build
if a later run recomputes a different value — the 2026-08-22 market-leg
restatement happened exactly this way, silently.

The record per closed day is deliberately small and PUBLIC-safe (aggregates
only, no rows, no counterparties):

    {day, out_tx, out_usd (to the cent), economy_out_usd, ops_out_usd,
     rates: {token: day-pinned rate used}}

sha256 is taken over the canonical JSON (sorted keys, no whitespace) of that
record. The ledger (day_digests.json) is append-only:

  * unseen day            -> sealed (sha + first_written_iso banked)
  * same sha              -> ok, nothing written
  * DIFFERENT sha         -> DigestMismatch, both values printed — the build
                             HARD FAILS, unless the day is listed as a
                             "## YYYY-MM-DD" heading in RESTATEMENTS.md, in
                             which case the sha is updated and a loud
                             RESTATED line is printed. Documenting the
                             restatement in that file IS the approval gate.
  * sealed day vanished   -> DigestMismatch too: a closed day losing all its
                             rows is a repricing to zero, not a non-event.

refresh.py calls enforce() right after building the daily facts, feeding the
SAME priced row dicts the page is rendered from — so the ledger can never
diverge from what actually published. The __main__ seeding path below
rebuilds identical rows from the on-disk shards + day_rates.json (the
tests/test_parity.py recipe) so the ledger can be seeded/verified without a
network crawl.

    python3 digests.py --seed    seed/verify the ledger from on-disk data
"""
import hashlib
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER_PATH = os.path.join(HERE, "day_digests.json")
RESTATEMENTS_PATH = os.path.join(HERE, "RESTATEMENTS.md")


class DigestMismatch(Exception):
    """A sealed closed day recomputed to a different digest."""


def day_record(day, day_rows):
    """Canonical aggregate record for one closed day's OUT rows.

    day_rows are refresh.py-style priced dicts: {ts, tok, usd, cat, rate,
    tx, li, ...}. Rows are sorted deterministically before summing so the
    float sums are bit-identical no matter which caller (refresh.py's live
    rows or the offline seeder) produced the list.
    """
    rs = sorted(day_rows, key=lambda r: (r["ts"], str(r.get("tx", "")), str(r.get("li", ""))))
    out = sum(r["usd"] for r in rs)
    eco = sum(r["usd"] for r in rs if r["cat"] != "nonstandard")
    rates = {}
    for r in rs:
        rates.setdefault(r["tok"], r["rate"])
    return {"day": day, "out_tx": len(rs), "out_usd": round(out, 2),
            "economy_out_usd": round(eco, 2), "ops_out_usd": round(out - eco, 2),
            "rates": {k: rates[k] for k in sorted(rates)}}


def day_sha(record):
    """sha256 over the canonical JSON form (sorted keys, no whitespace)."""
    blob = json.dumps(record, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def parse_restatements(path=RESTATEMENTS_PATH):
    """Days approved for restatement: literal '## YYYY-MM-DD' headings."""
    if not os.path.exists(path):
        return set()
    return set(re.findall(r"^##\s+(\d{4}-\d{2}-\d{2})\s*$", open(path).read(), re.M))


def load_ledger(path=LEDGER_PATH):
    if not os.path.exists(path):
        return {}
    led = json.load(open(path))
    return led if isinstance(led, dict) else {}


def _atomic_dump(obj, path):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(obj, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _days_between(day, today):
    import datetime as _dt
    try:
        return (_dt.date.fromisoformat(today) - _dt.date.fromisoformat(day)).days
    except ValueError:
        return 999


def enforce(records, ledger_path=LEDGER_PATH, restatements_path=RESTATEMENTS_PATH,
            now_iso=None):
    """Seal new closed days; hard-fail on any resealed day that changed.

    records: {day: record} from day_record() for every closed day with OUT
    rows this run. Raises DigestMismatch (both shas in the message) on a
    non-restated change; returns the (possibly updated) ledger otherwise.
    """
    led = load_ledger(ledger_path)
    restated = parse_restatements(restatements_path)
    changed = sealed = ok = 0
    # yesterday gets a one-day grace before sealing: indexer lag can add
    # late rows to a just-closed day, and sealing it immediately would turn
    # that into a false "repriced" hard-fail. Already-sealed days still verify.
    today = (now_iso or "")[:10]
    for day in sorted(records):
        sha = day_sha(records[day])
        ent = led.get(day)
        if ent is None:
            if today and _days_between(day, today) < 2:
                continue
            led[day] = {"sha": sha, "first_written_iso": now_iso}
            sealed += 1
        elif ent.get("sha") == sha:
            ok += 1
        elif day in restated:
            print(f"RESTATED {day}: digest {ent.get('sha')} -> {sha} "
                  f"(approved via RESTATEMENTS.md '## {day}')")
            led[day] = {**ent, "sha": sha, "restated_iso": now_iso}
            changed += 1
        else:
            raise DigestMismatch(
                f"closed day {day} repriced: sealed digest {ent.get('sha')} != "
                f"recomputed {sha} — closed days never reprice. Recomputed "
                f"record: {json.dumps(records[day], sort_keys=True)}. If this "
                f"is a deliberate restatement, document it under a '## {day}' "
                f"heading in RESTATEMENTS.md first.")
    # a sealed day that produced NO record this run lost all its rows —
    # that is a repricing to zero, not a non-event
    missing = [d for d in led if d not in records and d not in restated]
    if missing:
        raise DigestMismatch(
            f"sealed closed day(s) {missing} produced no rows this run — a "
            f"vanished day is a repricing to zero. Restore the shard data or "
            f"document a '## <day>' restatement in RESTATEMENTS.md.")
    if sealed or changed:
        _atomic_dump(led, ledger_path)
    print(f"digest ledger: {ok} verified · {sealed} newly sealed · {changed} restated "
          f"({len(led)} closed days total)")
    return led


# ---- offline row builder (seeding / verification without a crawl) ----
# Rebuilds the exact refresh.py pricing from the on-disk artifacts: shards
# for rows, day_rates.json for the pinned rates (mirroring refresh.day_rate's
# closed-day legs: exact day, then carry-forward, then carry-back, then the
# live-rate fallback from data.json), classify.py for the taxonomy. Same
# recipe tests/test_parity.py already trusts.

def _pin(day_rates, day, fallback):
    if day in day_rates:
        return day_rates[day]
    prior = [k for k in sorted(day_rates) if k <= day]
    if prior:
        return day_rates[prior[-1]]
    later = [k for k in sorted(day_rates) if k > day]
    if later:
        return day_rates[later[0]]
    return fallback


def rows_from_disk(root=HERE):
    """refresh.py-equivalent priced OUT rows from shards + day_rates.json."""
    import shards
    from classify import classify_usd
    D = json.load(open(os.path.join(root, "data.json")))
    dr = json.load(open(os.path.join(root, "day_rates.json")))
    toks = {a.lower(): s for s, a in D["scope"]["tokens"].items()}
    rates = {sym: dict(dr["day_rates"].get(sym, {})) for sym in toks.values()}
    today = D["scope"]["generated_iso"][:10]
    # today's provisional open-day rate — same legs refresh.day_rate walks
    for sym, od in (dr.get("open_day_rate") or {}).items():
        rates.setdefault(sym, {}).setdefault(od["d"], od["rate"])
    rows = []
    for i in shards.load(os.path.join(root, "transfers")):
        sym = toks.get(i["token"]["address_hash"].lower())
        if not sym:
            continue
        dec = int(i["total"].get("decimals") or 18)
        val = int(i["total"]["value"]) / 10 ** dec
        ts = i["timestamp"][:19]
        rate = _pin(rates.get(sym, {}), ts[:10], D["facts"]["rate"].get(sym) or 0)
        usd = val * rate
        coarse, fine, _ = classify_usd(usd)
        rows.append({"ts": ts, "tok": sym, "val": val, "rate": rate, "usd": usd,
                     "cat": coarse, "fine": fine, "tx": i["transaction_hash"],
                     "li": i.get("log_index", "")})
    return rows, today


def records_for_closed_days(rows, today):
    """{day: record} for every closed UTC day (< today) with OUT rows."""
    by_day = {}
    for r in rows:
        d = r["ts"][:10]
        if d < today:
            by_day.setdefault(d, []).append(r)
    return {d: day_record(d, rs) for d, rs in by_day.items()}


if __name__ == "__main__":
    import sys
    from datetime import datetime, timezone
    if "--seed" not in sys.argv:
        raise SystemExit("usage: digests.py --seed   (refresh.py calls enforce() directly)")
    rows, today = rows_from_disk()
    recs = records_for_closed_days(rows, today)
    try:
        enforce(recs, now_iso=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    except DigestMismatch as e:
        raise SystemExit(f"FATAL: {e}")

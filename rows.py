#!/usr/bin/env python3
"""One canonical row shape across every on-chain source this repo reads.

Four datasets describe the same chain in four layouts (Blockscout-slim
shards, pre-slimmed cognition rows, moca-ledger's daily jsonl). Anything that
wants to compare two of them — the weekly reconciliation, an audit, a future
merge — had to re-learn each layout. canonical_rows() is the ONE adapter, in
the spirit of classify.py being the one classifier.

This module reads; it never fetches and never writes. Importing it is free.
"""
import json, os
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

CHAIN = "base"
TREASURY = "0xbd956171f5b50936f0ad1c4db80c022bd2442519"      # lowercase, see _lc


def _token_map():
    """addr(lower) -> symbol, taken from data.json's scope block — which
    refresh.py writes straight from its TOKENS constant. Importing refresh.py
    would run the whole network crawl, so the contract file is the source."""
    d = json.load(open(os.path.join(HERE, "data.json")))
    return {a.lower(): s for s, a in d["scope"]["tokens"].items()}


def _lc(a):
    # Addresses are lowercased here because casing differs BY SOURCE:
    # Blockscout returns EIP-55 checksummed, the eth_getLogs fallback and
    # moca-ledger return lowercase. Set comparisons across sources are only
    # meaningful on one casing. Consumers that need a checksummed address for
    # display read it off the source rows, not off this adapter.
    return (a or "").lower()


def _epoch(iso):
    return int(datetime.fromisoformat(iso[:19]).replace(tzinfo=timezone.utc).timestamp())


def _row(token, block, ts_epoch, tx, li, frm, to, wei, src):
    return {"chain": CHAIN, "token": token, "block": block, "ts_epoch": ts_epoch,
            "tx_hash": tx, "log_index": int(li), "from_addr": _lc(frm),
            "to_addr": _lc(to), "value_wei": int(wei), "source_dataset": src}


def canonical_rows(source, path=None):
    """Yield canonical rows for one source.

    source: "treasury_out" | "treasury_in" | "cognition_in" | "moca_ledger"
    path:   required for "moca_ledger" (the clone's data/ directory);
            ignored otherwise.

    PRECISION CAVEAT (cognition_in): those rows are pre-slimmed and carry
    `val` as a FLOAT token amount, not the on-chain integer — the wei value is
    reconstructed as int(val * 1e18) and is therefore accurate to ~float64
    precision (~1e-4 MENTE on a 1e5-sized amount), NOT exact. Never use
    cognition_in wei for an equality reconciliation; use it for sums only.
    """
    import shards
    if source == "moca_ledger":
        if not path:
            raise ValueError("moca_ledger needs the clone's data/ path")
        yield from _moca_ledger(path)
        return
    toks = _token_map()
    if source in ("treasury_out", "treasury_in"):
        d = "transfers" if source == "treasury_out" else "transfers_in"
        for i in shards.load(os.path.join(HERE, d)):
            sym = toks.get(_lc(i["token"]["address_hash"]))
            if not sym:
                continue                       # untracked token (airdrop/spam)
            yield _row(sym, i.get("block_number", 0), _epoch(i["timestamp"]),
                       i["transaction_hash"], i["log_index"],
                       i["from"]["hash"], i["to"]["hash"], i["total"]["value"], source)
    elif source == "cognition_in":
        for c in shards.load(os.path.join(HERE, "cognition_in"), ts_key="ts"):
            yield _row("MENTE", 0, _epoch(c["ts"]), c["tx"], c["log_index"],
                       c["from"], "0xd85096faec1ac03075667b4c1a1661f5623bf111",
                       int(c["val"] * 1e18), source)
    else:
        raise ValueError(f"unknown source {source!r}")


def _moca_ledger(data_dir):
    """moca-ledger daily jsonl: block, ts (epoch), tx, li, from, to, value (wei
    string). MOCA-only by construction — the crawler watches one contract."""
    for fn in sorted(os.listdir(data_dir)):
        if not fn.endswith(".jsonl"):
            continue
        with open(os.path.join(data_dir, fn)) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                yield _row("MOCA", r["block"], r["ts"], r["tx"], r["li"],
                           r["from"], r["to"], r["value"], "moca_ledger")


class StaleData(Exception):
    """Raised by require_fresh. Write/send paths let it kill the process;
    read-only consumers may catch it and degrade."""


def require_fresh(catalog_path, dataset_name, max_age_hours, field="coverage.to"):
    """Generalises notify.py's refusal: publishing a stale figure is worse
    than publishing none. Compares the dataset's measured coverage.to against
    now and raises on stale, missing or unmeasured.

    field="generated_iso" checks BUILD time instead of newest-row time. Both
    are needed and they answer different questions: coverage.to catches "the
    chain stopped reaching us", generated_iso catches "the pipeline stopped
    running". A quiet hour on-chain is not a pipeline failure, so consumers
    that only care whether the pipeline is alive (notify.py) pass
    generated_iso; consumers that need recent CHAIN data use the default.

    Returns the age in hours on success.
    """
    try:
        entries = json.load(open(catalog_path))
    except (FileNotFoundError, json.JSONDecodeError) as e:
        raise StaleData(f"catalog {catalog_path} unreadable ({e}) — refusing")
    e = next((x for x in entries if x.get("name") == dataset_name), None)
    if e is None:
        raise StaleData(f"dataset {dataset_name!r} not in catalog — refusing")
    to = (e.get("coverage") or {}).get("to") if field == "coverage.to" else e.get(field)
    if not to:
        raise StaleData(f"dataset {dataset_name!r} has no measured {field} — refusing")
    to = to.rstrip("Z")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ts = datetime.fromisoformat(to[:19]) if "T" in to else datetime.fromisoformat(to + "T00:00:00")
    age_h = (now - ts).total_seconds() / 3600
    if age_h > max_age_hours:
        raise StaleData(f"dataset {dataset_name!r} stale: {field} = {to} "
                        f"({age_h:.1f}h old, limit {max_age_hours}h)")
    return age_h

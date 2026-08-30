#!/usr/bin/env python3
"""Monthly-sharded JSON caches (<name>/<YYYY-MM>.json).

Replaces the old single-file caches (transfers.json et al.) after
transfers.json hit GitHub's 100 MB file limit on 2026-07-30. Two layers of
defense: rows are slimmed to only the fields the pipeline reads, and each
month lives in its own file so no single file grows unbounded.
"""
import json, os, re


class ShardCorrupt(Exception):
    """A shard file failed to parse. Deliberately loud and NAMED (Cycle-3
    Loop 2): a truncated shard — half-written by a killed process, or mangled
    by a bad merge — must stop the pipeline with the offending path in hand,
    never be half-read or silently skipped. The fix is always the same:
    restore the named file from git history (every shard is committed
    hourly), then re-run."""


def _atomic_dump(obj, path):
    """Write JSON via tmp-file-in-same-dir + os.replace (state.py's pattern):
    a kill mid-write leaves the previous complete file, never a truncation."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(obj, fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def slim(i):
    """Keep only the fields consumed downstream, preserving the raw
    Blockscout nesting so consuming code is unchanged."""
    return {"timestamp": i["timestamp"], "transaction_hash": i["transaction_hash"],
            "log_index": i["log_index"], "block_number": i.get("block_number", 0),
            "from": {"hash": i["from"]["hash"]}, "to": {"hash": i["to"]["hash"]},
            "token": {"address_hash": (i.get("token") or {}).get("address_hash", "")},
            # NFT mints have no total.value; they're filtered out downstream by
            # token address, so "0" is never read — it just keeps the shape.
            "total": {"value": (i.get("total") or {}).get("value", "0"),
                      "decimals": (i.get("total") or {}).get("decimals")}}

def month_of(row, ts_key="timestamp"):
    return row[ts_key][:7]

def load(dir_path, ts_key="timestamp"):
    if not os.path.isdir(dir_path):
        return []
    rows = []
    for fn in sorted(os.listdir(dir_path)):
        if re.fullmatch(r"\d{4}-\d{2}\.json", fn):
            p = os.path.join(dir_path, fn)
            try:
                rows += json.load(open(p))
            except json.JSONDecodeError as e:
                raise ShardCorrupt(
                    f"corrupt shard {p}: {e} — restore it from git history "
                    f"(shards are committed hourly), then re-run") from e
    rows.sort(key=lambda r: r[ts_key], reverse=True)
    return rows

def save(dir_path, rows, months=None, ts_key="timestamp"):
    """Write shard files, newest rows first within each shard.
    months=None writes every month present; otherwise only the given ones
    (so hourly refreshes touch only shards that actually gained rows)."""
    os.makedirs(dir_path, exist_ok=True)
    by_m = {}
    for r in rows:
        by_m.setdefault(month_of(r, ts_key), []).append(r)
    for m, rs in by_m.items():
        if months is None or m in months:
            _atomic_dump(rs, os.path.join(dir_path, f"{m}.json"))

def migrate_legacy(dir_path, ts_key="timestamp", do_slim=True):
    """One-time: convert <dir>.json into monthly shards, then delete it."""
    legacy = dir_path.rstrip("/") + ".json"
    if os.path.exists(legacy) and not os.path.isdir(dir_path):
        rows = json.load(open(legacy))
        if do_slim:
            rows = [slim(i) for i in rows]
        save(dir_path, rows, ts_key=ts_key)
        os.remove(legacy)
        print(f"migrated {legacy} -> {dir_path}/ ({len(rows)} rows)")

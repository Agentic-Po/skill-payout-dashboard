#!/usr/bin/env python3
"""Monthly-sharded JSON caches (<name>/<YYYY-MM>.json).

Replaces the old single-file caches (transfers.json et al.) after
transfers.json hit GitHub's 100 MB file limit on 2026-07-30. Two layers of
defense: rows are slimmed to only the fields the pipeline reads, and each
month has a live file plus immutable numbered parts when it grows.
A part is written before the live file is reduced; interrupted rotations
can overlap, but exact log deduplication makes replay safe without data loss.
"""
import json, os, re

SHARD_RE = re.compile(r"(\d{4}-\d{2})(?:-part-(\d{4}))?\.json")
MAX_ACTIVE_BYTES = 32 * 1024 * 1024
PART_BYTES = 16 * 1024 * 1024


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
            json.dump(obj, fh, separators=(",", ":"))
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

def _key(row):
    if "transaction_hash" in row and "log_index" in row:
        return (str(row["transaction_hash"]).lower(), row["log_index"])
    return json.dumps(row, sort_keys=True, separators=(",", ":"))


def _read(path, month, ts_key):
    try:
        with open(path) as fh:
            rows = json.load(fh)
        if not isinstance(rows, list) or any(month_of(r, ts_key) != month for r in rows):
            raise ValueError("invalid month or row array")
        return rows
    except (json.JSONDecodeError, ValueError, TypeError, KeyError) as e:
        raise ShardCorrupt(f"corrupt shard {path}: {e} — restore it from git history, then re-run") from e


def _parts(dir_path, month):
    found = sorted(f for f in os.listdir(dir_path)
                   if SHARD_RE.fullmatch(f) and f.startswith(month + "-part-"))
    expected = [f"{month}-part-{i:04d}.json" for i in range(1, len(found) + 1)]
    if found != expected or found and not os.path.isfile(os.path.join(dir_path, month + ".json")):
        raise ShardCorrupt(f"incomplete shard sequence in {dir_path}/{month}")
    return found


def load(dir_path, ts_key="timestamp"):
    if not os.path.isdir(dir_path):
        return []
    rows, seen = [], {}
    names = sorted(f for f in os.listdir(dir_path) if SHARD_RE.fullmatch(f))
    for month in {f[:7] for f in names}:
        _parts(dir_path, month)
    for fn in names:
        p = os.path.join(dir_path, fn)
        for row in _read(p, fn[:7], ts_key):
            key = _key(row)
            if key in seen:
                if seen[key] != row:
                    raise ShardCorrupt(f"contradictory duplicate log in {p}")
                continue
            seen[key] = row
            rows.append(row)
    rows.sort(key=lambda r: r[ts_key], reverse=True)
    return rows


def save(dir_path, rows, months=None, ts_key="timestamp"):
    """Append bounded immutable parts, then atomically replace the live month.

    Existing parts must remain byte-identical and present in the supplied full
    history. If interrupted after a part write, load() deduplicates the exact
    overlap with the old live month; conflicting records always fail closed.
    """
    os.makedirs(dir_path, exist_ok=True)
    by_m = {}
    for r in rows:
        by_m.setdefault(month_of(r, ts_key), []).append(r)
    for m, rs in by_m.items():
        if months is not None and m not in months:
            continue
        supplied = {}
        for row in rs:
            key = _key(row)
            if key in supplied and supplied[key] != row:
                raise ShardCorrupt(f"contradictory supplied log in {dir_path}/{m}")
            supplied[key] = row
        parts = _parts(dir_path, m)
        for fn in parts:
            for row in _read(os.path.join(dir_path, fn), m, ts_key):
                if supplied.get(_key(row)) != row:
                    raise ShardCorrupt(f"archived log changed or missing: {dir_path}/{fn}")
                del supplied[_key(row)]
        active = sorted(supplied.values(), key=lambda r: r[ts_key], reverse=True)
        # Serialize the whole candidate before writing any file: malformed rows
        # or unserializable values must leave every previous file untouched.
        encoded = [json.dumps(r, separators=(",", ":")).encode() for r in active]
        if any(len(r) + 2 > PART_BYTES for r in encoded):
            raise ShardCorrupt(f"single row exceeds part bound in {dir_path}/{m}")
        size = 2 + sum(len(r) + 1 for r in encoded)
        number = len(parts)
        while size > MAX_ACTIVE_BYTES:
            n, part_size = 0, 2
            for raw in reversed(encoded):
                if part_size + len(raw) + 1 > PART_BYTES:
                    break
                part_size += len(raw) + 1
                n += 1
            # Keep equal timestamps together so stable historical ordering and
            # floating-point accumulation remain identical after rotation.
            while 0 < n < len(active) and active[-n][ts_key] == active[-n-1][ts_key]:
                n -= 1
            number += 1
            if not n or number > 9999:
                raise ShardCorrupt(f"shard capacity exhausted in {dir_path}/{m}")
            _atomic_dump(active[-n:], os.path.join(dir_path, f"{m}-part-{number:04d}.json"))
            active, encoded = active[:-n], encoded[:-n]
            size = 2 + sum(len(r) + 1 for r in encoded)
        _atomic_dump(active, os.path.join(dir_path, f"{m}.json"))

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

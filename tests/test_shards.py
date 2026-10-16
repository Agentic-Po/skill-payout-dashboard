#!/usr/bin/env python3
"""Shard cache safety gate (Cycle-3 Loop 2, item 2).

  1. A truncated shard raises the NAMED ShardCorrupt with the shard path in
     the message — loud and actionable, never a bare JSONDecodeError and
     never a silent partial read.
  2. save() is atomic: no .tmp file is left behind on success, and a write
     that blows up mid-dump (simulated with a non-serializable object)
     leaves the original shard byte-identical and no .tmp behind.

  python3 tests/test_shards.py     (no network, instant)
"""
import json
import os
import shutil
import sys
import tempfile
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import shards


def _tmp_files(d):
    return [f for f in os.listdir(d) if f.endswith(".tmp")]


def main():
    src_dir = os.path.join(ROOT, "transfers")
    src = os.path.join(src_dir, sorted(os.listdir(src_dir))[-1])

    # 1. truncated shard -> ShardCorrupt naming the path
    with tempfile.TemporaryDirectory() as td:
        dst = os.path.join(td, "2026-08.json")
        blob = open(src, "rb").read()
        open(dst, "wb").write(blob[: len(blob) // 2])   # mid-file truncation
        try:
            shards.load(td)
            raise AssertionError("truncated shard loaded without error")
        except shards.ShardCorrupt as e:
            assert dst in str(e), f"ShardCorrupt message lacks the shard path: {e}"
            print(f"ok truncated shard -> ShardCorrupt with path")

    rows = [{"timestamp": "2026-08-01T00:00:00", "v": 1},
            {"timestamp": "2026-08-02T00:00:00", "v": 2},
            {"timestamp": "2026-07-31T00:00:00", "v": 3}]

    # 2a. success path: correct files, no .tmp stragglers, round-trips
    with tempfile.TemporaryDirectory() as td:
        shards.save(td, rows)
        assert sorted(os.listdir(td)) == ["2026-07.json", "2026-08.json"], os.listdir(td)
        assert not _tmp_files(td), f".tmp left behind on success: {_tmp_files(td)}"
        assert len(shards.load(td)) == 3
        print("ok atomic save: no .tmp left on success, round-trips")

        # 2b. failure path: json.dump raises mid-write -> original untouched
        before = open(os.path.join(td, "2026-08.json"), "rb").read()
        poison = [{"timestamp": "2026-08-03T00:00:00", "v": object()}]
        try:
            shards.save(td, poison)
            raise AssertionError("non-serializable row saved without error")
        except TypeError:
            pass
        after = open(os.path.join(td, "2026-08.json"), "rb").read()
        assert after == before, "failed save modified the original shard"
        assert not _tmp_files(td), f".tmp left behind on failure: {_tmp_files(td)}"
        assert len(shards.load(td)) == 3, "failed save changed readable rows"
        print("ok failed save: original shard byte-identical, no .tmp left")

    # Small synthetic bounds exercise repeated growth and an interrupted
    # part-before-live rotation without creating oversized test fixtures.
    growing = [{"timestamp": f"2026-08-{i:02d}T00:00:00", "transaction_hash": f"0x{i:064x}",
                "log_index": 0, "value": "x" * 90} for i in range(1, 21)]
    with tempfile.TemporaryDirectory() as td, patch.object(shards, "MAX_ACTIVE_BYTES", 1200), patch.object(shards, "PART_BYTES", 700):
        shards.save(td, growing[:4])
        original_dump = shards._atomic_dump
        def interrupted(obj, path):
            if path.endswith("2026-08.json"):
                raise OSError("simulated interruption before live replace")
            return original_dump(obj, path)
        try:
            with patch.object(shards, "_atomic_dump", interrupted):
                shards.save(td, growing[:9])
            raise AssertionError("interruption was not simulated")
        except OSError:
            pass
        recovered = shards.load(td)
        assert all(r in recovered for r in growing[:4]), "rotation lost earlier committed rows"
        assert len(recovered) == len({(r['transaction_hash'], r['log_index']) for r in recovered})
        shards.save(td, growing[:9])
        frozen = {f: open(os.path.join(td, f), 'rb').read() for f in os.listdir(td) if '-part-' in f}
        shards.save(td, growing)
        assert shards.load(td) == sorted(growing, key=lambda r:r['timestamp'], reverse=True)
        for f, blob in frozen.items():
            assert open(os.path.join(td, f), 'rb').read() == blob, "archived part was rewritten"
        assert all(os.path.getsize(os.path.join(td, f)) <= (700 if '-part-' in f else 1200) for f in os.listdir(td))
        before = {f: open(os.path.join(td, f), 'rb').read() for f in os.listdir(td)}
        try:
            shards.save(td, growing[1:])
            raise AssertionError("missing archived log accepted")
        except shards.ShardCorrupt:
            pass
        assert before == {f: open(os.path.join(td, f), 'rb').read() for f in os.listdir(td)}
        print("ok bounded repeated rotation: exact rows, immutable parts, interrupted recovery, no archival loss")
        part = next(f for f in os.listdir(td) if '-part-' in f)
        wrong = dict(growing[0], value='conflict')
        with open(os.path.join(td, '2026-08.json'), 'w') as fh:
            json.dump([wrong], fh)
        try:
            shards.load(td)
            raise AssertionError("contradictory overlap accepted")
        except shards.ShardCorrupt:
            pass
        print("ok contradictory overlap fails closed")

    with tempfile.TemporaryDirectory() as td, patch.object(shards, "MAX_ACTIVE_BYTES", 1500), patch.object(shards, "PART_BYTES", 800):
        tied = [dict(r, timestamp=f"2026-08-{(i//2)+1:02d}T00:00:00") for i,r in enumerate(growing[:10])]
        shards.save(td, tied)
        assert shards.load(td) == sorted(tied, key=lambda r:r['timestamp'], reverse=True), "equal-time order changed"
        print("ok equal timestamps retain exact prior ordering across archive boundaries")

    print("test_shards: PASS")


if __name__ == "__main__":
    main()

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

    print("test_shards: PASS")


if __name__ == "__main__":
    main()

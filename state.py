#!/usr/bin/env python3
"""Single writer for alert_state.json (council loop 3, 2026-08-30).

Three scripts used to read-modify-write the file independently; a kill
mid-write could leave truncated JSON that silently reset all dedup state.
All mutations now go through update(), which merges onto the latest on-disk
state and writes atomically (tmp file + os.replace). load() never crashes:
missing or corrupt state degrades to {} — the worst case is one duplicate
alert, never silence and never a traceback in a notification path.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "alert_state.json")


def load():
    try:
        with open(PATH) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def update(mutation):
    """Merge mutation onto the latest on-disk state, atomically."""
    st = load()
    st.update(mutation)
    tmp = PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(st, fh)
    os.replace(tmp, PATH)
    return st

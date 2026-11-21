"""prefs.py -- the few things ProtoProbe remembers about the person (not the PLC setup, that is workspace.py): whether the first-run tour was
shown (any way of closing it counts as seen). A tiny JSON file next to workspace.json, in the user's own profile, never in the repository.

It must never stop the app: a missing, unreadable or newer file just means "defaults", and a write that fails is ignored.
"""
import json
import os

SCHEMA = 1
DEFAULTS = {"tour_done": False}


def path_for(workspace_file):
    return os.path.join(os.path.dirname(workspace_file) or ".", "prefs.json")


def load(path):
    data = dict(DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict) and raw.get("schema", 0) <= SCHEMA:
            for key in DEFAULTS:
                if isinstance(raw.get(key), bool):
                    data[key] = raw[key]
    except (OSError, ValueError):
        pass
    return data


def save(path, data):
    """True if written. Never raises."""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"schema": SCHEMA, **{k: bool(data.get(k, v)) for k, v in DEFAULTS.items()}}, f, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        return False

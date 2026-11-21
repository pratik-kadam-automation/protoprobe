"""The copy-and-paste output JSON built from the tags the engineer ticked on Live values.

    {"deployment_id": ..., "gateway_id": ..., "machine_id": ..., "timestamp": ..., "data": {"machine status": 1, ...}}

A tag is ticked when its dict has a non-empty ``output`` (the name to show in ``data``). Nothing here talks to a PLC or the
network. ``mode`` "values" puts the live value after each name; "names" puts the name itself (a template to paste where the
value will arrive later, e.g. ``"machine status": "machine status"``).
"""
import json
import time

KEYS = ("deployment_id", "gateway_id", "machine_id")


def ticked(connections):
    """[(connection name, tag dict)] for every tag with an output name."""
    out = []
    for conn in connections.values():
        for tag in conn.tags:
            if str(tag.get("output") or "").strip():
                out.append((conn, tag))
    return out


def build_output(ids, connections, mode="values", now=None):
    """Returns (payload dict, warnings list). ``ids`` has deployment_id / gateway_id / machine_id (missing ones stay empty)."""
    warnings = []
    data = {}
    for conn, tag in ticked(connections):
        name = str(tag["output"]).strip()
        if name in data:
            warnings.append(f'The name "{name}" is used by more than one tag; only the last one is shown.')
        if mode == "names":
            data[name] = name
        elif tag["name"] in conn.latest_values:
            data[name] = conn.latest_values[tag["name"]]
        else:
            data[name] = None
            warnings.append(f'No value yet for "{name}" (tag {tag["name"]}).')
    payload = {k: str(ids.get(k) or "").strip() for k in KEYS}
    for k in KEYS:
        if not payload[k]:
            warnings.append(f"{k} is empty (fill it in, or answer it on the Commissioning screen).")
    payload["timestamp"] = int(now if now is not None else time.time())
    payload["data"] = data
    if not data:
        warnings.append("No tag is ticked yet: select rows on Live values and press \"Include in output JSON\".")
    return payload, warnings


def to_text(payload):
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)

"""candidates.py -- the hand-over of a scan: the tags worth looking at for OEE or predictive maintenance, as plain rows (CSV / JSON) that another tool
or a person can use. Pure functions; the app passes in how to spell an address and how to classify a row.

A row says WHAT was seen, not what the tag IS: `evidence` is the behaviour during the scan and `confidence` is deliberately low until a person
confirms it on the machine. Nothing here is a fact about the plant.
"""
import csv
import io
import json

COLUMNS = ["connection", "address", "suggested_name", "data_type", "role", "oee_use", "predictive_use", "evidence",
           "snapshots", "first_value", "last_value", "confidence"]
_ROLE_OF_KIND = {"counter": "counter", "state": "state", "flags": "state", "measurement": "measurement", "constant": "setpoint",
                 "countdown": "measurement", "blink": "ignore", "idle": "ignore", "steady_on": "state", "unknown": ""}


def _safe_name(address):
    return "".join(ch if ch.isalnum() else "_" for ch in address).strip("_")


def rows_from_results(results, protocol_of, spell_address, hint_of, bit_hint_of, only_candidates=True):
    """results: the app's scan result dicts. protocol_of(conn_name) -> protocol; spell_address(protocol, tag) -> 'MD60';
    hint_of(result) -> signal_hints.Hint or None; bit_hint_of(series) -> Hint for one bit's series."""
    rows = []
    for r in results:
        protocol = protocol_of(r["conn_name"])
        tag = r["tag"]
        if r.get("bits_series") and r.get("bit_area"):
            for (byte, bit), series in sorted(r["bits_series"].items()):          # one row per bit that changed
                hint = bit_hint_of({(byte, bit): series})
                address = f"{tag['area']}{byte}.{bit}"
                rows.append(_row(r, address, "bit", hint, series[0], series[-1], len(series), series))
            continue
        hint = hint_of(r)
        values = r.get("values") or [r.get("latest")]
        rows.append(_row(r, spell_address(protocol, tag), tag.get("data_type", ""), hint, values[0], values[-1], len(values), values))
    if only_candidates:
        rows = [x for x in rows if x["oee_use"] or x["predictive_use"]]
    return rows


def _row(r, address, data_type, hint, first, last, snapshots, series=None):
    kind = hint.kind if hint else "unknown"
    return {
        "connection": r["conn_name"], "address": address, "suggested_name": _safe_name(address), "data_type": data_type,
        "role": _ROLE_OF_KIND.get(kind, ""), "oee_use": hint.oee if hint else "", "predictive_use": hint.predictive if hint else "",
        "evidence": hint.text if hint else "", "snapshots": snapshots, "first_value": first, "last_value": last,
        "confidence": "low: guessed from %d snapshot(s), confirm on the machine" % snapshots if snapshots < 20 else "medium: %d snapshots, confirm on the machine" % snapshots,
        "series": list(series) if series is not None else [],      # the values seen during the scan: used for an optional raw excerpt, not part of the CSV / JSON export
    }


def to_csv(rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=COLUMNS, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def to_json(rows, app_version=""):
    clean = [{k: v for k, v in row.items() if k != "series"} for row in rows]
    return json.dumps({"schema": 1, "kind": "protoprobe-candidates", "app_version": app_version, "candidates": clean}, indent=2)

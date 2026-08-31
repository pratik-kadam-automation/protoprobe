#!/usr/bin/env python3
"""
protoprobe.py

Step 2: migrates the working multi-PLC Connections/Tags manager,
Live Values table, and JSON/MQTT Output screen into the sidebar
shell from Step 1. Dashboard now reflects real connections instead
of a placeholder. Network Tools and Settings remain placeholders
for Steps 4-5.
"""

import ast
import math
import struct
try:
    import sv_ttk
    SV_TTK_AVAILABLE = True
except ImportError:
    SV_TTK_AVAILABLE = False
import csv
import json
import operator
import os
import queue
import subprocess
import sys
import threading
import asyncio
import time
import uuid
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

APP_NAME = "ProtoProbe"
APP_VERSION = "0.2.0"
APP_TAGLINE = "Multi-vendor industrial protocol diagnostics and edge gateway configuration"


def resource_path(relative_path: str) -> str:
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


sys.path.insert(0, resource_path("edge-gateway"))
from drivers.modbus_driver import ModbusDriver
from drivers.s7_driver import S7Driver
from drivers.mc_driver import MCProtocolDriver
from drivers.opcua_driver import OPCUADriver
from drivers.bacnet_driver import BACnetDriver

NAV_ITEMS = [
    ("dashboard", "Dashboard"),
    ("connections", "Connections"),
    ("scan_results", "Scan results"),
    ("live_values", "Live values"),
    ("oee", "OEE config"),
    ("output", "Output"),
    ("network", "Network tools"),
    ("settings", "Settings"),
]

DRIVER_FACTORY = {
    "modbus_tcp": lambda cfg: ModbusDriver("conn", cfg, transport="tcp"),
    "modbus_rtu": lambda cfg: ModbusDriver("conn", cfg, transport="rtu"),
    "s7comm": lambda cfg: S7Driver("conn", cfg),
    "mc_protocol_3e": lambda cfg: MCProtocolDriver("conn", cfg),
    "opcua": lambda cfg: OPCUADriver("conn", cfg),
    "bacnet_ip": lambda cfg: BACnetDriver("conn", cfg),
}

PROTOCOL_LABELS = {
    "modbus_tcp": "Modbus TCP",
    "modbus_rtu": "Modbus RTU",
    "s7comm": "S7comm (Siemens)",
    "mc_protocol_3e": "MC Protocol / SLMP (Mitsubishi)",
    "opcua": "OPC UA",
    "bacnet_ip": "BACnet/IP",
}

INTERFACE_FIELDS = {
    "modbus_tcp": [("ip", "192.168.1.50"), ("port", "502")],
    "modbus_rtu": [("port", "COM3"), ("baudrate", "9600"), ("parity", "N"),
                   ("stopbits", "1"), ("bytesize", "8")],
    "s7comm": [("ip", "192.168.1.50"), ("rack", "0"), ("slot", "1")],
    "mc_protocol_3e": [("ip", "192.168.3.250"), ("port", "5010")],
    "opcua": [("endpoint", "opc.tcp://192.168.1.70:4840")],
    "bacnet_ip": [("local_ip", "192.168.1.10"), ("device_instance", "260001"), ("device_ip", "192.168.1.20"),
                  ("remote_device_instance", "0"), ("device_port", "47808")],
}

TAG_FIELDS = {
    "modbus_tcp": [("address", "100"), ("register_type", "holding"), ("data_type", "int16")],
    "modbus_rtu": [("address", "100"), ("register_type", "holding"), ("data_type", "int16")],
    "s7comm": [("area", "DB"), ("db_number", "1"), ("start_byte", "0"), ("bit_offset", "0"), ("data_type", "float32_be")],
    "mc_protocol_3e": [("device_code", "D"), ("device_number", "100"), ("data_type", "int16")],
    "opcua": [("node_id", "ns=2;s=Machine.Tag")],
    "bacnet_ip": [("object_type", "analogInput"), ("object_instance", "1"), ("data_type", "float32")],
}

FIELD_OPTIONS = {
    "data_type": {
        "options": ["int16", "uint16", "int32", "uint32", "float32_be", "float32_le", "bit"],
        "hint": "int16/uint16 = signed/unsigned 16-bit whole number. int32/uint32 = "
                "32-bit whole number. float32 = decimal value (_be/_le = byte order -- "
                "try _be first, switch to _le if the number looks wildly wrong). "
                "bit = a single on/off value (S7 only, used with Bit offset below) -- "
                "typical for I/Q/M areas.",
    },
    "area": {
        "options": ["DB", "I", "Q", "M"],
        "hint": "DB = data block (byte offset, usually word/float values). I = physical "
                "inputs -- usually documented from wiring, so manual entry beats scanning. "
                "Q = physical outputs -- same. M = internal markers/flags -- not tied to "
                "physical wiring, so scanning M can be as useful as scanning a DB. I/Q/M "
                "are typically single bits -- set Data type to 'bit' and Bit offset 0-7.",
    },
    "register_type": {
        "options": ["holding", "input", "coil"],
        "hint": "holding = most PLC data (read/write). input = read-only sensor "
                "registers. coil = single on/off bit.",
    },
    "device_code": {
        "options": ["D", "M", "R", "W"],
        "hint": "D = Data register (most common). M = internal relay/bit. "
                "R = file register. W = link register.",
    },
    "object_type": {
        "options": ["analogInput", "analogOutput", "analogValue", "binaryInput",
                    "binaryOutput", "binaryValue"],
        "hint": "BACnet object type -- matches what's configured on the device.",
    },
}

DEFAULT_TEMPLATE = json.dumps({
    "deviceId": "__DEVICE_ID__",
    "timestamp": "__TIMESTAMP__",
    "metrics": "__METRICS__",
}, indent=2)


BACNET_TYPE_ABBR = {
    "analogInput": "AI", "analogOutput": "AO", "analogValue": "AV",
    "binaryInput": "BI", "binaryOutput": "BO", "binaryValue": "BV",
}


def format_address(protocol: str, tag_fields: dict) -> str:
    """Compact, conventional address notation per protocol -- 'D100'
    instead of 'device_code=D, device_number=100'."""
    if protocol == "mc_protocol_3e":
        return f"{tag_fields.get('device_code', '')}{tag_fields.get('device_number', '')}"
    if protocol in ("modbus_tcp", "modbus_rtu"):
        return f"{tag_fields.get('register_type', '')}[{tag_fields.get('address', '')}]"
    if protocol == "s7comm":
        area = tag_fields.get("area", "DB")
        start_byte = tag_fields.get("start_byte", "")
        base = f"DB{tag_fields.get('db_number', '')}.{start_byte}" if area == "DB" else f"{area}{start_byte}"
        if tag_fields.get("data_type") == "bit":
            base += f".{tag_fields.get('bit_offset', 0)}"
        return base
    if protocol == "opcua":
        return tag_fields.get("node_id", "")
    if protocol == "bacnet_ip":
        obj_type = tag_fields.get("object_type", "")
        abbr = BACNET_TYPE_ABBR.get(obj_type, obj_type)
        return f"{abbr}{tag_fields.get('object_instance', '')}"
    return ", ".join(f"{k}={v}" for k, v in tag_fields.items() if k not in ("name", "data_type"))


def coerce(value):
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def substitute_template(node, sentinel_values):
    if isinstance(node, dict):
        return {k: substitute_template(v, sentinel_values) for k, v in node.items()}
    if isinstance(node, list):
        return [substitute_template(v, sentinel_values) for v in node]
    if isinstance(node, str) and node in sentinel_values:
        return sentinel_values[node]
    return node


# --- safe formula evaluator (OEE screen) --------------------------------------

_ALLOWED_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub,
    ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.Pow: operator.pow, ast.Mod: operator.mod,
}
_ALLOWED_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


class FormulaError(Exception):
    pass


def safe_eval(expr: str, variables: dict) -> float:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise FormulaError(f"Syntax error: {e}")

    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise FormulaError("Only numbers are allowed as literals")
        if isinstance(node, ast.Name):
            if node.id not in variables:
                raise FormulaError(f"Unknown tag name in formula: {node.id}")
            return variables[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
            left = _eval(node.left)
            right = _eval(node.right)
            if isinstance(node.op, ast.Div) and right == 0:
                raise FormulaError("Division by zero")
            return _ALLOWED_BINOPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARY:
            return _ALLOWED_UNARY[type(node.op)](_eval(node.operand))
        raise FormulaError(f"Unsupported expression: {ast.dump(node)}")

    return _eval(tree)


def extract_tag_names(expr: str) -> list:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return []
    return sorted({n.id for n in ast.walk(tree) if isinstance(n, ast.Name)})


# --- domain model --------------------------------------------------------------

# --- background scan workers (shared by single-area and full-PLC scans) -----

SCAN_BATCH_SIZE = 200
SCAN_SAFETY_CEILING = 20000


def build_range_tags(protocol, params):
    """params varies by protocol -- see each branch. Returns a list of
    synthetic int16 tag dicts covering the requested range."""
    if protocol == "mc_protocol_3e":
        code = params["device_code"]
        return [
            {"name": f"{code}{n}", "device_code": code, "device_number": n,
             "data_type": "int16", "multiplier": 1.0, "offset": 0.0, "mqtt_key": f"{code}{n}"}
            for n in range(params["start"], params["end"] + 1)
        ]
    if protocol == "s7comm":
        area = params.get("area", "DB")
        db = params.get("db_number", 1)
        prefix = f"DB{db}." if area == "DB" else area
        return [
            {"name": f"{prefix}{n}", "area": area, "db_number": db, "start_byte": n,
             "data_type": "int16", "multiplier": 1.0, "offset": 0.0, "mqtt_key": f"{prefix}{n}"}
            for n in range(params["start"], params["end"] + 1, 2)
        ]
    # modbus_tcp / modbus_rtu
    rtype = params["register_type"]
    return [
        {"name": f"{rtype}[{a}]", "address": a, "register_type": rtype,
         "data_type": "int16", "multiplier": 1.0, "offset": 0.0, "mqtt_key": f"{rtype}[{a}]"}
        for a in range(params["start"], params["end"] + 1)
    ]


def discover_db_numbers_sync(conn, max_db, out_queue=None):
    """Blocking-safe (runs inside an already-running asyncio.run via a
    nested async function) probe of DB1..max_db. Returns a list of DB
    numbers that responded. Pushes progress to out_queue if given."""
    from drivers.s7_driver import S7Driver

    async def run_probe():
        driver = S7Driver("probe", {"interface": conn.interface, "tags": []})
        await driver.connect()
        loop = asyncio.get_event_loop()
        found = []
        for db in range(1, max_db + 1):
            if out_queue:
                out_queue.put(("status", f"Probing DB{db}..."))
            try:
                await loop.run_in_executor(None, driver.client.db_read, db, 0, 4)
                found.append(db)
            except Exception:
                pass
        await driver.disconnect()
        return found

    return asyncio.run(run_probe())


def type_label_for_tag(protocol, tag):
    """Which device-code/area/register-type this tag belongs to, for
    grouping and filtering scan results."""
    if protocol == "mc_protocol_3e":
        return tag.get("device_code", "?")
    if protocol == "s7comm":
        return tag.get("area", "DB")
    if protocol in ("modbus_tcp", "modbus_rtu"):
        return tag.get("register_type", "?")
    return protocol


def _raw_word_from_int16(decoded_value: int) -> int:
    """Reverses BaseDriver's int16 decode -- gets back the original raw
    16-bit register content from the signed value the scan stored."""
    return int(decoded_value) & 0xFFFF


def _plausible_float(value: float) -> bool:
    if not math.isfinite(value):
        return False
    if value == 0.0:
        return True
    return 1e-6 < abs(value) < 1e7


def guess_float32_pair(current_value, next_value):
    """Given two adjacent addresses' latest int16-decoded values, try
    reading them as one float32 value instead (big-endian first, since
    that's this tool's own default word order). Returns the float if it
    looks like a plausible real-world number, else None. This is a
    heuristic to help you spot likely 32-bit/float tags among what a
    scan assumed were separate 16-bit values -- not a certain answer;
    confirm against the HMI or known behavior the same as any other
    scan result.

    Both-zero pairs are deliberately excluded even though 0x00000000
    is technically a valid float32 (positive zero) -- on any real
    scan, the overwhelming majority of unused/unpopulated register
    pairs are also zero, so flagging every one of them as 'possible
    float32 = 0.0' is just noise that drowns out genuine signal. A
    real float32 tag sitting at exactly 0.0 at scan time is a rare
    enough edge case that it's better caught on a later scan once its
    value has actually moved (and 'Changed = Yes' would flag it then
    anyway)."""
    try:
        hi = _raw_word_from_int16(current_value)
        lo = _raw_word_from_int16(next_value)
        if hi == 0 and lo == 0:
            return None
        raw_be = struct.pack(">HH", hi, lo)
        value_be = struct.unpack(">f", raw_be)[0]
        if _plausible_float(value_be):
            return round(value_be, 4)
        raw_le = struct.pack(">HH", lo, hi)
        value_le = struct.unpack(">f", raw_le)[0]
        if _plausible_float(value_le):
            return round(value_le, 4)
    except (struct.error, OverflowError, ValueError):
        pass
    return None


def compute_32bit_candidates(current_value, next_value):
    """Every 32-bit interpretation this tool's own drivers can actually
    decode -- int32, uint32 (both big-endian word order, matching
    base_driver.decode_value), and float32_be/float32_le. Unlike
    guess_float32_pair, this returns every candidate regardless of how
    'plausible' it looks -- used for search-by-value, where the point
    is an exact numeric match, not a guess about what looks reasonable."""
    candidates = {}
    try:
        hi = _raw_word_from_int16(current_value)
        lo = _raw_word_from_int16(next_value)
        raw_be = struct.pack(">HH", hi, lo)
        candidates["int32"] = struct.unpack(">i", raw_be)[0]
        candidates["uint32"] = struct.unpack(">I", raw_be)[0]

        value_be = struct.unpack(">f", raw_be)[0]
        if math.isfinite(value_be):
            candidates["float32_be"] = round(value_be, 4)

        raw_le = struct.pack(">HH", lo, hi)
        value_le = struct.unpack(">f", raw_le)[0]
        if math.isfinite(value_le):
            candidates["float32_le"] = round(value_le, 4)
    except (struct.error, OverflowError, ValueError):
        pass
    return candidates


def run_range_scan(conn, protocol, tags, num_snapshots, delay_s, job_label, out_queue, stop_event=None):
    """Reads `tags` num_snapshots times (batched, with progress pushed to
    out_queue), diffs them, and pushes one ('results', (conn.name,
    job_label, results_list)) message when this sub-scan finishes.
    Connects and disconnects its own driver -- callers running multiple
    sub-scans call this once per sub-area/device-code sequentially.
    If stop_event is set mid-scan, stops after the current batch and
    still reports whatever was read so far, rather than discarding it."""
    if len(tags) > SCAN_SAFETY_CEILING:
        out_queue.put(("status", f"[{job_label}] skipped -- {len(tags)} addresses is unreasonably large"))
        return

    if not tags:
        out_queue.put(("status", f"[{job_label}] skipped -- empty range"))
        return

    batches = [tags[i:i + SCAN_BATCH_SIZE] for i in range(0, len(tags), SCAN_BATCH_SIZE)]

    if protocol in ("modbus_tcp", "modbus_rtu"):
        group_cfg = {"interface": conn.interface,
                     "devices": [{"unit_id": conn.interface.get("unit_id", 1), "tags": []}]}
    else:
        group_cfg = {"interface": conn.interface, "tags": []}

    driver = DRIVER_FACTORY[protocol](group_cfg)

    def set_batch_tags(batch):
        if protocol in ("modbus_tcp", "modbus_rtu"):
            driver.config["devices"][0]["tags"] = batch
        else:
            driver.config["tags"] = batch

    async def run_scan():
        await driver.connect()
        snapshots = [dict() for _ in range(num_snapshots)]
        stopped_early = False
        for i in range(num_snapshots):
            done_count = 0
            for batch in batches:
                if stop_event is not None and stop_event.is_set():
                    out_queue.put(("status", f"[{job_label}] stopped -- reporting {done_count}/{len(tags)} read so far"))
                    stopped_early = True
                    break
                set_batch_tags(batch)
                values = await driver.poll()
                snapshots[i].update(values)
                done_count += len(batch)
                out_queue.put((
                    "status",
                    f"[{job_label}] snapshot {i + 1}/{num_snapshots} -- {done_count}/{len(tags)} read"
                ))
            if stopped_early:
                break
            if i < num_snapshots - 1:
                await asyncio.sleep(delay_s)
        await driver.disconnect()
        return snapshots

    try:
        snapshots = asyncio.run(run_scan())
    except Exception as e:
        out_queue.put(("status", f"[{job_label}] failed: {e}"))
        return

    results = []
    for tag in tags:
        name = tag["name"]
        vals_present = [snap[name] for snap in snapshots if name in snap]
        if not vals_present:
            continue
        changed = len(set(vals_present)) > 1
        results.append({
            "name": name, "tag": tag, "changed": changed, "latest": vals_present[-1],
            "raw_value": _raw_word_from_int16(vals_present[-1]),
            "type_label": type_label_for_tag(protocol, tag),
        })

    # 32-bit pairing: adjacent entries in `tags` are adjacent addresses
    # by construction (build_range_tags steps by 1 device number or 2
    # bytes), so results[i] and results[i+1] are a valid pair to try as
    # one 32-bit value.
    #
    # The displayed "float32_guess" is only shown when at least one of
    # the pair actually changed across snapshots. A static bit pattern
    # that happens to decode plausibly is indistinguishable from two
    # unrelated static/unused int16 values that coincidentally combine
    # into something plausible-looking -- with thousands of addresses
    # scanned, that coincidence is common, not meaningful. Requiring
    # real movement as corroborating evidence is what actually
    # distinguishes a likely float tag from noise.
    #
    # candidates_32bit (used by Search) is still computed regardless --
    # a deliberate search for a known static setpoint value is a
    # legitimate use case even without observed change.
    latest_by_name = {r["name"]: r["latest"] for r in results}
    changed_by_name = {r["name"]: r["changed"] for r in results}
    result_by_name = {r["name"]: r for r in results}
    for i, tag in enumerate(tags[:-1]):
        name = tag["name"]
        next_name = tags[i + 1]["name"]
        if name in latest_by_name and next_name in latest_by_name:
            result_by_name[name]["candidates_32bit"] = compute_32bit_candidates(
                latest_by_name[name], latest_by_name[next_name]
            )
            has_evidence = changed_by_name.get(name) or changed_by_name.get(next_name)
            if has_evidence:
                guess = guess_float32_pair(latest_by_name[name], latest_by_name[next_name])
                result_by_name[name]["float32_guess"] = guess
                result_by_name[name]["float32_pair_with"] = next_name if guess is not None else None
            else:
                result_by_name[name]["float32_guess"] = None
                result_by_name[name]["float32_pair_with"] = None

    out_queue.put(("results", (conn.name, job_label, results)))


def run_full_plc_scan_job(conn, protocol, options, num_snapshots, delay_s, out_queue, stop_event=None):
    """Runs multiple run_range_scan() calls back to back, covering
    every selected device-code/area/register-type -- the 'scan the
    whole PLC' entry point. Runs entirely in a background thread;
    the caller can close any dialog immediately after starting this.
    Checks stop_event between sub-scans (and run_range_scan itself
    checks it between batches), so Stop takes effect promptly rather
    than only between whole device-codes/areas."""
    max_addr = options["max_address"]

    if protocol == "mc_protocol_3e":
        for code in options["device_codes"]:
            if stop_event is not None and stop_event.is_set():
                out_queue.put(("status", "Stopped -- remaining device codes skipped"))
                break
            tags = build_range_tags(protocol, {"device_code": code, "start": 0, "end": max_addr})
            run_range_scan(conn, protocol, tags, num_snapshots, delay_s, f"{code} (0-{max_addr})", out_queue, stop_event)

    elif protocol == "s7comm":
        if "DB" in options["areas"]:
            out_queue.put(("status", "Discovering DBs before scanning..."))
            try:
                db_list = discover_db_numbers_sync(conn, options.get("max_db_probe", 50), out_queue)
            except Exception as e:
                db_list = []
                out_queue.put(("status", f"DB discovery failed: {e}"))
            if not db_list:
                out_queue.put(("status", "No DBs found to scan"))
            for db in db_list:
                if stop_event is not None and stop_event.is_set():
                    out_queue.put(("status", "Stopped -- remaining DBs skipped"))
                    break
                tags = build_range_tags(protocol, {"area": "DB", "db_number": db, "start": 0, "end": max_addr})
                run_range_scan(conn, protocol, tags, num_snapshots, delay_s, f"DB{db} (0-{max_addr})", out_queue, stop_event)

        for area in ("I", "Q", "M"):
            if stop_event is not None and stop_event.is_set():
                break
            if area in options["areas"]:
                tags = build_range_tags(protocol, {"area": area, "start": 0, "end": max_addr})
                run_range_scan(conn, protocol, tags, num_snapshots, delay_s, f"{area} (0-{max_addr})", out_queue, stop_event)

    else:  # modbus
        for rtype in options["register_types"]:
            if stop_event is not None and stop_event.is_set():
                out_queue.put(("status", "Stopped -- remaining register types skipped"))
                break
            tags = build_range_tags(protocol, {"register_type": rtype, "start": 0, "end": max_addr})
            run_range_scan(conn, protocol, tags, num_snapshots, delay_s, f"{rtype}[0-{max_addr}]", out_queue, stop_event)

    out_queue.put(("job_done", None))


class Connection:
    def __init__(self, name, protocol, interface):
        self.name = name
        self.protocol = protocol
        self.interface = interface
        self.tags = []
        self.thread = None
        self.latest_values = {}
        self.last_updated = {}
        self.status = "idle"


class PollerThread(threading.Thread):
    def __init__(self, conn: Connection, interval_s: float, out_queue: queue.Queue):
        super().__init__(daemon=True)
        self.conn = conn
        self.interval_s = interval_s
        self.out_queue = out_queue
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        asyncio.run(self._run_async())

    async def _run_async(self):
        group_cfg = self._build_driver_config()
        driver = DRIVER_FACTORY[self.conn.protocol](group_cfg)

        try:
            self.out_queue.put(("conn_status", (self.conn.name, "connecting")))
            await driver.connect()
            self.out_queue.put(("conn_status", (self.conn.name, "connected")))
        except Exception as e:
            self.out_queue.put(("conn_status", (self.conn.name, f"error: {e}")))
            return

        while not self._stop_event.is_set():
            cycle_start = time.monotonic()
            try:
                values = await driver.poll()
                ts = time.strftime("%H:%M:%S")
                self.out_queue.put(("values", (self.conn.name, values, ts)))
            except Exception as e:
                self.out_queue.put(("conn_status", (self.conn.name, f"error: {e}")))

            elapsed = time.monotonic() - cycle_start
            wait_left = max(0, self.interval_s - elapsed)
            slept = 0.0
            while slept < wait_left and not self._stop_event.is_set():
                step = min(0.1, wait_left - slept)
                time.sleep(step)
                slept += step

        try:
            await driver.disconnect()
        except Exception:
            pass
        self.out_queue.put(("conn_status", (self.conn.name, "stopped")))

    def _build_driver_config(self):
        tags = []
        for t in self.conn.tags:
            entry = {"name": t["name"], "mqtt_key": t["name"], "multiplier": 1.0, "offset": 0.0}
            entry.update({k: v for k, v in t.items() if k != "name"})
            tags.append(entry)

        if self.conn.protocol in ("modbus_tcp", "modbus_rtu"):
            return {
                "interface": self.conn.interface,
                "devices": [{"unit_id": self.conn.interface.get("unit_id", 1), "tags": tags}],
            }
        return {"interface": self.conn.interface, "tags": tags}


class MQTTPublisher:
    def __init__(self):
        self.client = None
        self.connected = False

    def connect(self, host, port, client_id, username, password,
                use_tls, ca_cert, client_cert, client_key):
        import paho.mqtt.client as mqtt

        self.client = mqtt.Client(client_id=client_id or f"gw-sim-{uuid.uuid4().hex[:6]}",
                                   protocol=mqtt.MQTTv311)
        if username:
            self.client.username_pw_set(username, password or "")
        if use_tls:
            kwargs = {}
            if ca_cert:
                kwargs["ca_certs"] = ca_cert
            if client_cert:
                kwargs["certfile"] = client_cert
            if client_key:
                kwargs["keyfile"] = client_key
            self.client.tls_set(**kwargs) if kwargs else self.client.tls_set()

        def on_connect(c, userdata, flags, rc):
            self.connected = (rc == 0)

        def on_disconnect(c, userdata, rc):
            self.connected = False

        self.client.on_connect = on_connect
        self.client.on_disconnect = on_disconnect
        self.client.connect(host, port, keepalive=30)
        self.client.loop_start()

        for _ in range(50):
            if self.connected:
                return True
            time.sleep(0.1)
        return self.connected

    def publish(self, topic, payload_dict, qos=1):
        if not self.client or not self.connected:
            return False
        result = self.client.publish(topic, json.dumps(payload_dict), qos=qos)
        result.wait_for_publish(timeout=5)
        return result.is_published()

    def disconnect(self):
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
        self.connected = False


# --- Dashboard -------------------------------------------------------------

class DashboardScreen(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self._build()

    def _build(self):
        header = ttk.Frame(self)
        header.pack(fill="x", padx=20, pady=(20, 12))
        ttk.Label(header, text="Dashboard", font=("Segoe UI", 16)).pack(side="left")
        self.status_label = ttk.Label(header, text="", foreground="gray")
        self.status_label.pack(side="right")

        cards = ttk.Frame(self)
        cards.pack(fill="x", padx=20, pady=8)
        for i in range(4):
            cards.columnconfigure(i, weight=1)

        self.availability_val = self._metric_card(cards, "Availability today", "--", 0)
        self.cycles_val = self._metric_card(cards, "Cycles today", "--", 1)
        self.mode_val = self._metric_card(cards, "Mode", "--", 2)
        self.oee_val = self._metric_card(cards, "OEE (est.)", "--", 3)

        conn_frame = ttk.LabelFrame(self, text="Connections")
        conn_frame.pack(fill="both", expand=True, padx=20, pady=12)
        self.conn_tree = ttk.Treeview(
            conn_frame, columns=("protocol", "status", "tags"), show="headings", height=8
        )
        for col, label, w in [("protocol", "Protocol", 220), ("status", "Status", 140), ("tags", "# Tags", 80)]:
            self.conn_tree.heading(col, text=label)
            self.conn_tree.column(col, width=w, anchor="w")
        self.conn_tree.pack(fill="both", expand=True, padx=8, pady=8)

    def _metric_card(self, parent, label, value, col):
        card = ttk.Frame(parent, relief="ridge", borderwidth=1)
        card.grid(row=0, column=col, padx=6, sticky="nsew")
        ttk.Label(card, text=label, foreground="gray", font=("Segoe UI", 9)).pack(
            anchor="w", padx=12, pady=(10, 0)
        )
        val_label = ttk.Label(card, text=value, font=("Segoe UI", 20))
        val_label.pack(anchor="w", padx=12, pady=(0, 10))
        return val_label

    def refresh(self):
        conns = self.app.connections
        self.status_label.configure(
            text=f"{len(conns)} connection(s) configured" if conns else "No connections yet"
        )
        self.conn_tree.delete(*self.conn_tree.get_children())
        for name, conn in conns.items():
            self.conn_tree.insert("", "end", iid=name, values=(
                PROTOCOL_LABELS[conn.protocol], conn.status, len(conn.tags)
            ))

        oee = self.app.compute_oee()

        self.availability_val.configure(
            text=f"{oee['availability']:.1f}%" if oee["availability"] is not None else "--"
        )
        self.cycles_val.configure(
            text=str(oee["cycles"]) if oee["cycles"] is not None else "--"
        )
        self.mode_val.configure(text=oee["mode"] or "--")

        if oee["oee"] is not None:
            self.oee_val.configure(text=f"{oee['oee']:.1f}%")
        elif self.app.oee_config.get("last_result") is not None:
            # fall back to the last manual custom-formula test, if the
            # standard mapping isn't complete enough for a live OEE% yet
            cfg = self.app.oee_config
            self.oee_val.configure(
                text=f"{cfg['last_result']:.1f}%" if cfg.get("as_percent") else f"{cfg['last_result']:.2f}"
            )
        else:
            self.oee_val.configure(text="--")


# --- OEE Configuration --------------------------------------------------------

class OEEScreen(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.test_value_entries = {}
        self._build()

    def _build(self):
        ttk.Label(self, text="OEE configuration", font=("Segoe UI", 16)).pack(
            anchor="w", padx=20, pady=(20, 4)
        )
        ttk.Label(
            self,
            text="Reference tag names directly in your formula. Type test values by hand "
                 "to check the math -- live wiring to real polled values comes in Step 3.",
            foreground="gray", wraplength=760, justify="left",
        ).pack(anchor="w", padx=20, pady=(0, 12))

        std_frame = ttk.LabelFrame(self, text="Standard mapping (optional -- fills the formula box for you)")
        std_frame.pack(fill="x", padx=20, pady=6)

        self.run_status_entry = self._labeled_entry(std_frame, "Run status tag name", "Run_Status")
        self.total_count_entry = self._labeled_entry(std_frame, "Total count tag name", "Total_Count")
        self.reject_count_entry = self._labeled_entry(std_frame, "Reject count tag name", "Reject_Count")
        self.planned_time_entry = self._labeled_entry(std_frame, "Planned production time (min)", "480")
        self.ideal_cycle_entry = self._labeled_entry(std_frame, "Ideal cycle time (s)", "30")

        ttk.Button(std_frame, text="Fill formula from standard mapping", command=self.fill_standard_formula).pack(
            anchor="w", padx=8, pady=8
        )

        formula_frame = ttk.LabelFrame(self, text="Custom formula")
        formula_frame.pack(fill="x", padx=20, pady=6)
        ttk.Label(
            formula_frame,
            text="Use +, -, *, /, parentheses, and tag names as variables. Example: "
                 "(Total_Count - Reject_Count) / Total_Count",
            foreground="gray", wraplength=760, justify="left",
        ).pack(anchor="w", padx=8, pady=(8, 4))

        self.formula_text = tk.Text(formula_frame, height=2, font=("Consolas", 10))
        self.app.dark_aware_texts.append(self.formula_text)
        self.formula_text.insert("1.0", "(Total_Count - Reject_Count) / Total_Count")
        self.formula_text.pack(fill="x", padx=8, pady=4)

        self.as_percent = tk.BooleanVar(value=True)
        ttk.Checkbutton(formula_frame, text="Show result as a percentage (x 100)", variable=self.as_percent).pack(
            anchor="w", padx=8, pady=(0, 8)
        )

        ttk.Button(formula_frame, text="Load test-value fields for this formula", command=self.load_test_fields).pack(
            anchor="w", padx=8, pady=(0, 8)
        )

        self.test_frame = ttk.LabelFrame(self, text="Test values (type numbers, or pull from live data)")
        self.test_frame.pack(fill="x", padx=20, pady=6)
        self.test_fields_container = ttk.Frame(self.test_frame)
        self.test_fields_container.pack(fill="x", padx=8, pady=8)
        ttk.Label(self.test_fields_container, text="Click \"Load test-value fields\" above first.", foreground="gray").pack()
        ttk.Button(self.test_frame, text="Fill from live data", command=self.fill_from_live).pack(
            anchor="w", padx=8, pady=(0, 8)
        )

        result_frame = ttk.Frame(self)
        result_frame.pack(fill="x", padx=20, pady=10)
        ttk.Button(result_frame, text="Test formula", command=self.test_formula).pack(side="left")
        ttk.Button(result_frame, text="Save OEE config", command=self.save_config).pack(side="left", padx=8)
        self.result_label = ttk.Label(result_frame, text="", font=("Segoe UI", 12))
        self.result_label.pack(side="left", padx=16)

        session_frame = ttk.LabelFrame(self, text="Live session (from the standard mapping above, tracked since Start All / last reset)")
        session_frame.pack(fill="x", padx=20, pady=(10, 20))
        self.session_labels = {}
        grid = ttk.Frame(session_frame)
        grid.pack(fill="x", padx=8, pady=8)
        for i, key in enumerate(["run_time", "cycles", "availability", "performance", "quality", "oee"]):
            titles = {"run_time": "Run time", "cycles": "Cycles", "availability": "Availability",
                      "performance": "Performance", "quality": "Quality", "oee": "OEE"}
            cell = ttk.Frame(grid)
            cell.grid(row=0, column=i, padx=10)
            ttk.Label(cell, text=titles[key], foreground="gray", font=("Segoe UI", 9)).pack()
            lbl = ttk.Label(cell, text="--", font=("Segoe UI", 13))
            lbl.pack()
            self.session_labels[key] = lbl
        ttk.Button(session_frame, text="Reset session", command=self.app_reset_session).pack(anchor="w", padx=8, pady=(0, 8))

    def app_reset_session(self):
        self.app.reset_oee_session()
        self.refresh()

    def fill_from_live(self):
        metrics = self.app.get_flat_metrics()
        if not self.test_value_entries:
            self.load_test_fields()
        missing = []
        for name, entry in self.test_value_entries.items():
            if name in metrics:
                entry.delete(0, "end")
                entry.insert(0, str(metrics[name]))
            else:
                missing.append(name)
        if missing:
            messagebox.showinfo(
                "Some tags not found",
                f"No live value yet for: {', '.join(missing)}. Check the tag name matches "
                f"exactly, and that Start All is running on Connections.",
            )

    def refresh(self):
        rt = self.app.oee_runtime
        oee = self.app.compute_oee()
        self.session_labels["run_time"].configure(text=f"{rt['run_seconds']:.0f}s")
        self.session_labels["cycles"].configure(text=str(oee["cycles"]) if oee["cycles"] is not None else "--")
        self.session_labels["availability"].configure(
            text=f"{oee['availability']:.1f}%" if oee["availability"] is not None else "--"
        )
        self.session_labels["performance"].configure(
            text=f"{oee['performance']:.1f}%" if oee["performance"] is not None else "--"
        )
        self.session_labels["quality"].configure(
            text=f"{oee['quality']:.1f}%" if oee["quality"] is not None else "--"
        )
        self.session_labels["oee"].configure(text=f"{oee['oee']:.1f}%" if oee["oee"] is not None else "--")

    def _labeled_entry(self, parent, label, default):
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=8, pady=3)
        ttk.Label(row, text=label, width=30).pack(side="left")
        e = ttk.Entry(row, width=25)
        e.insert(0, default)
        e.pack(side="left")
        return e

    def fill_standard_formula(self):
        total = self.total_count_entry.get().strip() or "Total_Count"
        reject = self.reject_count_entry.get().strip() or "Reject_Count"
        formula = f"(({total} - {reject}) / {total})"
        self.formula_text.delete("1.0", "end")
        self.formula_text.insert("1.0", formula)
        messagebox.showinfo(
            "Formula filled",
            "This is the Quality factor as a starting point. Availability and Performance "
            "depend on run-time tracking over a shift, which needs live polling (Step 3) "
            "rather than a single instant value.",
        )

    def load_test_fields(self):
        expr = self.formula_text.get("1.0", "end").strip()
        tag_names = extract_tag_names(expr)
        for child in self.test_fields_container.winfo_children():
            child.destroy()
        self.test_value_entries.clear()

        if not tag_names:
            ttk.Label(self.test_fields_container, text="No tag names found in the formula.", foreground="gray").pack()
            return

        for name in tag_names:
            row = ttk.Frame(self.test_fields_container)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=name, width=25).pack(side="left")
            e = ttk.Entry(row, width=15)
            e.insert(0, "0")
            e.pack(side="left")
            self.test_value_entries[name] = e

    def test_formula(self):
        expr = self.formula_text.get("1.0", "end").strip()
        if not expr:
            messagebox.showerror("Empty formula", "Type a formula first")
            return
        if not self.test_value_entries:
            self.load_test_fields()
            if not self.test_value_entries:
                return

        values = {}
        for name, entry in self.test_value_entries.items():
            try:
                values[name] = float(entry.get().strip())
            except ValueError:
                messagebox.showerror("Invalid value", f"'{name}' needs a number")
                return

        try:
            result = safe_eval(expr, values)
        except FormulaError as e:
            messagebox.showerror("Formula error", str(e))
            self.result_label.configure(text="")
            return

        display = result * 100 if self.as_percent.get() else result
        unit = "%" if self.as_percent.get() else ""
        self.result_label.configure(text=f"Result: {display:.2f}{unit}")

        self.app.oee_config["last_result"] = display
        self.app.oee_config["as_percent"] = self.as_percent.get()
        self.app.refresh_dashboard()

    def save_config(self):
        self.app.oee_config.update({
            "run_status_tag": self.run_status_entry.get().strip(),
            "total_count_tag": self.total_count_entry.get().strip(),
            "reject_count_tag": self.reject_count_entry.get().strip(),
            "planned_time_min": self.planned_time_entry.get().strip(),
            "ideal_cycle_s": self.ideal_cycle_entry.get().strip(),
            "formula": self.formula_text.get("1.0", "end").strip(),
            "as_percent": self.as_percent.get(),
        })
        messagebox.showinfo("Saved", "OEE configuration saved for this session.")


# --- Connections & Tags --------------------------------------------------------

class ConnectionsScreen(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self._build()

    def _build(self):
        ttk.Label(self, text="Connections", font=("Segoe UI", 16)).pack(anchor="w", padx=20, pady=(20, 4))

        btn_row = ttk.Frame(self)
        btn_row.pack(fill="x", padx=20, pady=4)
        ttk.Button(btn_row, text="Add Connection", command=self.open_add_connection).pack(side="left", padx=2)
        ttk.Button(btn_row, text="Edit Selected", command=self.open_edit_connection).pack(side="left", padx=2)
        ttk.Button(btn_row, text="Remove Selected", command=self.remove_connection).pack(side="left", padx=2)
        ttk.Button(btn_row, text="Manage Tags", command=self.open_tag_manager).pack(side="left", padx=2)
        ttk.Button(btn_row, text="Scan for Live Tags", command=self.open_scan_dialog).pack(side="left", padx=2)

        self.conn_tree = ttk.Treeview(
            self, columns=("protocol", "interface", "tags", "status"), show="headings", height=10
        )
        for col, label, w in [("protocol", "Protocol", 200), ("interface", "Interface", 300),
                               ("tags", "# Tags", 70), ("status", "Status", 140)]:
            self.conn_tree.heading(col, text=label)
            self.conn_tree.column(col, width=w, anchor="w")
        self.conn_tree.pack(fill="both", expand=True, padx=20, pady=8)

        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", padx=20, pady=8)
        self.start_btn = ttk.Button(ctrl, text="Start All", command=self.start_all)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(ctrl, text="Stop All", command=self.stop_all, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        ttk.Button(ctrl, text="Save Multi-PLC Config", command=self.save_full_config).pack(side="left", padx=4)
        ttk.Button(ctrl, text="Load Multi-PLC Config", command=self.load_full_config).pack(side="left", padx=4)

    def refresh(self):
        self.conn_tree.delete(*self.conn_tree.get_children())
        for name, conn in self.app.connections.items():
            iface_str = ", ".join(f"{k}={v}" for k, v in conn.interface.items())
            self.conn_tree.insert("", "end", iid=name, values=(
                PROTOCOL_LABELS[conn.protocol], iface_str, len(conn.tags), conn.status
            ))

    def open_add_connection(self):
        self._connection_dialog(existing=None)

    def open_edit_connection(self):
        sel = self.conn_tree.selection()
        if not sel:
            messagebox.showinfo("Select a connection", "Select a connection to edit first")
            return
        self._connection_dialog(existing=self.app.connections[sel[0]])

    def _connection_dialog(self, existing):
        win = tk.Toplevel(self)
        win.title("Edit Connection" if existing else "Add Connection")
        win.geometry("460x420")
        win.grab_set()

        ttk.Label(win, text="Connection Name").pack(anchor="w", padx=8, pady=(8, 0))
        name_entry = ttk.Entry(win, width=30)
        name_entry.insert(0, existing.name if existing else f"conn_{uuid.uuid4().hex[:4]}")
        name_entry.pack(anchor="w", padx=8)
        if existing:
            name_entry.configure(state="disabled")

        ttk.Label(win, text="Protocol").pack(anchor="w", padx=8, pady=(8, 0))
        protocol_cb = ttk.Combobox(win, values=list(PROTOCOL_LABELS.values()), state="readonly", width=35)
        label_to_key = {v: k for k, v in PROTOCOL_LABELS.items()}
        protocol_cb.set(PROTOCOL_LABELS[existing.protocol] if existing else PROTOCOL_LABELS["mc_protocol_3e"])
        protocol_cb.pack(anchor="w", padx=8)

        fields_frame = ttk.Frame(win)
        fields_frame.pack(fill="both", expand=True, padx=8, pady=8)
        field_entries = {}

        hint_label = ttk.Label(win, text="", foreground="gray", wraplength=440, justify="left")
        hint_label.pack(anchor="w", padx=8, pady=(0, 4))

        def render_fields(*_):
            for child in fields_frame.winfo_children():
                child.destroy()
            field_entries.clear()
            protocol_key = label_to_key[protocol_cb.get()]
            for label, default in INTERFACE_FIELDS[protocol_key]:
                row = ttk.Frame(fields_frame)
                row.pack(fill="x", pady=2)
                ttk.Label(row, text=label, width=16).pack(side="left")
                e = ttk.Entry(row, width=25)
                val = str(existing.interface.get(label, default)) if existing else default
                e.insert(0, val)
                e.pack(side="left")
                field_entries[label] = e

            if protocol_key == "bacnet_ip":
                hint_label.configure(
                    text="local_ip = this laptop's own IP (for ProtoProbe's identity on the "
                         "network). device_ip = the target BACnet device's IP. device_instance "
                         "= ProtoProbe's own made-up instance number (any unused number is "
                         "fine, e.g. 599999). remote_device_instance = the TARGET device's own "
                         "instance number -- find this on the device's own config screen, or "
                         "via a Who-Is scan tool, not something you invent."
                )
            else:
                hint_label.configure(text="")

        protocol_cb.bind("<<ComboboxSelected>>", render_fields)
        render_fields()

        def save():
            name = name_entry.get().strip()
            if not name:
                messagebox.showerror("Invalid", "Connection name required")
                return
            protocol_key = label_to_key[protocol_cb.get()]
            interface = {k: coerce(e.get().strip()) for k, e in field_entries.items()}

            if existing:
                existing.protocol = protocol_key
                existing.interface = interface
            else:
                if name in self.app.connections:
                    messagebox.showerror("Duplicate", f"Connection '{name}' already exists")
                    return
                self.app.connections[name] = Connection(name, protocol_key, interface)

            self.refresh()
            self.app.refresh_dashboard()
            win.destroy()

        ttk.Button(win, text="Save", command=save).pack(pady=8)

    def remove_connection(self):
        sel = self.conn_tree.selection()
        for name in sel:
            self.app.connections.pop(name, None)
        self.refresh()
        self.app.refresh_dashboard()

    def open_tag_manager(self):
        sel = self.conn_tree.selection()
        if not sel:
            messagebox.showinfo("Select a connection", "Click a connection's row above first, then click Manage Tags")
            return
        conn = self.app.connections[sel[0]]

        win = tk.Toplevel(self)
        win.title(f"Tags -- {conn.name}")
        win.geometry("600x500")
        win.grab_set()

        tree = ttk.Treeview(win, columns=("name", "fields"), show="headings", height=14)
        tree.heading("name", text="Name")
        tree.heading("fields", text="Address / Fields")
        tree.column("name", width=180)
        tree.column("fields", width=380)
        tree.pack(fill="both", expand=True, padx=8, pady=8)

        def refresh_tags():
            tree.delete(*tree.get_children())
            for t in conn.tags:
                data_type = t.get("data_type", "")
                fields_str = f"{format_address(conn.protocol, t)}  ({data_type})" if data_type else format_address(conn.protocol, t)
                tree.insert("", "end", iid=t["name"], values=(t["name"], fields_str))
        refresh_tags()

        def tag_dialog(existing_tag):
            twin = tk.Toplevel(win)
            twin.title("Edit Tag" if existing_tag else "Add Tag")
            twin.geometry("440x380")
            twin.grab_set()

            ttk.Label(twin, text="Name").pack(anchor="w", padx=8, pady=(8, 0))
            name_e = ttk.Entry(twin, width=32)
            name_e.insert(0, existing_tag["name"] if existing_tag else "")
            name_e.pack(anchor="w", padx=8)

            field_entries = {}
            for label, default in TAG_FIELDS[conn.protocol]:
                row = ttk.Frame(twin)
                row.pack(fill="x", padx=8, pady=3)
                ttk.Label(row, text=label, width=14).pack(side="left")
                if label in FIELD_OPTIONS:
                    e = ttk.Combobox(row, values=FIELD_OPTIONS[label]["options"], width=18, state="readonly")
                    e.set(str(existing_tag.get(label, default)) if existing_tag else default)
                else:
                    e = ttk.Entry(row, width=22)
                    e.insert(0, str(existing_tag.get(label, default)) if existing_tag else default)
                e.pack(side="left")
                field_entries[label] = e

            hints = {label for label, _ in TAG_FIELDS[conn.protocol] if label in FIELD_OPTIONS}
            if hints:
                hint_frame = ttk.Frame(twin)
                hint_frame.pack(fill="x", padx=8, pady=(8, 0))
                for label in hints:
                    ttk.Label(hint_frame, text=FIELD_OPTIONS[label]["hint"], wraplength=400,
                              foreground="gray", font=("Segoe UI", 8), justify="left").pack(anchor="w", pady=1)

            if conn.protocol == "opcua":
                ttk.Button(
                    twin, text="Browse Server for Tags...",
                    command=lambda: self._open_opcua_browser(conn, field_entries.get("node_id"))
                ).pack(pady=(10, 0))

            if conn.protocol == "bacnet_ip":
                ttk.Button(
                    twin, text="Browse Device for Tags...",
                    command=lambda: self._open_bacnet_browser(
                        conn, name_e, field_entries.get("object_type"), field_entries.get("object_instance")
                    )
                ).pack(pady=(10, 0))

            def save_tag():
                name = name_e.get().strip()
                if not name:
                    messagebox.showerror("Invalid", "Name required")
                    return
                if existing_tag and existing_tag in conn.tags:
                    conn.tags.remove(existing_tag)
                elif not existing_tag and any(t["name"] == name for t in conn.tags):
                    messagebox.showerror("Duplicate", f"Tag '{name}' already exists")
                    return

                new_tag = {"name": name}
                for k, e in field_entries.items():
                    raw = e.get().strip()
                    new_tag[k] = raw if k in FIELD_OPTIONS else coerce(raw)

                conn.tags.append(new_tag)
                refresh_tags()
                self.refresh()
                self.app.refresh_dashboard()
                twin.destroy()

            ttk.Button(twin, text="Save", command=save_tag).pack(pady=10)

        btn_row = ttk.Frame(win)
        btn_row.pack(fill="x", padx=8, pady=4)
        ttk.Button(btn_row, text="Add Tag", command=lambda: tag_dialog(None)).pack(side="left", padx=2)

        def edit_selected():
            sel2 = tree.selection()
            if not sel2:
                messagebox.showinfo("Select a tag", "Click a tag's row above first")
                return
            existing_tag = next(t for t in conn.tags if t["name"] == sel2[0])
            tag_dialog(existing_tag)

        ttk.Button(btn_row, text="Edit Selected", command=edit_selected).pack(side="left", padx=2)

        def remove_selected():
            sel2 = tree.selection()
            for name in sel2:
                conn.tags = [t for t in conn.tags if t["name"] != name]
            refresh_tags()
            self.refresh()
            self.app.refresh_dashboard()

        ttk.Button(btn_row, text="Remove Selected", command=remove_selected).pack(side="left", padx=2)

        def import_csv():
            path = filedialog.askopenfilename(filetypes=[("CSV files", "*.csv")])
            if not path:
                return
            expected_cols = ["name"] + [label for label, _ in TAG_FIELDS[conn.protocol]]
            added, skipped = 0, 0
            with open(path, newline="") as f:
                reader = csv.DictReader(f)
                missing = [c for c in expected_cols if c not in (reader.fieldnames or [])]
                if missing:
                    messagebox.showerror(
                        "CSV columns don't match",
                        f"This protocol needs columns: {', '.join(expected_cols)}\nMissing: {', '.join(missing)}"
                    )
                    return
                for row in reader:
                    name = row["name"].strip()
                    if not name or any(t["name"] == name for t in conn.tags):
                        skipped += 1
                        continue
                    new_tag = {"name": name}
                    for label, _ in TAG_FIELDS[conn.protocol]:
                        val = row[label].strip()
                        new_tag[label] = val if label in FIELD_OPTIONS else coerce(val)
                    conn.tags.append(new_tag)
                    added += 1
            refresh_tags()
            self.refresh()
            self.app.refresh_dashboard()
            messagebox.showinfo("Import complete", f"Added {added} tag(s), skipped {skipped} (blank/duplicate).")

        ttk.Button(btn_row, text="Import from CSV...", command=import_csv).pack(side="left", padx=2)
        ttk.Label(
            win, text=f"CSV columns required: name, {', '.join(l for l, _ in TAG_FIELDS[conn.protocol])}",
            foreground="gray", wraplength=560,
        ).pack(anchor="w", padx=8, pady=(0, 6))

        tree.bind("<Double-1>", lambda e: edit_selected())

    def open_scan_dialog(self):
        sel = self.conn_tree.selection()
        if not sel:
            messagebox.showinfo("Select a connection", "Click a connection's row above first")
            return
        conn = self.app.connections[sel[0]]

        if conn.protocol not in ("modbus_tcp", "modbus_rtu", "s7comm", "mc_protocol_3e"):
            messagebox.showinfo(
                "Not needed for this protocol",
                "OPC UA and BACnet already expose real tag names over the wire -- use "
                "'Browse Server for Tags' (OPC UA) or CSV import (BACnet) in Manage Tags "
                "instead of scanning a raw address range.",
            )
            return

        win = tk.Toplevel(self)
        win.title(f"Scan for live tags -- {conn.name}")
        win.geometry("640x560")
        win.grab_set()

        ttk.Label(
            win,
            text="Both options below run in the background and land on the 'Scan results' "
                 "tab -- you can close this window right after clicking Start and check "
                 "results whenever you like. Assumes int16 for the scan itself; check and "
                 "fix the data type after adding a tag if it's actually 32-bit or float.",
            wraplength=600, foreground="gray", justify="left",
        ).pack(anchor="w", padx=10, pady=(10, 10))

        opt_row = ttk.Frame(win)
        opt_row.pack(fill="x", padx=10, pady=4)
        ttk.Label(opt_row, text="Snapshots").pack(side="left")
        snapshots_entry = ttk.Entry(opt_row, width=6)
        snapshots_entry.insert(0, "3")
        snapshots_entry.pack(side="left", padx=(2, 12))
        ttk.Label(opt_row, text="Delay between (s)").pack(side="left")
        delay_entry = ttk.Entry(opt_row, width=6)
        delay_entry.insert(0, "2")
        delay_entry.pack(side="left", padx=(2, 12))

        # ---- Section 1: single area/range (a quick, targeted check) ----
        single_frame = ttk.LabelFrame(win, text="Scan a single area/range")
        single_frame.pack(fill="x", padx=10, pady=6)

        range_frame = ttk.Frame(single_frame)
        range_frame.pack(fill="x", padx=8, pady=6)
        range_entries = {}

        if conn.protocol == "mc_protocol_3e":
            fields = [("device_code", "D"), ("start", "0"), ("end", "100")]
        elif conn.protocol == "s7comm":
            fields = [("area", "DB"), ("db_number", "1"), ("start", "0"), ("end", "100")]
        else:
            fields = [("register_type", "holding"), ("start", "0"), ("end", "100")]

        for label, default in fields:
            row = ttk.Frame(range_frame)
            row.pack(side="left", padx=8)
            ttk.Label(row, text=label).pack()
            if label == "device_code":
                e = ttk.Combobox(row, values=["D", "M", "R", "W"], width=6, state="readonly")
                e.set(default)
            elif label == "register_type":
                e = ttk.Combobox(row, values=["holding", "input"], width=10, state="readonly")
                e.set(default)
            elif label == "area":
                e = ttk.Combobox(row, values=["DB", "I", "Q", "M"], width=6, state="readonly")
                e.set(default)
            else:
                e = ttk.Entry(row, width=10)
                e.insert(0, default)
            e.pack()
            range_entries[label] = e

        single_status = ttk.Label(single_frame, text="", foreground="gray")

        if conn.protocol == "s7comm":
            ttk.Label(
                single_frame, text="Note: for area I/Q, manual entry in Manage Tags is usually "
                          "better -- those bits map to physical wiring you probably already "
                          "have documented. M has no wiring to reference, so scanning it is "
                          "worthwhile. db_number is ignored unless area = DB.",
                wraplength=600, foreground="gray", justify="left",
            ).pack(anchor="w", padx=8, pady=(0, 4))

            discover_frame = ttk.Frame(single_frame)
            discover_frame.pack(fill="x", padx=8, pady=(0, 4))
            ttk.Label(discover_frame, text="Don't know which DB numbers exist? Probe up to DB").pack(side="left")
            max_db_entry = ttk.Entry(discover_frame, width=6)
            max_db_entry.insert(0, "50")
            max_db_entry.pack(side="left", padx=4)
            discover_btn = ttk.Button(discover_frame, text="Discover DBs")
            discover_btn.pack(side="left", padx=8)
            discover_status = ttk.Label(discover_frame, text="", foreground="gray")
            discover_status.pack(side="left", padx=8)

            discovered_list = tk.Listbox(single_frame, height=3, font=("Consolas", 9))
            discovered_list.pack(fill="x", padx=8, pady=(0, 4))

            def use_discovered_db():
                sel3 = discovered_list.curselection()
                if not sel3:
                    return
                db_num = discovered_list.get(sel3[0]).split()[0].replace("DB", "")
                range_entries["db_number"].delete(0, "end")
                range_entries["db_number"].insert(0, db_num)

            discovered_list.bind("<Double-1>", lambda e: use_discovered_db())
            ttk.Label(single_frame, text="Double-click a discovered DB above to use it.", foreground="gray").pack(
                anchor="w", padx=8, pady=(0, 6)
            )

            discover_out_queue = queue.Queue()

            def start_discover():
                try:
                    max_db = int(max_db_entry.get().strip())
                except ValueError:
                    messagebox.showerror("Invalid input", "Max DB must be a number")
                    return
                discover_btn.configure(state="disabled")
                discover_status.configure(text="Probing...")
                discovered_list.delete(0, "end")

                def worker():
                    dbs = discover_db_numbers_sync(conn, max_db, discover_out_queue)
                    discover_out_queue.put(("db_discovery_done", dbs))

                threading.Thread(target=worker, daemon=True).start()
                pump_discover()

            def pump_discover():
                try:
                    while True:
                        kind, data = discover_out_queue.get_nowait()
                        if kind == "status":
                            discover_status.configure(text=data)
                        elif kind == "db_discovery_done":
                            for db in data:
                                discovered_list.insert("end", f"DB{db}")
                            discover_btn.configure(state="normal")
                            discover_status.configure(text=f"Done -- {len(data)} DB(s) found")
                            return
                except queue.Empty:
                    pass
                win.after(150, pump_discover)

            discover_btn.configure(command=start_discover)

        def start_single_scan():
            try:
                snapshots_n = int(snapshots_entry.get().strip())
                delay_s = float(delay_entry.get().strip())
                params = {k: (e.get().strip() if isinstance(e, ttk.Combobox) else int(e.get().strip()))
                          for k, e in range_entries.items()}
            except ValueError:
                messagebox.showerror("Invalid input", "Check the range/snapshot fields are valid numbers")
                return

            tags = build_range_tags(conn.protocol, params)
            step = 2 if conn.protocol == "s7comm" else 1
            address_count = max(0, (params["end"] - params["start"]) // step + 1)
            total_reads = address_count * snapshots_n
            est_seconds = total_reads * 0.05 + (snapshots_n - 1) * delay_s

            if address_count > 300:
                proceed = messagebox.askyesno(
                    "Large scan",
                    f"This will read {address_count} addresses x {snapshots_n} snapshots. "
                    f"Rough estimate: {est_seconds/60:.1f} minutes.\n\nContinue?",
                )
                if not proceed:
                    return

            job_label = f"{params.get('device_code', params.get('area', params.get('register_type', '')))} " \
                        f"({params['start']}-{params['end']})"

            self.app.start_background_scan(
                run_range_scan, (conn, conn.protocol, tags, snapshots_n, delay_s, job_label, self.app.scan_out_queue),
            )
            single_status.configure(
                text="Started -- check the 'Scan results' tab. You can close this window now.",
                foreground="green",
            )

        ttk.Button(single_frame, text="Start scan (background)", command=start_single_scan).pack(
            anchor="w", padx=8, pady=6
        )
        single_status.pack(anchor="w", padx=8, pady=(0, 8))

        # ---- Section 2: scan the whole PLC's common areas at once ----
        full_frame = ttk.LabelFrame(win, text="Scan the whole PLC (all common areas, one background job)")
        full_frame.pack(fill="x", padx=10, pady=6)

        full_status = ttk.Label(full_frame, text="", foreground="gray")
        checkbox_vars = {}

        if conn.protocol == "mc_protocol_3e":
            ttk.Label(full_frame, text="Device codes to include:").pack(anchor="w", padx=8, pady=(6, 0))
            code_row = ttk.Frame(full_frame)
            code_row.pack(fill="x", padx=8)
            for code, default_on in [("D", True), ("M", True), ("R", True), ("W", True)]:
                v = tk.BooleanVar(value=default_on)
                ttk.Checkbutton(code_row, text=code, variable=v).pack(side="left", padx=6)
                checkbox_vars[code] = v
            max_row = ttk.Frame(full_frame)
            max_row.pack(fill="x", padx=8, pady=6)
            ttk.Label(max_row, text="Max address (0 to this, per selected code)").pack(side="left")
            max_addr_entry = ttk.Entry(max_row, width=8)
            max_addr_entry.insert(0, "8000")
            max_addr_entry.pack(side="left", padx=6)

        elif conn.protocol == "s7comm":
            ttk.Label(full_frame, text="Areas to include:").pack(anchor="w", padx=8, pady=(6, 0))
            code_row = ttk.Frame(full_frame)
            code_row.pack(fill="x", padx=8)
            for area, default_on in [("DB", True), ("I", True), ("Q", True), ("M", True)]:
                v = tk.BooleanVar(value=default_on)
                ttk.Checkbutton(code_row, text=area, variable=v).pack(side="left", padx=6)
                checkbox_vars[area] = v
            ttk.Label(
                full_frame, text="DB auto-discovers which DB numbers exist first. I/Q left "
                          "unchecked by default -- manual entry from wiring docs is usually "
                          "better for those.",
                wraplength=600, foreground="gray", justify="left",
            ).pack(anchor="w", padx=8, pady=(2, 0))
            max_row = ttk.Frame(full_frame)
            max_row.pack(fill="x", padx=8, pady=6)
            ttk.Label(max_row, text="Max byte per area/DB").pack(side="left")
            max_addr_entry = ttk.Entry(max_row, width=8)
            max_addr_entry.insert(0, "2000")
            max_addr_entry.pack(side="left", padx=6)

        else:  # modbus
            ttk.Label(full_frame, text="Register types to include:").pack(anchor="w", padx=8, pady=(6, 0))
            code_row = ttk.Frame(full_frame)
            code_row.pack(fill="x", padx=8)
            for rtype, default_on in [("holding", True), ("input", True)]:
                v = tk.BooleanVar(value=default_on)
                ttk.Checkbutton(code_row, text=rtype, variable=v).pack(side="left", padx=6)
                checkbox_vars[rtype] = v
            max_row = ttk.Frame(full_frame)
            max_row.pack(fill="x", padx=8, pady=6)
            ttk.Label(max_row, text="Max address per register type").pack(side="left")
            max_addr_entry = ttk.Entry(max_row, width=8)
            max_addr_entry.insert(0, "2000")
            max_addr_entry.pack(side="left", padx=6)

        def start_full_scan():
            selected = [k for k, v in checkbox_vars.items() if v.get()]
            if not selected:
                messagebox.showinfo("Nothing selected", "Check at least one area/code/type first")
                return
            try:
                snapshots_n = int(snapshots_entry.get().strip())
                delay_s = float(delay_entry.get().strip())
                max_addr = int(max_addr_entry.get().strip())
            except ValueError:
                messagebox.showerror("Invalid input", "Check snapshots/delay/max address are numbers")
                return

            if conn.protocol == "mc_protocol_3e":
                options = {"device_codes": selected, "max_address": max_addr}
            elif conn.protocol == "s7comm":
                options = {"areas": selected, "max_address": max_addr, "max_db_probe": 50}
            else:
                options = {"register_types": selected, "max_address": max_addr}

            proceed = messagebox.askyesno(
                "Full PLC scan",
                f"This scans {len(selected)} area(s)/type(s) up to address/byte {max_addr} each, "
                f"x {snapshots_n} snapshots. This can take several minutes depending on how much "
                f"you selected -- it runs in the background, so you can close this window and "
                f"keep working. Check the 'Scan results' tab for progress and results.\n\nStart?",
            )
            if not proceed:
                return

            self.app.start_background_scan(
                run_full_plc_scan_job,
                (conn, conn.protocol, options, snapshots_n, delay_s, self.app.scan_out_queue),
            )
            full_status.configure(
                text="Started -- check the 'Scan results' tab. You can close this window now.",
                foreground="green",
            )

        ttk.Button(full_frame, text="Start full PLC scan (background)", command=start_full_scan).pack(
            anchor="w", padx=8, pady=6
        )
        full_status.pack(anchor="w", padx=8, pady=(0, 8))

        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(4, 10))

    def _open_opcua_browser(self, conn, node_id_entry):
        win = tk.Toplevel(self)
        win.title(f"Browse OPC UA -- {conn.interface.get('endpoint', '')}")
        win.geometry("480x420")
        win.grab_set()

        path_label = ttk.Label(win, text="Objects (root)", foreground="gray")
        path_label.pack(anchor="w", padx=8, pady=(8, 0))

        listbox = tk.Listbox(win, font=("Consolas", 10))
        listbox.pack(fill="both", expand=True, padx=8, pady=8)

        state = {"current_node_id": "i=85", "path": ["Objects"], "children": []}

        def fetch_children(node_id):
            async def _fetch():
                from asyncua import Client
                async with Client(url=conn.interface["endpoint"]) as client:
                    node = client.get_node(node_id)
                    children = await node.get_children()
                    results = []
                    for child in children:
                        try:
                            name = (await child.read_browse_name()).Name
                        except Exception:
                            name = "?"
                        results.append((name, child.nodeid.to_string()))
                    return results
            return asyncio.run(_fetch())

        def refresh_list():
            listbox.delete(0, "end")
            try:
                state["children"] = fetch_children(state["current_node_id"])
            except Exception as e:
                messagebox.showerror("Browse failed", str(e))
                return
            for name, node_id in state["children"]:
                listbox.insert("end", f"{name}   [{node_id}]")
            path_label.configure(text=" / ".join(state["path"]))

        def drill_in(_event=None):
            sel = listbox.curselection()
            if not sel:
                return
            name, node_id = state["children"][sel[0]]
            state["current_node_id"] = node_id
            state["path"].append(name)
            refresh_list()

        def go_up():
            if len(state["path"]) <= 1:
                return
            state["path"].pop()
            state["current_node_id"] = "i=85"
            refresh_list()

        def select_node():
            sel = listbox.curselection()
            if not sel:
                messagebox.showinfo("Select a node", "Click a node in the list first")
                return
            name, node_id = state["children"][sel[0]]
            if node_id_entry is not None:
                node_id_entry.delete(0, "end")
                node_id_entry.insert(0, node_id)
            win.destroy()

        btn_row = ttk.Frame(win)
        btn_row.pack(fill="x", padx=8, pady=4)
        ttk.Button(btn_row, text="Up", command=go_up).pack(side="left", padx=2)
        ttk.Button(btn_row, text="Open Selected Folder", command=drill_in).pack(side="left", padx=2)
        ttk.Button(btn_row, text="Use Selected Node ID", command=select_node).pack(side="left", padx=2)

        listbox.bind("<Double-1>", drill_in)
        refresh_list()

    def _open_bacnet_browser(self, conn, name_entry, object_type_entry, object_instance_entry):
        win = tk.Toplevel(self)
        win.title(f"Browse BACnet -- {conn.interface.get('device_ip', '')}")
        win.geometry("560x460")
        win.grab_set()

        ttk.Label(
            win,
            text="Untested against real hardware -- if this errors out, that's the thing to "
                 "debug, not necessarily your PLC config. Reads the device's own object list "
                 "and names directly from the wire.",
            wraplength=520, foreground="gray", justify="left",
        ).pack(anchor="w", padx=8, pady=(8, 4))

        status_label = ttk.Label(win, text="", foreground="gray")
        status_label.pack(anchor="w", padx=8)

        listbox = tk.Listbox(win, font=("Consolas", 10))
        listbox.pack(fill="both", expand=True, padx=8, pady=8)

        state = {"objects": []}

        def fetch_objects():
            async def _fetch():
                import bacpypes3.app
                import bacpypes3.local.device
                from bacpypes3.pdu import Address
                from bacpypes3.primitivedata import ObjectIdentifier

                iface = conn.interface
                device_obj = bacpypes3.local.device.LocalDeviceObject(
                    objectName="protoprobe-browse",
                    objectIdentifier=int(iface.get("device_instance", 599999)),
                    maxApduLengthAccepted=1024,
                    segmentationSupported="segmentedBoth",
                )
                app = bacpypes3.app.Application.from_object_name_and_address(
                    device_obj, f"{iface['local_ip']}/24"
                )
                try:
                    dest = Address(f"{iface['device_ip']}:{iface.get('device_port', 47808)}")
                    dev_oid = ObjectIdentifier(("device", int(iface["remote_device_instance"])))

                    length = int(await app.read_property(dest, dev_oid, "objectList", 0))
                    results = []
                    for i in range(1, length + 1):
                        try:
                            oid = await app.read_property(dest, dev_oid, "objectList", i)
                            obj_type, obj_inst = oid[0], int(oid[1])
                            obj_type_str = obj_type if isinstance(obj_type, str) else str(obj_type)
                            try:
                                name = await app.read_property(
                                    dest, ObjectIdentifier((obj_type_str, obj_inst)), "objectName"
                                )
                            except Exception:
                                name = "?"
                            results.append((str(name), obj_type_str, obj_inst))
                        except Exception:
                            continue
                    return results
                finally:
                    app.close()

            return asyncio.run(_fetch())

        def refresh_list():
            listbox.delete(0, "end")
            status_label.configure(text="Reading device object list...")
            self.update_idletasks()
            try:
                state["objects"] = fetch_objects()
            except Exception as e:
                messagebox.showerror(
                    "Browse failed", f"{e}\n\nCheck remote_device_instance and device_ip are "
                                     f"correct in the connection's interface settings."
                )
                status_label.configure(text="Failed")
                return
            for name, obj_type, obj_inst in state["objects"]:
                listbox.insert("end", f"{name}   [{obj_type}:{obj_inst}]")
            status_label.configure(text=f"{len(state['objects'])} object(s) found")

        def select_object():
            sel = listbox.curselection()
            if not sel:
                messagebox.showinfo("Select an object", "Click an object in the list first")
                return
            name, obj_type, obj_inst = state["objects"][sel[0]]
            if name_entry is not None:
                name_entry.delete(0, "end")
                name_entry.insert(0, name if name != "?" else f"{obj_type}_{obj_inst}")
            if object_type_entry is not None:
                object_type_entry.set(obj_type) if hasattr(object_type_entry, "set") else None
            if object_instance_entry is not None:
                object_instance_entry.delete(0, "end")
                object_instance_entry.insert(0, str(obj_inst))
            win.destroy()

        btn_row = ttk.Frame(win)
        btn_row.pack(fill="x", padx=8, pady=4)
        ttk.Button(btn_row, text="Refresh", command=refresh_list).pack(side="left", padx=2)
        ttk.Button(btn_row, text="Use Selected Object", command=select_object).pack(side="left", padx=2)

        listbox.bind("<Double-1>", lambda e: select_object())
        refresh_list()

    def start_all(self):
        if not self.app.connections:
            messagebox.showinfo("No connections", "Add at least one PLC connection first")
            return
        any_started = False
        for conn in self.app.connections.values():
            if not conn.tags:
                continue
            merge_interval = float(self.app.output_screen.merge_interval_entry.get().strip() or 5) \
                if self.app.output_screen else 5.0
            poll_interval = min(merge_interval, 2.0)
            thread = PollerThread(conn, poll_interval, self.app.out_queue)
            conn.thread = thread
            thread.start()
            any_started = True

        if not any_started:
            messagebox.showinfo("No tags", "None of your connections have tags yet -- use Manage Tags first")
            return

        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.app.merge_timer_running = True
        self.app.start_merge_tick()

    def stop_all(self):
        for conn in self.app.connections.values():
            if conn.thread:
                conn.thread.stop()
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.app.merge_timer_running = False

    def save_full_config(self):
        polling_groups = []
        for conn in self.app.connections.values():
            tags = []
            for t in conn.tags:
                entry = {"name": t["name"], "mqtt_key": t["name"], "multiplier": 1.0,
                         "offset": 0.0, "deadband": 0}
                entry.update({k: v for k, v in t.items() if k != "name"})
                tags.append(entry)
            group = {
                "group_id": conn.name, "protocol": conn.protocol, "enabled": True,
                "interface": conn.interface, "poll_interval_s": 5,
            }
            if conn.protocol in ("modbus_tcp", "modbus_rtu"):
                group["devices"] = [{"unit_id": conn.interface.get("unit_id", 1), "tags": tags}]
            else:
                group["tags"] = tags
            polling_groups.append(group)

        config = {"device": {"deviceId": "AG221-Edge-01"}, "polling_groups": polling_groups}
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON files", "*.json")],
                                             initialfile="multi_plc_config.json")
        if path:
            with open(path, "w") as f:
                json.dump(config, f, indent=2)
            messagebox.showinfo("Saved", f"Config saved to:\n{path}")

    def load_full_config(self):
        path = filedialog.askopenfilename(filetypes=[("JSON files", "*.json")])
        if not path:
            return
        with open(path) as f:
            config = json.load(f)

        self.app.connections.clear()
        for group in config.get("polling_groups", []):
            conn = Connection(group["group_id"], group["protocol"], group.get("interface", {}))
            if "devices" in group:
                conn.tags = [
                    {k: v for k, v in t.items() if k not in ("mqtt_key", "multiplier", "offset", "deadband")}
                    for d in group["devices"] for t in d["tags"]
                ]
            else:
                conn.tags = [
                    {k: v for k, v in t.items() if k not in ("mqtt_key", "multiplier", "offset", "deadband")}
                    for t in group.get("tags", [])
                ]
            self.app.connections[conn.name] = conn

        self.refresh()
        self.app.refresh_dashboard()
        messagebox.showinfo("Loaded", f"Loaded {len(self.app.connections)} connection(s) from:\n{path}")


# --- Live Values ------------------------------------------------------------

class LiveValuesScreen(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        ttk.Label(self, text="Live values", font=("Segoe UI", 16)).pack(anchor="w", padx=20, pady=(20, 12))

        columns = ("connection", "protocol", "tag", "address", "data_type", "value", "updated")
        self.live_tree = ttk.Treeview(self, columns=columns, show="headings", height=20)
        headings = [
            ("connection", "Connection", 130), ("protocol", "Protocol", 140),
            ("tag", "Tag", 150), ("address", "Address / fields", 220),
            ("data_type", "Data type", 90), ("value", "Live value", 110),
            ("updated", "Last updated", 100),
        ]
        for col, label, w in headings:
            self.live_tree.heading(col, text=label)
            self.live_tree.column(col, width=w, anchor="center")
        self.live_tree.pack(fill="both", expand=True, padx=20, pady=8)

    def refresh(self):
        self.live_tree.delete(*self.live_tree.get_children())
        for conn in self.app.connections.values():
            tag_lookup = {t["name"]: t for t in conn.tags}
            for tag_name, value in conn.latest_values.items():
                tag_fields = tag_lookup.get(tag_name, {})
                address_str = format_address(conn.protocol, tag_fields)
                data_type = tag_fields.get("data_type", "")
                iid = f"{conn.name}::{tag_name}"
                self.live_tree.insert("", "end", iid=iid, values=(
                    conn.name, PROTOCOL_LABELS.get(conn.protocol, conn.protocol), tag_name,
                    address_str, data_type, value, conn.last_updated.get(tag_name, "")
                ))


# --- Output (JSON template + MQTT) -------------------------------------------

class OutputScreen(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self._build()

    def _build(self):
        canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0)
        scroll_frame = ttk.Frame(canvas)
        vsb = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        root = scroll_frame
        ttk.Label(root, text="Output", font=("Segoe UI", 16)).pack(anchor="w", padx=20, pady=(20, 12))

        tmpl_frame = ttk.LabelFrame(root, text="JSON output template")
        tmpl_frame.pack(fill="x", padx=20, pady=6)

        row1 = ttk.Frame(tmpl_frame)
        row1.pack(fill="x", padx=4, pady=2)
        ttk.Label(row1, text="deviceId").pack(side="left")
        self.device_id_entry = ttk.Entry(row1, width=20)
        self.device_id_entry.insert(0, "AG221-Edge-01")
        self.device_id_entry.pack(side="left", padx=4)

        ttk.Label(row1, text="Merge/send interval (s)").pack(side="left", padx=(12, 0))
        self.merge_interval_entry = ttk.Entry(row1, width=8)
        self.merge_interval_entry.insert(0, "5")
        self.merge_interval_entry.pack(side="left", padx=4)

        self.group_by_conn = tk.BooleanVar(value=False)
        ttk.Checkbutton(row1, text="Group metrics by connection name", variable=self.group_by_conn).pack(side="left", padx=12)

        ttk.Label(
            tmpl_frame,
            text='Edit this JSON freely. Placeholders "__DEVICE_ID__", "__TIMESTAMP__", '
                 '"__METRICS__" get replaced with live data wherever you put them.',
            wraplength=1000, foreground="gray", justify="left",
        ).pack(anchor="w", padx=4, pady=(4, 0))

        self.template_text = tk.Text(tmpl_frame, height=8, font=("Consolas", 9))
        self.app.dark_aware_texts.append(self.template_text)
        self.template_text.insert("1.0", DEFAULT_TEMPLATE)
        self.template_text.pack(fill="x", padx=4, pady=4)

        ttk.Button(tmpl_frame, text="Preview JSON Now", command=self.preview_json_now).pack(anchor="w", padx=4, pady=(0, 6))

        mqtt_frame = ttk.LabelFrame(root, text="MQTT / Cloud publish (TLS + mTLS + AWS IoT Core compatible)")
        mqtt_frame.pack(fill="x", padx=20, pady=6)

        mrow1 = ttk.Frame(mqtt_frame)
        mrow1.pack(fill="x", padx=4, pady=2)
        self.mqtt_host = self._labeled_entry(mrow1, "Broker Host", "")
        self.mqtt_port = self._labeled_entry(mrow1, "Port", "8883")
        self.mqtt_topic = self._labeled_entry(mrow1, "Topic", "iiot/AG221-Edge-01/telemetry")

        mrow2 = ttk.Frame(mqtt_frame)
        mrow2.pack(fill="x", padx=4, pady=2)
        self.mqtt_username = self._labeled_entry(mrow2, "Username (optional)", "")
        self.mqtt_password = self._labeled_entry(mrow2, "Password (optional)", "", show="*")
        self.mqtt_use_tls = tk.BooleanVar(value=True)
        ttk.Checkbutton(mrow2, text="Use TLS", variable=self.mqtt_use_tls).pack(side="left", padx=12)

        ttk.Label(mqtt_frame, text="CA cert (required for AWS IoT Core: AmazonRootCA1.pem)").pack(anchor="w", padx=4)
        self.mqtt_ca_cert = self._file_picker_row(mqtt_frame)
        ttk.Label(mqtt_frame, text="Client cert (mTLS device certificate)").pack(anchor="w", padx=4)
        self.mqtt_client_cert = self._file_picker_row(mqtt_frame)
        ttk.Label(mqtt_frame, text="Client private key (mTLS device private key)").pack(anchor="w", padx=4)
        self.mqtt_client_key = self._file_picker_row(mqtt_frame)

        mctrl = ttk.Frame(mqtt_frame)
        mctrl.pack(fill="x", padx=4, pady=6)
        self.mqtt_connect_btn = ttk.Button(mctrl, text="Connect", command=self.mqtt_connect)
        self.mqtt_connect_btn.pack(side="left", padx=4)
        self.mqtt_disconnect_btn = ttk.Button(mctrl, text="Disconnect", command=self.mqtt_disconnect, state="disabled")
        self.mqtt_disconnect_btn.pack(side="left", padx=4)
        self.mqtt_publish_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(mctrl, text="Actually publish each merge cycle", variable=self.mqtt_publish_var).pack(side="left", padx=12)
        self.mqtt_status_label = ttk.Label(mctrl, text="Status: not connected", foreground="gray")
        self.mqtt_status_label.pack(side="right", padx=4)

        log_frame = ttk.LabelFrame(root, text="JSON send log")
        log_frame.pack(fill="both", expand=True, padx=20, pady=6)
        log_split = ttk.Frame(log_frame)
        log_split.pack(fill="both", expand=True)

        self.log_tree = ttk.Treeview(log_split, columns=("time", "metrics", "published"), show="headings", height=8)
        self.log_tree.heading("time", text="Time")
        self.log_tree.heading("metrics", text="Metric Count")
        self.log_tree.heading("published", text="Published?")
        self.log_tree.column("time", width=120)
        self.log_tree.column("metrics", width=90)
        self.log_tree.column("published", width=80)
        self.log_tree.pack(side="left", fill="both", expand=False)
        self.log_tree.bind("<<TreeviewSelect>>", self._show_log_detail)

        self.log_detail = tk.Text(log_split, font=("Consolas", 9))
        self.app.dark_aware_texts.append(self.log_detail)
        self.log_detail.pack(side="left", fill="both", expand=True, padx=(6, 0))

    def _labeled_entry(self, parent, label, default, show=None):
        ttk.Label(parent, text=label).pack(side="left")
        e = ttk.Entry(parent, width=20, show=show)
        e.insert(0, default)
        e.pack(side="left", padx=(2, 12))
        return e

    def _file_picker_row(self, parent):
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=4, pady=2)
        entry = ttk.Entry(row, width=60)
        entry.pack(side="left", padx=(0, 4))

        def browse():
            path = filedialog.askopenfilename(filetypes=[("Certificate/Key files", "*.pem *.crt *.key"), ("All files", "*.*")])
            if path:
                entry.delete(0, "end")
                entry.insert(0, path)

        ttk.Button(row, text="Browse...", command=browse).pack(side="left")
        return entry

    def _current_metrics(self):
        if self.group_by_conn.get():
            return {name: c.latest_values for name, c in self.app.connections.items() if c.latest_values}
        metrics = {}
        for c in self.app.connections.values():
            metrics.update(c.latest_values)
        return metrics

    def build_payload(self):
        try:
            template = json.loads(self.template_text.get("1.0", "end"))
        except json.JSONDecodeError as e:
            return None, f"Template is not valid JSON: {e}"
        sentinel_values = {
            "__DEVICE_ID__": self.device_id_entry.get().strip() or "AG221-Edge-01",
            "__TIMESTAMP__": int(time.time()),
            "__METRICS__": self._current_metrics(),
        }
        return substitute_template(template, sentinel_values), None

    def preview_json_now(self):
        payload, error = self.build_payload()
        self.log_detail.delete("1.0", "end")
        if error:
            self.log_detail.insert("1.0", error)
            return
        self.log_detail.insert("1.0", json.dumps(payload, indent=2))
        if not self._current_metrics():
            self.log_detail.insert("end", "\n\n(No live values yet -- Start All on Connections, wait a couple of cycles.)")

    def mqtt_connect(self):
        host = self.mqtt_host.get().strip()
        if not host:
            messagebox.showerror("Missing host", "Enter a broker host first")
            return
        try:
            port = int(self.mqtt_port.get().strip())
        except ValueError:
            messagebox.showerror("Invalid port", "Port must be a number")
            return

        self.mqtt_status_label.configure(text="Status: connecting...", foreground="orange")
        self.update_idletasks()

        ok = self.app.mqtt.connect(
            host=host, port=port, client_id=self.device_id_entry.get().strip(),
            username=self.mqtt_username.get().strip(), password=self.mqtt_password.get(),
            use_tls=self.mqtt_use_tls.get(),
            ca_cert=self.mqtt_ca_cert.get().strip() or None,
            client_cert=self.mqtt_client_cert.get().strip() or None,
            client_key=self.mqtt_client_key.get().strip() or None,
        )
        if ok:
            self.mqtt_status_label.configure(text="Status: connected", foreground="green")
            self.mqtt_connect_btn.configure(state="disabled")
            self.mqtt_disconnect_btn.configure(state="normal")
        else:
            self.mqtt_status_label.configure(text="Status: connect failed", foreground="red")

    def mqtt_disconnect(self):
        self.app.mqtt.disconnect()
        self.mqtt_status_label.configure(text="Status: not connected", foreground="gray")
        self.mqtt_connect_btn.configure(state="normal")
        self.mqtt_disconnect_btn.configure(state="disabled")

    def _show_log_detail(self, _event):
        sel = self.log_tree.selection()
        if not sel:
            return
        idx = self.log_tree.index(sel[0])
        ts, payload = self.app.send_log[idx]
        self.log_detail.delete("1.0", "end")
        self.log_detail.insert("1.0", json.dumps(payload, indent=2))

    def log_send(self, ts, payload, published):
        self.app.send_log.append((ts, payload))
        self.log_tree.insert("", "end", values=(
            ts, self._count_metrics(self._current_metrics()),
            "yes" if published else ("--" if not self.mqtt_publish_var.get() else "FAILED")
        ))
        self.log_tree.yview_moveto(1)

    @staticmethod
    def _count_metrics(metrics):
        total = 0
        for v in metrics.values():
            total += len(v) if isinstance(v, dict) else 1
        return total


# --- Network Tools ------------------------------------------------------------

KNOWN_PORTS = {
    502: "Modbus TCP",
    102: "S7comm (Siemens)",
    4840: "OPC UA",
    47808: "BACnet/IP",
    21: "FTP", 22: "SSH", 23: "Telnet", 80: "HTTP", 443: "HTTPS", 3389: "RDP",
}
MC_PROTOCOL_RANGE = range(5000, 5021)  # commonly used, but user-configurable -- see note in UI


def parse_port_spec(spec: str) -> list:
    ports = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-")
            ports.update(range(int(start.strip()), int(end.strip()) + 1))
        else:
            ports.add(int(part))
    return sorted(ports)


def label_port(port: int) -> str:
    if port in KNOWN_PORTS:
        return KNOWN_PORTS[port]
    if port in MC_PROTOCOL_RANGE:
        return "Possible MC Protocol / SLMP (config-dependent port)"
    return "--"


# --- Scan Results (persistent -- survives closing the scan dialog) ---------

class ScanResultsScreen(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.filter_vars = {}       # type_label -> BooleanVar, persists across refreshes
        self._last_distinct_types = None
        self._build()

    def _build(self):
        header = ttk.Frame(self)
        header.pack(fill="x", padx=20, pady=(20, 4))
        ttk.Label(header, text="Scan results", font=("Segoe UI", 16)).pack(side="left")
        self.status_label = ttk.Label(header, text="", foreground="gray")
        self.status_label.pack(side="right")

        ttk.Label(
            self,
            text="Results from every scan you've run this session, across all connections -- "
                 "runs in the background, so it's safe to switch away and check back later. "
                 "Sorted with changed addresses first. Raw value is the register's actual bit "
                 "pattern; 'Possible float32' pairs an address with the next one and checks "
                 "whether that combination decodes to a believable number. Only shown when at "
                 "least one of the pair actually changed during the scan -- a static pattern "
                 "that happens to look plausible is common by pure coincidence across thousands "
                 "of addresses and isn't real evidence on its own. Still just a hint worth "
                 "checking, not a guaranteed answer.",
            foreground="gray", wraplength=1000,
        ).pack(anchor="w", padx=20, pady=(0, 8))

        btn_row = ttk.Frame(self)
        btn_row.pack(fill="x", padx=20, pady=4)
        ttk.Button(btn_row, text="Add selected to tag list", command=self.add_selected).pack(side="left", padx=2)
        ttk.Button(btn_row, text="Clear all results", command=self.clear_results).pack(side="left", padx=2)
        ttk.Button(btn_row, text="Stop running scan", command=self.stop_scan).pack(side="left", padx=2)

        search_row = ttk.Frame(self)
        search_row.pack(fill="x", padx=20, pady=(4, 0))
        ttk.Label(search_row, text="Search for a known value:").pack(side="left")
        self.search_entry = ttk.Entry(search_row, width=12)
        self.search_entry.pack(side="left", padx=(6, 12))
        ttk.Label(search_row, text="+/- tolerance:").pack(side="left")
        self.tolerance_entry = ttk.Entry(search_row, width=8)
        self.tolerance_entry.insert(0, "0.5")
        self.tolerance_entry.pack(side="left", padx=(6, 12))
        ttk.Button(search_row, text="Search", command=self.run_search).pack(side="left", padx=2)
        ttk.Button(search_row, text="Clear search", command=self.clear_search).pack(side="left", padx=2)
        ttk.Label(
            self,
            text="Checks every already-scanned address against int16, uint16 (raw), int32, "
                 "uint32, and float32 (both byte orders) for that address paired with the next "
                 "one. Searches the last scan's snapshot -- if the real value has moved since "
                 "then, re-run the scan first.",
            foreground="gray", wraplength=1000,
        ).pack(anchor="w", padx=20, pady=(0, 4))
        self.search_target = None
        self.search_tolerance = 0.5

        self.filter_frame = ttk.Frame(self)
        self.filter_frame.pack(fill="x", padx=20, pady=(4, 4))

        columns = ("connection", "job", "type", "address", "changed", "raw", "latest", "float_guess", "match")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=18)
        headings = [
            ("connection", "Connection", 110), ("job", "Scan job", 120), ("type", "Type", 50),
            ("address", "Address", 100), ("changed", "Changed?", 70),
            ("raw", "Raw value", 120), ("latest", "As int16", 80),
            ("float_guess", "Possible float32", 150), ("match", "Search match", 160),
        ]
        for col, label, w in headings:
            self.tree.heading(col, text=label)
            self.tree.column(col, width=w, anchor="center")
        self.tree.pack(fill="both", expand=True, padx=20, pady=8)

    def run_search(self):
        try:
            self.search_target = float(self.search_entry.get().strip())
            self.search_tolerance = float(self.tolerance_entry.get().strip())
        except ValueError:
            messagebox.showerror("Invalid input", "Search value and tolerance must be numbers")
            return
        self.refresh()

    def clear_search(self):
        self.search_target = None
        self.search_entry.delete(0, "end")
        self.refresh()

    def update_status(self):
        running = " (running)" if self.app.scan_running else ""
        self.status_label.configure(text=f"{self.app.scan_status_text}{running}")

    def _rebuild_filters(self, distinct_types):
        for child in self.filter_frame.winfo_children():
            child.destroy()
        if not distinct_types:
            return
        ttk.Label(self.filter_frame, text="Filter by type:").pack(side="left", padx=(0, 8))
        for t in distinct_types:
            if t not in self.filter_vars:
                self.filter_vars[t] = tk.BooleanVar(value=True)
            ttk.Checkbutton(
                self.filter_frame, text=t, variable=self.filter_vars[t], command=self.refresh
            ).pack(side="left", padx=4)
        ttk.Button(self.filter_frame, text="All", command=self._select_all_filters).pack(side="left", padx=(12, 2))
        ttk.Button(self.filter_frame, text="None", command=self._select_no_filters).pack(side="left", padx=2)

    def _select_all_filters(self):
        for v in self.filter_vars.values():
            v.set(True)
        self.refresh()

    def _select_no_filters(self):
        for v in self.filter_vars.values():
            v.set(False)
        self.refresh()

    def _search_match_text(self, r):
        """Returns a description of which interpretation(s) matched the
        active search target, or None if nothing about this row matches."""
        if self.search_target is None:
            return ""
        target, tol = self.search_target, self.search_tolerance
        matches = []

        if r.get("latest") is not None and abs(r["latest"] - target) <= tol:
            matches.append(f"int16={r['latest']}")
        raw = r.get("raw_value")
        if raw is not None and abs(raw - target) <= tol:
            matches.append(f"uint16={raw}")
        for type_name, value in (r.get("candidates_32bit") or {}).items():
            if abs(value - target) <= tol:
                matches.append(f"{type_name}={value}")

        return ", ".join(matches) if matches else None

    def refresh(self):
        distinct_types = sorted({r.get("type_label", "?") for r in self.app.scan_results})
        if distinct_types != self._last_distinct_types:
            self._rebuild_filters(distinct_types)
            self._last_distinct_types = distinct_types

        active_types = {t for t, v in self.filter_vars.items() if v.get()}

        self.tree.delete(*self.tree.get_children())
        rows = sorted(self.app.scan_results, key=lambda r: not r["changed"])
        shown = 0
        for i, r in enumerate(rows):
            type_label = r.get("type_label", "?")
            if self.filter_vars and type_label not in active_types:
                continue

            match_text = self._search_match_text(r)
            if self.search_target is not None and match_text is None:
                continue  # active search and this row doesn't match anything -- hide it

            iid = f"{r['conn_name']}::{r['job_label']}::{r['name']}::{i}"
            float_guess = r.get("float32_guess")
            float_text = f"{float_guess} (with {r.get('float32_pair_with')})" if float_guess is not None else "--"
            raw = r.get("raw_value")
            raw_text = f"{raw} (0x{raw:04X})" if raw is not None else "--"
            self.tree.insert("", "end", iid=iid, values=(
                r["conn_name"], r["job_label"], type_label, r["name"],
                "Yes" if r["changed"] else "No", raw_text, r["latest"], float_text,
                match_text or "",
            ))
            shown += 1

        if self.search_target is not None:
            self.status_label.configure(
                text=f"{shown} address(es) match {self.search_target} +/- {self.search_tolerance}"
            )
        else:
            self.update_status()

    def add_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Select rows", "Select one or more results to add first")
            return

        rows = sorted(self.app.scan_results, key=lambda r: not r["changed"])
        by_iid = {}
        for i, r in enumerate(rows):
            iid = f"{r['conn_name']}::{r['job_label']}::{r['name']}::{i}"
            by_iid[iid] = r

        added, skipped, missing_conn = 0, 0, 0
        for iid in sel:
            r = by_iid.get(iid)
            if not r:
                continue
            conn = self.app.connections.get(r["conn_name"])
            if not conn:
                missing_conn += 1
                continue
            tag = dict(r["tag"])
            if any(t["name"] == tag["name"] for t in conn.tags):
                skipped += 1
                continue
            conn.tags.append(tag)
            added += 1

        self.app.refresh_dashboard()
        if "connections" in self.app.screens:
            self.app.screens["connections"].refresh()
        messagebox.showinfo(
            "Added", f"Added {added} tag(s), skipped {skipped} (already existed), "
                     f"{missing_conn} referenced a connection that no longer exists.\n"
                     f"Check data types in Manage Tags -- the scan assumes int16."
        )

    def clear_results(self):
        if not self.app.scan_results:
            return
        if messagebox.askyesno("Clear results", "Remove all scan results from this list?"):
            self.app.scan_results.clear()
            self.refresh()

    def stop_scan(self):
        if not self.app.scan_running:
            messagebox.showinfo("No scan running", "There's no scan currently in progress")
            return
        self.app.stop_background_scan()


class NetworkToolsScreen(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self._ip_queue = queue.Queue()
        self._port_queue = queue.Queue()
        self._ip_scanning = False
        self._port_scanning = False
        self._build()

    def _build(self):
        ttk.Label(self, text="Network tools", font=("Segoe UI", 16)).pack(anchor="w", padx=20, pady=(20, 4))
        ttk.Label(
            self, text="Find PLCs and check which industrial ports are open, without a "
                       "separate tool like Advanced IP Scanner or ModScan.",
            foreground="gray", wraplength=900,
        ).pack(anchor="w", padx=20, pady=(0, 12))

        ip_frame = ttk.LabelFrame(self, text="IP scanner")
        ip_frame.pack(fill="both", expand=True, padx=20, pady=6)

        row = ttk.Frame(ip_frame)
        row.pack(fill="x", padx=8, pady=6)
        ttk.Label(row, text="Base (first 3 octets)").pack(side="left")
        self.ip_base_entry = ttk.Entry(row, width=16)
        self.ip_base_entry.insert(0, "192.168.3")
        self.ip_base_entry.pack(side="left", padx=(4, 12))
        ttk.Label(row, text="Start").pack(side="left")
        self.ip_start_entry = ttk.Entry(row, width=6)
        self.ip_start_entry.insert(0, "1")
        self.ip_start_entry.pack(side="left", padx=(4, 12))
        ttk.Label(row, text="End").pack(side="left")
        self.ip_end_entry = ttk.Entry(row, width=6)
        self.ip_end_entry.insert(0, "254")
        self.ip_end_entry.pack(side="left", padx=(4, 12))
        self.ip_scan_btn = ttk.Button(row, text="Scan", command=self.start_ip_scan)
        self.ip_scan_btn.pack(side="left", padx=4)
        self.ip_progress_label = ttk.Label(row, text="", foreground="gray")
        self.ip_progress_label.pack(side="left", padx=12)

        self.ip_tree = ttk.Treeview(ip_frame, columns=("ip", "status"), show="headings", height=8)
        self.ip_tree.heading("ip", text="IP address")
        self.ip_tree.heading("status", text="Status")
        self.ip_tree.column("ip", width=160, anchor="center")
        self.ip_tree.column("status", width=160, anchor="center")
        self.ip_tree.pack(fill="both", expand=True, padx=8, pady=8)

        port_frame = ttk.LabelFrame(self, text="Port scanner")
        port_frame.pack(fill="both", expand=True, padx=20, pady=6)

        prow = ttk.Frame(port_frame)
        prow.pack(fill="x", padx=8, pady=6)
        ttk.Label(prow, text="Target IP").pack(side="left")
        self.port_ip_entry = ttk.Entry(prow, width=16)
        self.port_ip_entry.pack(side="left", padx=(4, 12))
        ttk.Label(prow, text="Ports").pack(side="left")
        self.ports_entry = ttk.Entry(prow, width=40)
        self.ports_entry.insert(0, "502,102,4840,47808,5000-5020")
        self.ports_entry.pack(side="left", padx=(4, 12))
        self.port_scan_btn = ttk.Button(prow, text="Scan ports", command=self.start_port_scan)
        self.port_scan_btn.pack(side="left", padx=4)

        self.port_tree = ttk.Treeview(port_frame, columns=("port", "status", "likely"), show="headings", height=8)
        self.port_tree.heading("port", text="Port")
        self.port_tree.heading("status", text="Status")
        self.port_tree.heading("likely", text="Likely protocol")
        self.port_tree.column("port", width=100, anchor="center")
        self.port_tree.column("status", width=100, anchor="center")
        self.port_tree.column("likely", width=320, anchor="w")
        self.port_tree.pack(fill="both", expand=True, padx=8, pady=8)

    def start_ip_scan(self):
        if self._ip_scanning:
            return
        try:
            base = self.ip_base_entry.get().strip()
            start = int(self.ip_start_entry.get().strip())
            end = int(self.ip_end_entry.get().strip())
        except ValueError:
            messagebox.showerror("Invalid input", "Start/End must be numbers")
            return
        if not (0 <= start <= 255 and 0 <= end <= 255 and start <= end):
            messagebox.showerror("Invalid range", "Start/End must be 0-255, Start <= End")
            return

        self.ip_tree.delete(*self.ip_tree.get_children())
        self._ip_scanning = True
        self.ip_scan_btn.configure(state="disabled")
        total = end - start + 1
        self.ip_progress_label.configure(text=f"Scanning 0/{total}...")

        thread = threading.Thread(target=self._run_ip_scan, args=(base, start, end), daemon=True)
        thread.start()
        self._pump_ip_queue(total)

    def _run_ip_scan(self, base, start, end):
        import concurrent.futures

        def ping_host(host_num):
            ip = f"{base}.{host_num}"
            try:
                if sys.platform.startswith("win"):
                    cmd = ["ping", "-n", "1", "-w", "500", ip]
                else:
                    cmd = ["ping", "-c", "1", "-W", "1", ip]
                result = subprocess.run(cmd, capture_output=True, timeout=2)
                reachable = result.returncode == 0
            except Exception:
                reachable = False
            return ip, reachable

        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
            futures = [pool.submit(ping_host, n) for n in range(start, end + 1)]
            for future in concurrent.futures.as_completed(futures):
                self._ip_queue.put(future.result())
        self._ip_queue.put(None)  # sentinel: scan complete

    def _pump_ip_queue(self, total):
        done_count = 0
        try:
            while True:
                item = self._ip_queue.get_nowait()
                if item is None:
                    self._ip_scanning = False
                    self.ip_scan_btn.configure(state="normal")
                    self.ip_progress_label.configure(text=f"Done -- {total} addresses checked")
                    return
                ip, reachable = item
                done_count += 1
                if reachable:
                    self.ip_tree.insert("", "end", values=(ip, "Reachable"))
        except queue.Empty:
            pass
        if self._ip_scanning:
            self.after(150, lambda: self._pump_ip_queue(total))

    def start_port_scan(self):
        if self._port_scanning:
            return
        ip = self.port_ip_entry.get().strip()
        if not ip:
            messagebox.showerror("Missing IP", "Enter a target IP first")
            return
        try:
            ports = parse_port_spec(self.ports_entry.get().strip())
        except ValueError:
            messagebox.showerror("Invalid ports", "Use comma-separated ports or ranges, e.g. 502,4840,5000-5020")
            return
        if not ports:
            messagebox.showerror("No ports", "Enter at least one port")
            return

        self.port_tree.delete(*self.port_tree.get_children())
        self._port_scanning = True
        self.port_scan_btn.configure(state="disabled")

        thread = threading.Thread(target=self._run_port_scan, args=(ip, ports), daemon=True)
        thread.start()
        self._pump_port_queue()

    def _run_port_scan(self, ip, ports):
        import socket as socket_mod
        import concurrent.futures

        def check_port(port):
            sock = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_STREAM)
            sock.settimeout(0.6)
            try:
                result = sock.connect_ex((ip, port))
                open_ = (result == 0)
            except Exception:
                open_ = False
            finally:
                sock.close()
            return port, open_

        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
            futures = [pool.submit(check_port, p) for p in ports]
            for future in concurrent.futures.as_completed(futures):
                self._port_queue.put(future.result())
        self._port_queue.put(None)

    def _pump_port_queue(self):
        try:
            while True:
                item = self._port_queue.get_nowait()
                if item is None:
                    self._port_scanning = False
                    self.port_scan_btn.configure(state="normal")
                    return
                port, open_ = item
                if open_:
                    self.port_tree.insert("", "end", values=(port, "Open", label_port(port)))
        except queue.Empty:
            pass
        if self._port_scanning:
            self.after(150, self._pump_port_queue)


# --- placeholder screens -----------------------------------------------------

class PlaceholderScreen(ttk.Frame):
    def __init__(self, parent, title, note):
        super().__init__(parent)
        ttk.Label(self, text=title, font=("Segoe UI", 16)).pack(anchor="w", padx=20, pady=(20, 4))
        ttk.Label(self, text=note, foreground="gray", wraplength=760, justify="left").pack(
            anchor="w", padx=20, pady=(0, 12)
        )


# --- Settings ------------------------------------------------------------------

class SettingsScreen(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        ttk.Label(self, text="Settings", font=("Segoe UI", 16)).pack(anchor="w", padx=20, pady=(20, 4))

        theme_frame = ttk.LabelFrame(self, text="Appearance")
        theme_frame.pack(fill="x", padx=20, pady=6)

        if SV_TTK_AVAILABLE:
            self.theme_var = tk.StringVar(value=self.app.current_theme)
            row = ttk.Frame(theme_frame)
            row.pack(fill="x", padx=8, pady=8)
            ttk.Radiobutton(row, text="Dark", variable=self.theme_var, value="dark",
                            command=self._on_theme_change).pack(side="left", padx=(0, 12))
            ttk.Radiobutton(row, text="Light", variable=self.theme_var, value="light",
                            command=self._on_theme_change).pack(side="left")
        else:
            ttk.Label(
                theme_frame,
                text="Install the sv-ttk package for the full dark/light theme:\n"
                     "pip install sv-ttk\n\nThen restart ProtoProbe.",
                foreground="gray", justify="left",
            ).pack(anchor="w", padx=8, pady=8)

        about_frame = ttk.LabelFrame(self, text="About")
        about_frame.pack(fill="x", padx=20, pady=6)
        ttk.Label(about_frame, text=f"{APP_NAME} v{APP_VERSION}", font=("Segoe UI", 11)).pack(
            anchor="w", padx=8, pady=(8, 0)
        )
        ttk.Label(about_frame, text=APP_TAGLINE, foreground="gray", wraplength=700).pack(
            anchor="w", padx=8, pady=(0, 8)
        )

    def _on_theme_change(self):
        self.app.apply_theme(self.theme_var.get())


# --- App shell -----------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1080x780")

        self.oee_config = {}
        self.oee_runtime = {
            "run_seconds": 0.0,
            "last_check_ts": None,
            "first_total_count": None,
            "last_total_count": None,
            "first_reject_count": None,
            "last_reject_count": None,
            "last_run_status": None,
        }
        self.connections = {}
        self.out_queue = queue.Queue()
        self.send_log = []
        self.merge_timer_running = False
        self.mqtt = MQTTPublisher()
        self.screens = {}
        self.output_screen = None
        self._logo_images = {}
        self.dark_aware_texts = []
        self.current_theme = "dark"

        self.scan_results = []
        self.scan_out_queue = queue.Queue()
        self.scan_status_text = "Idle -- no scan running"
        self.scan_running = False
        self.scan_stop_event = threading.Event()

        self._set_window_icon()
        self._build_menu()
        self._build_shell()
        self.apply_theme(self.current_theme)
        self.show_screen("dashboard")
        self.after(150, self._poll_queue)
        self.after(200, self._scan_pump)

    def start_background_scan(self, target_fn, args):
        """Launches a scan job (single-area or full-PLC) on a daemon
        thread. Safe to call while another scan is already running --
        results from both just interleave in scan_out_queue/scan_results,
        distinguished by connection name and job label."""
        self.scan_stop_event.clear()
        self.scan_running = True
        self.scan_status_text = "Starting..."
        threading.Thread(target=target_fn, args=args + (self.scan_stop_event,), daemon=True).start()

    def stop_background_scan(self):
        self.scan_stop_event.set()
        self.scan_status_text = "Stopping..."

    def _scan_pump(self):
        try:
            while True:
                kind, data = self.scan_out_queue.get_nowait()
                if kind == "status":
                    self.scan_status_text = data
                elif kind == "results":
                    conn_name, job_label, results = data
                    for r in results:
                        self.scan_results.append({
                            "conn_name": conn_name, "job_label": job_label,
                            "name": r["name"], "tag": r["tag"],
                            "changed": r["changed"], "latest": r["latest"],
                            "float32_guess": r.get("float32_guess"),
                            "float32_pair_with": r.get("float32_pair_with"),
                            "raw_value": r.get("raw_value"),
                            "type_label": r.get("type_label", "?"),
                            "candidates_32bit": r.get("candidates_32bit", {}),
                        })
                    if "scan_results" in self.screens:
                        self.screens["scan_results"].refresh()
                elif kind == "job_done":
                    self.scan_running = False
                    self.scan_status_text = "Done"
                if "scan_results" in self.screens:
                    self.screens["scan_results"].update_status()
        except queue.Empty:
            pass
        self.after(200, self._scan_pump)

    def apply_theme(self, theme_name: str):
        self.current_theme = theme_name
        if SV_TTK_AVAILABLE:
            sv_ttk.set_theme(theme_name)

        if theme_name == "dark":
            text_bg, text_fg, cursor = "#1e1e1e", "#e6e6e6", "#e6e6e6"
        else:
            text_bg, text_fg, cursor = "#ffffff", "#000000", "#000000"

        for widget in self.dark_aware_texts:
            try:
                widget.configure(bg=text_bg, fg=text_fg, insertbackground=cursor)
            except tk.TclError:
                pass  # widget may have been destroyed

    def _set_window_icon(self):
        ico_path = resource_path(os.path.join("assets", "protoprobe.ico"))
        try:
            self.iconbitmap(ico_path)
            return
        except tk.TclError:
            pass
        png_path = resource_path(os.path.join("assets", "protoprobe_32.png"))
        try:
            img = tk.PhotoImage(file=png_path)
            self._logo_images["window_icon"] = img
            self.iconphoto(True, img)
        except tk.TclError:
            pass

    def _build_menu(self):
        menubar = tk.Menu(self)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Exit", command=self.destroy)
        menubar.add_cascade(label="File", menu=file_menu)
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About ProtoProbe", command=self.show_about)
        menubar.add_cascade(label="Help", menu=help_menu)
        self.config(menu=menubar)

    def show_about(self):
        win = tk.Toplevel(self)
        win.title(f"About {APP_NAME}")
        win.geometry("360x300")
        win.resizable(False, False)
        win.grab_set()

        png_path = resource_path(os.path.join("assets", "protoprobe_128.png"))
        try:
            logo_img = tk.PhotoImage(file=png_path)
            self._logo_images["about"] = logo_img
            tk.Label(win, image=logo_img).pack(pady=(20, 8))
        except tk.TclError:
            pass

        ttk.Label(win, text=APP_NAME, font=("Segoe UI", 16, "bold")).pack()
        ttk.Label(win, text=f"Version {APP_VERSION}", foreground="gray").pack(pady=(0, 8))
        ttk.Label(win, text=APP_TAGLINE, wraplength=300, justify="center", foreground="gray").pack(padx=20)
        ttk.Label(
            win, text="Modbus TCP/RTU  ·  S7comm  ·  MC Protocol  ·  OPC UA  ·  BACnet/IP",
            font=("Segoe UI", 9), foreground="gray",
        ).pack(pady=(12, 0))
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=16)

    def _build_shell(self):
        container = ttk.Frame(self)
        container.pack(fill="both", expand=True)

        sidebar = ttk.Frame(container, width=180)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        header = ttk.Frame(sidebar)
        header.pack(fill="x", padx=14, pady=(16, 4))
        png_path = resource_path(os.path.join("assets", "protoprobe_32.png"))
        try:
            logo_img = tk.PhotoImage(file=png_path)
            self._logo_images["sidebar"] = logo_img
            ttk.Label(header, image=logo_img).pack(side="left", padx=(0, 8))
        except tk.TclError:
            pass
        ttk.Label(header, text=APP_NAME, font=("Segoe UI", 13, "bold")).pack(side="left")
        ttk.Label(sidebar, text=f"v{APP_VERSION}", font=("Segoe UI", 8), foreground="gray").pack(
            anchor="w", padx=16, pady=(0, 16)
        )

        self.nav_buttons = {}
        for key, label in NAV_ITEMS:
            btn = ttk.Button(sidebar, text=label, command=lambda k=key: self.show_screen(k))
            btn.pack(fill="x", padx=10, pady=2)
            self.nav_buttons[key] = btn

        content = ttk.Frame(container)
        content.pack(side="left", fill="both", expand=True)

        self.screens["dashboard"] = DashboardScreen(content, self)
        self.screens["connections"] = ConnectionsScreen(content, self)
        self.screens["scan_results"] = ScanResultsScreen(content, self)
        self.screens["live_values"] = LiveValuesScreen(content, self)
        self.screens["oee"] = OEEScreen(content, self)
        self.screens["output"] = OutputScreen(content, self)
        self.output_screen = self.screens["output"]
        self.screens["network"] = NetworkToolsScreen(content, self)
        self.screens["settings"] = SettingsScreen(content, self)

        for screen in self.screens.values():
            screen.place(x=0, y=0, relwidth=1, relheight=1)

    def show_screen(self, key):
        self.screens[key].tkraise()
        for k, btn in self.nav_buttons.items():
            btn.state(["pressed"] if k == key else ["!pressed"])
        if key == "dashboard":
            self.screens["dashboard"].refresh()
        elif key == "connections":
            self.screens["connections"].refresh()
        elif key == "live_values":
            self.screens["live_values"].refresh()
        elif key == "oee":
            self.screens["oee"].refresh()
        elif key == "scan_results":
            self.screens["scan_results"].refresh()

    def refresh_dashboard(self):
        self.screens["dashboard"].refresh()
        self.screens["connections"].refresh()
        self.screens["oee"].refresh()

    def get_flat_metrics(self):
        """Always-flat tag-name -> value map across every connection,
        independent of the Output screen's grouping toggle. This is
        what OEE tag-name lookups and the runtime tracker use."""
        metrics = {}
        for c in self.connections.values():
            metrics.update(c.latest_values)
        return metrics

    def _poll_queue(self):
        try:
            while True:
                kind, data = self.out_queue.get_nowait()
                if kind == "conn_status":
                    name, status = data
                    if name in self.connections:
                        self.connections[name].status = status
                        self.refresh_dashboard()
                elif kind == "values":
                    name, values, ts = data
                    if name in self.connections:
                        conn = self.connections[name]
                        conn.latest_values.update(values)
                        for tag_name in values:
                            conn.last_updated[tag_name] = ts
                        self.screens["live_values"].refresh()
                        self.update_oee_runtime()
                        self.refresh_dashboard()
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    def update_oee_runtime(self):
        """Called on every new poll reading. Accumulates real run time
        while the mapped Run Status tag reads true, and tracks the
        first/last Total Count and Reject Count seen this session."""
        cfg = self.oee_config
        metrics = self.get_flat_metrics()
        rt = self.oee_runtime
        now = time.time()

        run_status_tag = cfg.get("run_status_tag")
        run_status_val = metrics.get(run_status_tag) if run_status_tag else None
        if run_status_val is not None:
            is_running = bool(run_status_val)
            if rt["last_check_ts"] is not None and rt["last_run_status"]:
                rt["run_seconds"] += now - rt["last_check_ts"]
            rt["last_check_ts"] = now
            rt["last_run_status"] = is_running

        total_tag = cfg.get("total_count_tag")
        if total_tag and total_tag in metrics:
            val = metrics[total_tag]
            if rt["first_total_count"] is None or val < rt["last_total_count"]:
                # first reading this session, or counter reset (rolled over/restarted)
                rt["first_total_count"] = val
            rt["last_total_count"] = val

        reject_tag = cfg.get("reject_count_tag")
        if reject_tag and reject_tag in metrics:
            val = metrics[reject_tag]
            if rt["first_reject_count"] is None or val < rt["last_reject_count"]:
                rt["first_reject_count"] = val
            rt["last_reject_count"] = val

    def reset_oee_session(self):
        self.oee_runtime = {
            "run_seconds": 0.0, "last_check_ts": None,
            "first_total_count": None, "last_total_count": None,
            "first_reject_count": None, "last_reject_count": None,
            "last_run_status": None,
        }
        self.refresh_dashboard()

    def compute_oee(self):
        """Returns a dict with whatever of availability/cycles/mode/
        performance/quality/oee can be computed from the current
        mapping and session data. Missing pieces are None, not
        guessed -- the Dashboard shows '--' rather than a fake number."""
        cfg = self.oee_config
        rt = self.oee_runtime
        result = {"availability": None, "cycles": None, "mode": None,
                  "performance": None, "quality": None, "oee": None}

        if rt["last_run_status"] is not None:
            result["mode"] = "Running" if rt["last_run_status"] else "Stopped"

        try:
            planned_min = float(cfg.get("planned_time_min", "") or 0)
        except ValueError:
            planned_min = 0
        if planned_min > 0:
            result["availability"] = min(100.0, (rt["run_seconds"] / (planned_min * 60)) * 100)

        cycles = None
        if rt["first_total_count"] is not None and rt["last_total_count"] is not None:
            cycles = rt["last_total_count"] - rt["first_total_count"]
            result["cycles"] = cycles

        try:
            ideal_cycle_s = float(cfg.get("ideal_cycle_s", "") or 0)
        except ValueError:
            ideal_cycle_s = 0
        if ideal_cycle_s > 0 and cycles is not None and rt["run_seconds"] > 0:
            result["performance"] = min(100.0, (ideal_cycle_s * cycles / rt["run_seconds"]) * 100)

        if cycles is not None and cycles > 0 and rt["first_reject_count"] is not None and rt["last_reject_count"] is not None:
            rejects = rt["last_reject_count"] - rt["first_reject_count"]
            result["quality"] = max(0.0, min(100.0, ((cycles - rejects) / cycles) * 100))

        factors = [result["availability"], result["performance"], result["quality"]]
        if all(f is not None for f in factors):
            result["oee"] = (factors[0] / 100) * (factors[1] / 100) * (factors[2] / 100) * 100

        return result

    def start_merge_tick(self):
        self._merge_tick()

    def _merge_tick(self):
        if not self.merge_timer_running:
            return
        payload, error = self.output_screen.build_payload()
        if error is None and self.output_screen._current_metrics():
            ts = time.strftime("%H:%M:%S")
            published = False
            if self.output_screen.mqtt_publish_var.get() and self.mqtt.connected:
                topic = self.output_screen.mqtt_topic.get().strip() or "iiot/telemetry"
                published = self.mqtt.publish(topic, payload)
            self.output_screen.log_send(ts, payload, published)

        interval_s = float(self.output_screen.merge_interval_entry.get().strip() or 5)
        self.after(int(interval_s * 1000), self._merge_tick)


if __name__ == "__main__":
    app = App()
    app.mainloop()

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
import logging
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
APP_VERSION = "0.3.2"
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
from commissioning_screen import CommissioningScreen
from plc_reader import build_range_tags
import plc_catalog
import s7_address
import signal_hints
import workspace

NAV_ITEMS = [
    ("dashboard", "Dashboard"),
    ("connections", "Connections"),
    ("scan_results", "Scan results"),
    ("live_values", "Live values"),
    ("commission", "Commissioning"),
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


def explain_s7_error(message):
    """A plain-words hint for a snap7 error text, for when a probe or read of a DB fails. The texts are snap7's (they vary a little
    between versions), so this matches on the words that matter and returns None when it does not know the error."""
    m = str(message).lower()
    if "address out of range" in m or "item not available" in m or "invalid address" in m or "does not exist" in m:
        return ("that DB does not exist, or it has no variables yet (size 0), or it uses 'optimized block access' (then absolute addresses "
                "cannot be read: in TIA Portal turn off 'Optimized block access' for that DB and compile)")
    if "function not available" in m or "not allowed" in m or "access to object" in m or "protection" in m:
        return ("the PLC refuses the read: in TIA Portal enable 'Permit access with PUT/GET communication from remote partner' "
                "(S7-1200/1500) and check the CPU's protection level")
    if "timed out" in m or "timeout" in m or "recv tcp" in m or "connection reset" in m or "connection refused" in m or "unreachable" in m:
        return "the PLC does not answer on port 102: check the IP address, the cable or VPN, and that this PC is on the PLC's network"
    if "iso" in m and ("pdu" in m or "connect" in m or "negotiating" in m):
        return "the PLC refused the connection: check rack and slot (S7-1200/1500: rack 0, slot 1; S7-300: rack 0, slot 2)"
    return None


def read_tags_once(conn, tags, driver_factory=None):
    """Read several tags once over ONE connection: {name: (value, None)} or {name: (None, "what went wrong")}. The drivers log a failed tag
    instead of raising, so their warning text is collected and returned as the reason; a failure to connect is the reason for every tag."""
    collector = PollLogCollector()
    loggers = [logging.getLogger(n) for n in DRIVER_LOGGERS]
    for lg in loggers:
        lg.addHandler(collector)
    entries = []
    for tag in tags:
        entry = {"multiplier": 1.0, "offset": 0.0, "mqtt_key": tag["name"]}
        entry.update(tag)
        entries.append(entry)
    cfg = {"interface": conn.interface, "tags": entries}
    if conn.protocol in ("modbus_tcp", "modbus_rtu"):
        cfg = {"interface": conn.interface, "devices": [{"unit_id": conn.interface.get("unit_id", 1), "tags": entries}]}
    factory = driver_factory or DRIVER_FACTORY

    async def run():
        driver = factory[conn.protocol](cfg)
        await driver.connect()
        try:
            return await driver.poll()
        finally:
            try:
                await driver.disconnect()
            except Exception:
                pass

    try:
        values = asyncio.run(run())
    except Exception as exc:
        reason = str(exc) or exc.__class__.__name__
        return {t["name"]: (None, reason) for t in tags}
    finally:
        for lg in loggers:
            lg.removeHandler(collector)
    fallback = next(iter(collector.errors.values()), "no value came back")
    return {t["name"]: ((values[t["name"]], None) if t["name"] in values else (None, collector.errors.get(t["name"]) or fallback)) for t in tags}


def read_tag_once(conn, tag, driver_factory=None):
    """Read ONE tag once: (value, None) or (None, "what went wrong"). For the Test read and Read now buttons."""
    return read_tags_once(conn, [tag], driver_factory)[tag["name"]]


def discover_db_numbers_sync(conn, max_db, out_queue=None, errors=None):
    """Blocking-safe (runs inside an already-running asyncio.run via a
    nested async function) probe of DB1..max_db. Returns a list of DB
    numbers that responded. Pushes progress to out_queue if given.
    If `errors` is a list, every failed read is appended to it as (db, message), so
    the caller can tell "no DB exists" from "the PLC refuses every read"."""
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
            except Exception as exc:
                if errors is not None:
                    errors.append((db, str(exc)))
        await driver.disconnect()
        return found

    return asyncio.run(run_probe())


def hidden_subprocess_kwargs(platform=None):
    """Keyword arguments that start a child program (ping, arp ...) WITHOUT a window. ProtoProbe.exe has no console of its own, so on
    Windows every child process otherwise opens its own black console window: an IP scan of 254 addresses opened 254 of them, many at
    once, and they took the focus away so the app could not be used meanwhile. Elsewhere nothing is needed."""
    if not (platform or sys.platform).startswith("win"):
        return {}
    kwargs = {"creationflags": 0x08000000}                 # CREATE_NO_WINDOW
    if hasattr(subprocess, "STARTUPINFO"):                 # Windows only; also asks the program to start hidden
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = 0                               # SW_HIDE
        kwargs["startupinfo"] = info
    return kwargs


def ping_once(ip, platform=None, runner=None):
    """True if `ip` answers one ping. The ping runs hidden (see hidden_subprocess_kwargs). `runner` is a stand-in for subprocess.run in tests."""
    platform = platform or sys.platform
    cmd = ["ping", "-n", "1", "-w", "500", ip] if platform.startswith("win") else ["ping", "-c", "1", "-W", "1", ip]
    try:
        result = (runner or subprocess.run)(cmd, capture_output=True, timeout=2, **hidden_subprocess_kwargs(platform))
        return result.returncode == 0
    except Exception:
        return False


def run_db_discovery(conn, max_db, out_queue):
    """The background half of the "Discover DBs" button. Whatever happens it puts exactly one final message on out_queue, so the window
    can always recover: ("db_discovery_done", (dbs, errors)) or ("db_discovery_error", text). (Before this, a failed connection killed the
    thread silently and the button stayed disabled on "Probing..." for ever.)"""
    errs = []
    try:
        dbs = discover_db_numbers_sync(conn, max_db, out_queue, errs)
    except Exception as exc:
        out_queue.put(("db_discovery_error", str(exc) or exc.__class__.__name__))
        return
    out_queue.put(("db_discovery_done", (dbs, errs)))


def discovery_status(kind, data):
    """(text, colour) for the status line of the "Discover DBs" row, from a final message of run_db_discovery."""
    if kind == "db_discovery_error":
        hint = explain_s7_error(data)
        return f"Could not connect: {data}" + (f" -- {hint}" if hint else ""), "red"
    dbs, errs = data
    if dbs or not errs:
        return f"Done -- {len(dbs)} DB(s) found", "gray"
    missing = [(d, m) for d, m in errs if "does not exist" in m.lower()]
    refused = [(d, m) for d, m in errs if "does not exist" not in m.lower()]
    if missing and refused:
        # The PLC answers differently for a DB that is not there and for one that is there but refuses the read: the second kind exists.
        names = ", ".join(f"DB{d}" for d, _m in refused[:8]) + (" ..." if len(refused) > 8 else "")
        reason = refused[0][1]
        return (f"Done -- 0 readable. {names} {'exists' if len(refused) == 1 else 'exist'} but refuse{'s' if len(refused) == 1 else ''} the read ({reason}): "
                f"most likely the DB has no variables yet (size 0), or 'Optimized block access' is on for it, or the CPU's protection blocks it. "
                f"The other {len(missing)} probed DB(s) do not exist."), "red"
    counts = {}
    for _db, msg in errs:
        counts[msg] = counts.get(msg, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    reasons = "; ".join(f"{n} x {m}" for m, n in ranked[:2])
    hint = explain_s7_error(ranked[0][0])
    return (f"Done -- 0 DB(s) found; all {len(errs)} probed DBs failed ({reasons}; first: DB{errs[0][0]})"
            + (f" -- {hint}" if hint else "")), "red"


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


def is_bit_packed_area(protocol, tag):
    """True for memory that holds one digital signal per BIT (S7 process-image
    inputs I and outputs Q). A 16-bit/float32 reading of such a word is
    meaningless: it is 16 separate on/off channels, not a number. (Analog
    channels also live in I/Q but as whole 16-bit words -- that is why the
    int16 value stays visible.)"""
    return protocol == "s7comm" and tag.get("area") in ("I", "Q")


def s7_word_bits(area, start_byte, raw):
    """Bits set in one S7 word read at `start_byte`: S7 is big-endian, so the
    first byte is the HIGH byte. Returns [(byte, bit), ...]."""
    out = []
    for b in range(8):
        if (raw >> (8 + b)) & 1:
            out.append((start_byte, b))
    for b in range(8):
        if (raw >> b) & 1:
            out.append((start_byte + 1, b))
    return out


def format_bits(area, bits, changed=()):
    """'I0.3, I1.0*' (* = this bit changed during the scan)."""
    changed = set(changed)
    return ", ".join(f"{area}{b}.{k}" + ("*" if (b, k) in changed else "") for b, k in bits)


def hint_for_result(r):
    """What a scan row might be good for (signal_hints.Hint), from how it behaved during the scan; None when there is nothing to say."""
    if r.get("bit_area"):
        if r.get("bits_series"):
            return signal_hints.classify_bits(r["bits_series"])
        if r.get("bits_on"):
            return signal_hints.Hint("steady_on", "bit(s) on the whole time: a permanent condition (power ok, guard closed ...)")
        return signal_hints.Hint("idle", "no bit was on")
    return signal_hints.classify_word(r["values"]) if r.get("values") else None


def hint_text(hint):
    if hint is None:
        return ""
    tags = [f"OEE: {hint.oee}" if hint.oee else "", f"predictive: {hint.predictive}" if hint.predictive else ""]
    tags = [t for t in tags if t]
    return hint.text + (f"   [{'; '.join(tags)}]" if tags else "")


def bits_summary(area, bits_on, bits_changed):
    """Text for the results table: the bits that are on (* = changed during the scan) and the bits that changed but are off in the last
    snapshot. 'no bits on' when there is nothing to show."""
    changed = set(bits_changed)
    parts = []
    if bits_on:
        parts.append("on: " + format_bits(area, bits_on, changed))
    gone = sorted(changed - set(bits_on))
    if gone:
        parts.append("changed, now off: " + ", ".join(f"{area}{b}.{k}*" for b, k in gone))
    return "; ".join(parts) if parts else "no bits on"


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


# The drivers log under "edge_collector.s7", "edge_collector.mc", "edge_collector.modbus" ...; a handler on the parent catches all of them.
# (It was once written as "drivers.s7_driver", which no driver uses: a stand-in logger of that name made the tests pass while the real
# warnings still went to the console.)
DRIVER_LOGGERS = ("edge_collector",)


class ScanLogCollector(logging.Handler):
    """Collects the drivers' per-address warnings during a scan instead of letting Python print every one of them to the console (an
    unreadable range of 1000 addresses printed 1000 lines per snapshot). `counts` maps the error text to how many times it happened."""
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.counts = {}

    def emit(self, record):
        try:
            text = str(record.args[-1]) if record.args else record.getMessage()
        except Exception:
            text = "error"
        self.counts[text] = self.counts.get(text, 0) + 1

    def top(self):
        return max(self.counts, key=self.counts.get) if self.counts else None


def last_readable_offset(readable, start, end, step=2):
    """The highest offset in start..end (stepping by `step`) that `readable(offset)` accepts, found by halving: about log2(range) probes
    instead of one read per address. Assumes memory is readable from `start` up to some limit and not beyond. None if `start` itself is not readable."""
    if not readable(start):
        return None
    if readable(end):
        return end
    lo, hi = start, end                      # lo is readable, hi is not
    while hi - lo > step:
        mid = lo + max(step, ((hi - lo) // (2 * step)) * step)
        if readable(mid):
            lo = mid
        else:
            hi = mid
    return lo


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

    scan_tags = list(tags)                  # what is really scanned: for Siemens it is cut down to the readable part of the range
    batches = [scan_tags[i:i + SCAN_BATCH_SIZE] for i in range(0, len(scan_tags), SCAN_BATCH_SIZE)]

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
        if protocol == "s7comm":
            # Find where the PLC's memory really ends before reading it address by address: asking for memory that is not there
            # (an input area of 1024 bytes scanned up to 2000) only fails, thousands of times.
            first = scan_tags[0]
            area, db = first["area"], first.get("db_number", 0)
            loop = asyncio.get_event_loop()

            def readable(offset):
                try:
                    driver._read_area_blocking(area, db, offset, 2)
                    return True
                except Exception:
                    return False

            first_byte, last_byte = first["start_byte"], scan_tags[-1]["start_byte"]
            last_ok = await loop.run_in_executor(None, last_readable_offset, readable, first_byte, last_byte)
            if last_ok is None:
                out_queue.put(("status", f"[{job_label}] nothing readable at byte {first_byte}: the PLC has no memory here, or access is refused"))
                scan_tags[:] = []
                batches[:] = []
            elif last_ok < last_byte:
                scan_tags[:] = [t for t in scan_tags if t["start_byte"] <= last_ok]
                batches[:] = [scan_tags[i:i + SCAN_BATCH_SIZE] for i in range(0, len(scan_tags), SCAN_BATCH_SIZE)]
                out_queue.put(("status", f"[{job_label}] the PLC has {last_ok + 2 - first_byte} bytes here (up to byte {last_ok + 1}): scanning those, not up to {last_byte}"))
        for i in range(num_snapshots):
            done_count = 0
            for batch in batches:
                if stop_event is not None and stop_event.is_set():
                    out_queue.put(("status", f"[{job_label}] stopped -- reporting {done_count}/{len(scan_tags)} read so far"))
                    stopped_early = True
                    break
                set_batch_tags(batch)
                values = await driver.poll()
                snapshots[i].update(values)
                done_count += len(batch)
                out_queue.put((
                    "status",
                    f"[{job_label}] snapshot {i + 1}/{num_snapshots} -- {done_count}/{len(scan_tags)} read"
                ))
                out_queue.put(("progress", (i + done_count / max(1, len(scan_tags))) / max(1, num_snapshots)))
            if stopped_early:
                break
            if i < num_snapshots - 1:
                await asyncio.sleep(delay_s)
        await driver.disconnect()
        return snapshots

    collector = ScanLogCollector()
    loggers = [logging.getLogger(n) for n in DRIVER_LOGGERS]
    for lg in loggers:
        lg.addHandler(collector)            # quiet: a handler here also stops Python printing every warning to the console
    try:
        snapshots = asyncio.run(run_scan())
    except Exception as e:
        out_queue.put(("status", f"[{job_label}] failed: {e}"))
        return
    finally:
        for lg in loggers:
            lg.removeHandler(collector)
    if collector.counts:
        out_queue.put(("status", f"[{job_label}] {sum(collector.counts.values())} reads failed; most common: {collector.top()}"))

    results = []
    for tag in scan_tags:
        name = tag["name"]
        vals_present = [snap[name] for snap in snapshots if name in snap]
        if not vals_present:
            continue
        changed = len(set(vals_present)) > 1
        results.append({
            "name": name, "tag": tag, "changed": changed, "latest": vals_present[-1], "values": list(vals_present),
            "raw_value": _raw_word_from_int16(vals_present[-1]),
            "type_label": type_label_for_tag(protocol, tag),
        })
        if is_bit_packed_area(protocol, tag) or (protocol == "s7comm" and tag.get("area") == "M"):
            # I/Q: only bits. M holds flags too (clock memory, status bits) next to words and counters, so M rows get the bits as well as the word.
            raws = [_raw_word_from_int16(v) for v in vals_present]
            all_bits = set()
            for rw in raws:
                all_bits.update(s7_word_bits(tag["area"], tag["start_byte"], rw))
            now = set(s7_word_bits(tag["area"], tag["start_byte"], raws[-1]))
            ever_changed = {bt for bt in all_bits if len({bt in set(s7_word_bits(tag["area"], tag["start_byte"], rw)) for rw in raws}) > 1}
            results[-1]["bit_area"] = is_bit_packed_area(protocol, tag)
            results[-1]["bits_on"] = sorted(now)
            results[-1]["bits_changed"] = sorted(ever_changed)
            results[-1]["bits_series"] = {bt: [1 if bt in set(s7_word_bits(tag["area"], tag["start_byte"], rw)) else 0 for rw in raws]
                                          for bt in ever_changed}

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
        if result_by_name.get(name, {}).get("bit_area") or result_by_name.get(next_name, {}).get("bit_area"):
            continue  # digital inputs/outputs are bits, not numbers: no 32-bit/float reading
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


class PollLogCollector(logging.Handler):
    """Keeps the drivers' per-tag warnings of the latest poll ("[group] tag NAME decode error: TEXT"; the driver leaves that tag out of its answer),
    so the Live values screen can say WHICH tag failed and why. Also stops Python printing those warnings to the console."""
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.errors = {}

    def emit(self, record):
        try:
            args = record.args or ()
            tag = str(args[1]) if len(args) >= 2 else "*"
            text = str(args[-1]) if len(args) >= 3 else "read failed"
        except Exception:
            tag, text = "*", "error"
        self.errors[tag] = text

    def take(self):
        errors, self.errors = self.errors, {}
        return errors


def show_value(value):
    """A live value for the table: 1.0 -> 1, 20.0 -> 20 (the drivers scale everything as a float), other numbers as they are."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


HEALTH_COLOURS = {"green": "#2fb463", "amber": "#e0a100", "red": "#e0483f", "grey": "#8a8a8a"}


def connection_health(status, health, tag_count):
    """(colour, text) for one connection: green = connected and every tag reads, amber = connecting / some tags fail, red = no connection or no
    tag reads, grey = not polling. `status` is the connection's status string, `health` the poller's last report (or None)."""
    status = status or "idle"
    if status.startswith("error"):
        return "red", "No connection: " + status[len("error:"):].strip() if status.startswith("error:") else "No connection"
    if status in ("idle", "stopped"):
        return "grey", "Not reading"
    if status == "connecting":
        return "amber", "Connecting..."
    if not health or not health.get("polls"):
        return "amber", "Connected, waiting for the first reading"
    bad = len(health.get("bad", {}))
    if tag_count and bad >= tag_count:
        first = next(iter(health["bad"].values()), "no value")
        return "red", f"Connected, but no tag can be read ({first})"
    if bad:
        return "amber", f"Reading {max(0, tag_count - bad)} of {tag_count} tags; {bad} failing"
    return "green", f"Reading {tag_count} tag(s) OK, {health.get('cycle_ms', 0):.0f} ms per poll"


class Connection:
    def __init__(self, name, protocol, interface, plc_type=None, enabled=True, poll_interval_s=2.0):
        self.name = name
        self.protocol = protocol
        self.interface = interface
        self.enabled = enabled              # a disabled connection is kept but not polled
        self.poll_interval_s = poll_interval_s
        self.plc_type = plc_type            # the gateway's PLC-type name (plc_catalog), or None when only a protocol was chosen
        self.tags = []
        self.thread = None
        self.latest_values = {}
        self.last_updated = {}
        self.status = "idle"
        self.health = None                  # the poller's last report: polls, errors, cycle_ms, bad {tag: error text}


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
        collector = PollLogCollector()
        loggers = [logging.getLogger(n) for n in DRIVER_LOGGERS]
        for lg in loggers:
            lg.addHandler(collector)
        try:
            await self._poll_loop(driver, collector)
        finally:
            for lg in loggers:
                lg.removeHandler(collector)

    async def _poll_loop(self, driver, collector):
        polls = errors = 0
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
                polls += 1
                bad = collector.take()
                for t in self.conn.tags:                   # a tag with no value and no message still failed
                    if t["name"] not in values:
                        bad.setdefault(t["name"], "no value")
                known = {t["name"] for t in self.conn.tags}
                bad = {k: v for k, v in bad.items() if k in known}
                errors += len(bad)
                self.out_queue.put(("health", (self.conn.name, {
                    "polls": polls, "errors": errors, "cycle_ms": (time.monotonic() - cycle_start) * 1000, "bad": bad, "at": ts})))
                self.out_queue.put(("conn_status", (self.conn.name, "connected")))
            except Exception as e:
                errors += 1
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
        ttk.Button(btn_row, text="Read a Value", command=self.open_quick_read).pack(side="left", padx=2)
        ttk.Button(btn_row, text="Scan for Live Tags", command=self.open_scan_dialog).pack(side="left", padx=2)

        self.conn_tree = ttk.Treeview(
            self, columns=("protocol", "interface", "tags", "status"), show="headings", height=10
        )
        for colour, hex_colour in HEALTH_COLOURS.items():
            self.conn_tree.tag_configure(colour, foreground=hex_colour)
        for col, label, w in [("protocol", "Protocol", 200), ("interface", "Interface", 240),
                               ("tags", "# Tags", 60), ("status", "Status", 340)]:
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
        selected = self.conn_tree.selection()
        self.conn_tree.delete(*self.conn_tree.get_children())
        for name, conn in self.app.connections.items():
            iface_str = ", ".join(f"{k}={v}" for k, v in conn.interface.items())
            plc = getattr(conn, "plc_type", None)
            colour, text = connection_health(conn.status, getattr(conn, "health", None), len(conn.tags))
            if not getattr(conn, "enabled", True):
                colour, text = "grey", "Disabled: not read when polling starts"
            self.conn_tree.insert("", "end", iid=name, tags=(colour,), values=(
                PROTOCOL_LABELS[conn.protocol] + (f"  [{plc}]" if plc else ""), iface_str, len(conn.tags), "\u25cf " + text
            ))
        keep = [n for n in selected if n in self.app.connections]
        if keep:
            self.conn_tree.selection_set(keep)

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
        win.geometry("480x660")
        win.grab_set()

        ttk.Label(win, text="Connection Name").pack(anchor="w", padx=8, pady=(8, 0))
        name_entry = ttk.Entry(win, width=30)
        name_entry.insert(0, existing.name if existing else f"conn_{uuid.uuid4().hex[:4]}")
        name_entry.pack(anchor="w", padx=8)
        if existing:
            name_entry.configure(state="disabled")

        ttk.Label(win, text="PLC type (optional: the same list as the gateway's protocol box)").pack(anchor="w", padx=8, pady=(8, 0))
        type_entries = plc_catalog.dropdown_entries()
        type_texts = ["(choose the protocol below)"] + [t for t, _ in type_entries]
        type_key_of = {t: k for t, k in type_entries}
        type_cb = ttk.Combobox(win, values=type_texts, state="readonly", width=58)
        existing_type = getattr(existing, "plc_type", None) if existing else None
        type_cb.set(next((t for t, k in type_entries if k == existing_type), type_texts[0]))
        type_cb.pack(anchor="w", padx=8)
        type_overrides = {}                 # interface defaults of the chosen PLC type (for example rack and slot)

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

        extra = ttk.Frame(win)
        extra.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Label(extra, text="Poll every (s)").pack(side="left")
        poll_var = tk.StringVar(value=str(existing.poll_interval_s if existing else 2.0))
        ttk.Entry(extra, textvariable=poll_var, width=7).pack(side="left", padx=6)
        enabled_var = tk.BooleanVar(value=existing.enabled if existing else True)
        ttk.Checkbutton(extra, text="Enabled (read it when polling starts)", variable=enabled_var).pack(side="left", padx=12)

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
                if label in type_overrides:
                    val = str(type_overrides[label])
                e.insert(0, val)
                e.pack(side="left")
                field_entries[label] = e

            chosen = type_key_of.get(type_cb.get())
            if chosen:
                hint_label.configure(text=plc_catalog.describe(chosen))
            elif protocol_key == "bacnet_ip":
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

        def type_chosen(*_):
            type_overrides.clear()
            key = type_key_of.get(type_cb.get())
            if key:
                plc = plc_catalog.get(key)
                if plc.driver:
                    protocol_cb.set(PROTOCOL_LABELS[plc.driver])
                    type_overrides.update(plc.interface)
            render_fields()

        def protocol_chosen(*_):
            type_cb.set(type_texts[0])          # choosing a protocol by hand means no PLC type
            type_overrides.clear()
            render_fields()

        type_cb.bind("<<ComboboxSelected>>", type_chosen)
        protocol_cb.bind("<<ComboboxSelected>>", protocol_chosen)
        render_fields()

        def save():
            name = name_entry.get().strip()
            if not name:
                messagebox.showerror("Invalid", "Connection name required")
                return
            chosen_type = type_key_of.get(type_cb.get())
            if chosen_type and not plc_catalog.get(chosen_type).driver:
                messagebox.showinfo("Not supported yet", f"ProtoProbe cannot connect to {plc_catalog.get(chosen_type).label} yet.\n"
                                    "Pick another PLC type, or choose the protocol by hand below.")
                return
            protocol_key = label_to_key[protocol_cb.get()]
            interface = {k: coerce(e.get().strip()) for k, e in field_entries.items()}
            try:
                poll = float(poll_var.get())
                if not workspace.POLL_MIN_S <= poll <= workspace.POLL_MAX_S:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Invalid", f"Poll interval must be a number of seconds between {workspace.POLL_MIN_S} and {workspace.POLL_MAX_S:g}")
                return

            if existing:
                existing.protocol = protocol_key
                existing.interface = interface
                existing.plc_type = chosen_type
                existing.poll_interval_s = poll
                existing.enabled = enabled_var.get()
            else:
                if name in self.app.connections:
                    messagebox.showerror("Duplicate", f"Connection '{name}' already exists")
                    return
                self.app.connections[name] = Connection(name, protocol_key, interface, chosen_type, enabled_var.get(), poll)

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

    def _s7_guided_dialog(self, parent, conn, on_saved, quick=False, existing=None):
        """Add one Siemens tag the way the gateway's own dialog does it: pick the register type, enter the address, see the address spelled out,
        choose the data type, press Test read to see the value right away."""
        win = tk.Toplevel(parent)
        win.title(f"Read a value -- {conn.name}" if quick else "Edit Siemens tag" if existing else "Add Siemens tag")
        win.geometry("1140x700" if quick else "640x640")
        if not quick:
            win.grab_set()
        box = ttk.Frame(win)                         # the form; in quick mode the watch list sits to its right
        if quick:
            box.pack(side="left", fill="y")
            side = ttk.Frame(win)
            side.pack(side="left", fill="both", expand=True, padx=(0, 10), pady=10)
        else:
            box.pack(fill="both", expand=True)

        ttk.Label(box, text="Tag name (used if you add it as a tag)" if quick else "Tag name").pack(anchor="w", padx=10, pady=(10, 0))
        name_var = tk.StringVar(value=f"Tag{len(conn.tags) + 1}")
        ttk.Entry(box, textvariable=name_var, width=40).pack(anchor="w", padx=10)

        ttk.Label(box, text="1. Choose the register type", font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=10, pady=(12, 2))
        grid = ttk.Frame(box)
        grid.pack(anchor="w", padx=10)
        choice = tk.StringVar(value="M|bit")
        titles = {"bit": "Bit (1 bit)", "byte": "Byte (8 bit)", "word": "Word (16 bit)", "dword": "DWord (32 bit)"}
        for c, width in enumerate(s7_address.WIDTHS, start=1):
            ttk.Label(grid, text=titles[width]).grid(row=0, column=c, padx=4, pady=2)
        labels = {"I": ("I", "IB", "IW", "ID"), "Q": ("Q", "QB", "QW", "QD"), "M": ("M", "MB", "MW", "MD"),
                  "DB": ("DBn.DBX", "DBn.DBB", "DBn.DBW", "DBn.DBD")}
        rows = {"I": "I (Input)", "Q": "Q (Output)", "M": "M (Bit memory)", "DB": "DB (Data block)"}
        for r, area in enumerate(s7_address.AREAS, start=1):
            ttk.Label(grid, text=rows[area], width=16).grid(row=r, column=0, sticky="w")
            for c, width in enumerate(s7_address.WIDTHS, start=1):
                ttk.Radiobutton(grid, text=labels[area][c - 1], style="Toolbutton", width=9, variable=choice, value=f"{area}|{width}",
                                state="normal" if s7_address.readable(width) else "disabled").grid(row=r, column=c, padx=3, pady=2)
        ttk.Label(box, text="Byte values (IB, MB, DBB) are greyed out: ProtoProbe cannot read single bytes yet.", foreground="gray").pack(
            anchor="w", padx=10)

        ttk.Label(box, text="2. Enter the address", font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=10, pady=(12, 2))
        addr = ttk.Frame(box)
        addr.pack(anchor="w", padx=10)
        db_var, byte_var, bit_var = tk.StringVar(value="1"), tk.StringVar(value="0"), tk.StringVar(value="0")
        ttk.Label(addr, text="DB number").grid(row=0, column=0, padx=4)
        db_entry = ttk.Entry(addr, textvariable=db_var, width=8)
        db_entry.grid(row=1, column=0, padx=4)
        ttk.Label(addr, text="Byte offset").grid(row=0, column=1, padx=4)
        ttk.Entry(addr, textvariable=byte_var, width=8).grid(row=1, column=1, padx=4)
        ttk.Label(addr, text="Bit (0-7)").grid(row=0, column=2, padx=4)
        bit_entry = ttk.Entry(addr, textvariable=bit_var, width=8)
        bit_entry.grid(row=1, column=2, padx=4)

        preview = ttk.Label(box, text="", font=("Consolas", 12, "bold"))
        preview.pack(anchor="w", padx=10, pady=(10, 0))

        ttk.Label(box, text="3. Data type and scaling", font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=10, pady=(12, 2))
        dt = ttk.Frame(box)
        dt.pack(anchor="w", padx=10)
        type_var, scale_var, offset_var, unit_var = tk.StringVar(), tk.StringVar(value="1"), tk.StringVar(value="0"), tk.StringVar()
        ttk.Label(dt, text="Data type").grid(row=0, column=0, sticky="w", padx=4)
        type_cb = ttk.Combobox(dt, textvariable=type_var, state="readonly", width=18)
        type_cb.grid(row=0, column=1, padx=4)
        for i, (text, var) in enumerate((("Scale", scale_var), ("Offset", offset_var), ("Unit", unit_var)), start=1):
            ttk.Label(dt, text=text).grid(row=i, column=0, sticky="w", padx=4, pady=2)
            ttk.Entry(dt, textvariable=var, width=20).grid(row=i, column=1, padx=4, pady=2)

        value_label = ttk.Label(box, text="", font=("Segoe UI", 22, "bold"))
        if quick:
            value_label.pack(anchor="w", padx=10, pady=(10, 0))
        result = ttk.Label(box, text="", wraplength=600, justify="left")
        result.pack(anchor="w", padx=10, pady=(4, 0))

        def current():
            area, width = choice.get().split("|")
            return area, width

        def build():
            area, width = current()
            return s7_address.build_tag(name_var.get(), area, width, db_var.get() or 0, byte_var.get() or 0, bit_var.get() or 0,
                                        type_var.get() or None, float(scale_var.get() or 1), float(offset_var.get() or 0), unit_var.get().strip())

        def update(*_):
            area, width = current()
            options = s7_address.data_types_for(width)
            type_cb.configure(values=options)
            if type_var.get() not in options:
                type_var.set(options[0] if options else "")
            db_entry.configure(state="normal" if area == "DB" else "disabled")
            bit_entry.configure(state="normal" if width == "bit" else "disabled")
            try:
                text = s7_address.address_text(area, width, db_var.get() or 0, byte_var.get() or 0, bit_var.get() or 0)
                preview.configure(text=f"{text}   ({s7_address.friendly_type(type_var.get())})", foreground="#2e7d32")
            except ValueError as exc:
                preview.configure(text=str(exc), foreground="#c62828")

        for var in (choice, db_var, byte_var, bit_var, type_var):
            var.trace_add("write", update)
        update()
        def fill_from(tag):
            """Put an existing tag's register, address, type and scaling into the form."""
            old_type = tag.get("data_type", "int16")
            old_width = "bit" if old_type == "bit" else "dword" if ("32" in old_type) else "word"
            name_var.set(tag["name"])
            db_var.set(str(tag.get("db_number") or 1))
            byte_var.set(str(tag.get("start_byte", 0)))
            bit_var.set(str(tag.get("bit_offset", 0)))
            choice.set(f"{tag.get('area', 'M')}|{old_width}")
            if old_type not in s7_address.data_types_for(old_width):
                type_cb.configure(values=s7_address.data_types_for(old_width) + [old_type])
            type_var.set(old_type)
            scale_var.set(str(tag.get("multiplier", 1.0)))
            offset_var.set(str(tag.get("offset", 0.0)))
            unit_var.set(str(tag.get("unit", "")))
            update()

        if existing:                                  # editing: start from what the tag is now
            fill_from(existing)

        test_queue = queue.Queue()
        keep_var = tk.BooleanVar(value=False)
        busy = {"reading": False}

        def test_read(*_):
            if busy["reading"] or not win.winfo_exists():
                return
            try:
                tag = build()
            except ValueError as exc:
                result.configure(text=str(exc), foreground="#c62828")
                return
            busy["reading"] = True
            result.configure(text="Reading...", foreground="gray")

            def worker():
                test_queue.put(read_tag_once(conn, tag))

            threading.Thread(target=worker, daemon=True).start()

            def check():
                if not win.winfo_exists():
                    return
                try:
                    value, error = test_queue.get_nowait()
                except queue.Empty:
                    win.after(100, check)
                    return
                busy["reading"] = False
                stamp = time.strftime("%H:%M:%S")
                if error is None:
                    value_label.configure(text=str(show_value(value)), foreground="#2e7d32")
                    result.configure(text=f"\u25cf Read OK  ({stamp})", foreground="#2e7d32")
                else:
                    hint = explain_s7_error(error)
                    value_label.configure(text="--", foreground="#c62828")
                    result.configure(text=f"\u25cf Read failed ({stamp}): {error}" + (f"\n{hint}" if hint else ""), foreground="#c62828")
                if keep_var.get():
                    win.after(1000, test_read)

            win.after(100, check)

        def keep_toggled(*_):
            if keep_var.get():
                test_read()

        def save():
            try:
                tag = build()
            except ValueError as exc:
                messagebox.showerror("Cannot add", str(exc))
                return
            if existing:
                if any(t["name"] == tag["name"] and t is not existing for t in conn.tags):
                    messagebox.showerror("Duplicate", f"Tag '{tag['name']}' already exists")
                    return
                conn.latest_values.pop(existing["name"], None)
                existing.clear()
                existing.update(tag)                  # the same dict: everything that holds this tag sees the change
                on_saved()
                win.destroy()
                return
            if any(t["name"] == tag["name"] for t in conn.tags):
                messagebox.showerror("Duplicate", f"Tag '{tag['name']}' already exists")
                return
            conn.tags.append(tag)
            on_saved()
            win.destroy()

        if quick:
            ttk.Label(side, text="Values to watch (several at once)", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(10, 4))
            cols = ("name", "address", "value", "quality")
            watch = ttk.Treeview(side, columns=cols, show="headings", height=14, selectmode="extended")
            for col, text, width in (("name", "Name", 140), ("address", "Address", 100), ("value", "Value", 190), ("quality", "Quality", 60)):
                watch.heading(col, text=text)
                watch.column(col, width=width, anchor="w" if col == "name" else "center")
            for colour, hex_colour in HEALTH_COLOURS.items():
                watch.tag_configure(colour, foreground=hex_colour)
            watch.pack(fill="both", expand=True)
            watch_status = ttk.Label(side, text="Press 'Add to list' to put the address on the left into the list.", foreground="gray",
                                     wraplength=420, justify="left")
            watch_status.pack(anchor="w", pady=(6, 0))
            watched = []                             # tag dicts, in the order of the list
            list_keep = tk.BooleanVar(value=False)
            list_busy = {"reading": False}
            list_results = queue.Queue()

            def add_to_list():
                try:
                    tag = build()
                except ValueError as exc:
                    watch_status.configure(text=str(exc), foreground="#c62828")
                    return
                area, width = current()
                address = s7_address.address_text(area, width, db_var.get() or 0, byte_var.get() or 0, bit_var.get() or 0)
                if any(t["name"] == tag["name"] for t in watched):
                    tag["name"] = f"{tag['name']}_{address}"
                if any(t["name"] == tag["name"] for t in watched):
                    watch_status.configure(text=f"'{tag['name']}' is already in the list.", foreground="#c62828")
                    return
                tag["_address"] = address
                watched.append(tag)
                watch.insert("", "end", iid=tag["name"], tags=("grey",), values=(tag["name"], address, "", "--"))
                watch_status.configure(text=f"{len(watched)} value(s) in the list.", foreground="gray")
                name_var.set(f"Tag{len(conn.tags) + len(watched) + 1}")

            def load_selected(*_):
                sel = watch.selection()
                if sel:
                    fill_from(next(t for t in watched if t["name"] == sel[0]))
                    watch_status.configure(text="Loaded into the form: change it, then press 'Update selected'.", foreground="gray")

            def update_selected():
                sel = watch.selection()
                if not sel:
                    watch_status.configure(text="Select a row in the list first (double-click loads it into the form).", foreground="#c62828")
                    return
                try:
                    tag = build()
                except ValueError as exc:
                    watch_status.configure(text=str(exc), foreground="#c62828")
                    return
                area, width = current()
                index = next(i for i, t in enumerate(watched) if t["name"] == sel[0])
                if any(t["name"] == tag["name"] for i, t in enumerate(watched) if i != index):
                    watch_status.configure(text=f"'{tag['name']}' is already in the list.", foreground="#c62828")
                    return
                tag["_address"] = s7_address.address_text(area, width, db_var.get() or 0, byte_var.get() or 0, bit_var.get() or 0)
                watched[index] = tag
                watch.delete(sel[0])
                watch.insert("", index, iid=tag["name"], tags=("grey",), values=(tag["name"], tag["_address"], "", "--"))
                watch.selection_set(tag["name"])
                watch_status.configure(text=f"Updated: {tag['name']} is now {tag['_address']}.", foreground="gray")

            watch.bind("<Double-1>", load_selected)
            watch.bind("<Return>", load_selected)          # the same from the keyboard

            def remove_from_list():
                for iid in watch.selection():
                    watched[:] = [t for t in watched if t["name"] != iid]
                    watch.delete(iid)

            def read_list(*_):
                if list_busy["reading"] or not win.winfo_exists() or not watched:
                    return
                list_busy["reading"] = True
                tags = [{k: v for k, v in t.items() if k != "_address"} for t in watched]
                threading.Thread(target=lambda: list_results.put(read_tags_once(conn, tags)), daemon=True).start()

                def check():
                    if not win.winfo_exists():
                        return
                    try:
                        results = list_results.get_nowait()
                    except queue.Empty:
                        win.after(100, check)
                        return
                    list_busy["reading"] = False
                    good = 0
                    for tag in watched:
                        if tag["name"] not in watch.get_children():
                            continue
                        value, error = results.get(tag["name"], (None, "no value"))
                        if error is None:
                            good += 1
                        watch.item(tag["name"], tags=(("green" if error is None else "red"),),
                                   values=(tag["name"], tag["_address"], show_value(value) if error is None else str(error).replace("Read operation failed: ", ""),
                                   "Good" if error is None else "Bad"))
                    watch_status.configure(text=f"\u25cf {good} of {len(watched)} read OK  ({time.strftime('%H:%M:%S')})",
                                           foreground="#2e7d32" if good == len(watched) else "#e0a100" if good else "#c62828")
                    if list_keep.get():
                        win.after(1000, read_list)

                win.after(100, check)

            def save_list():
                added = 0
                for tag in watched:
                    if any(t["name"] == tag["name"] for t in conn.tags):
                        continue
                    conn.tags.append({k: v for k, v in tag.items() if k != "_address"})
                    added += 1
                if added:
                    on_saved()
                watch_status.configure(text=f"{added} tag(s) added to the connection.", foreground="gray")

            row = ttk.Frame(side)
            row.pack(fill="x", pady=(8, 0))
            ttk.Button(row, text="Add to list", command=add_to_list).pack(side="left")
            ttk.Button(row, text="Update selected", command=update_selected).pack(side="left", padx=(6, 0))
            ttk.Button(row, text="Remove selected", command=remove_from_list).pack(side="left", padx=6)
            ttk.Button(row, text="Read list now", command=read_list).pack(side="left")
            row2 = ttk.Frame(side)
            row2.pack(fill="x", pady=(6, 0))
            ttk.Checkbutton(row2, text="Keep reading the list (every second)", variable=list_keep,
                            command=lambda: read_list() if list_keep.get() else None).pack(side="left")
            ttk.Button(row2, text="Add all as tags", command=save_list).pack(side="right")

        buttons = ttk.Frame(box)
        buttons.pack(anchor="e", padx=10, pady=14, fill="x")
        ttk.Button(buttons, text="Read now" if quick else "Test read", command=test_read).pack(side="left")
        if quick:
            ttk.Checkbutton(buttons, text="Keep reading (every second)", variable=keep_var, command=keep_toggled).pack(side="left", padx=12)
            ttk.Button(buttons, text="Close", command=win.destroy).pack(side="right", padx=4)
            ttk.Button(buttons, text="Add as tag", command=save).pack(side="right", padx=4)
        else:
            ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side="right", padx=4)
            ttk.Button(buttons, text="Save" if existing else "OK", command=save).pack(side="right", padx=4)
        return win

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
        def add_tag():
            if conn.protocol == "s7comm":
                def saved():
                    refresh_tags()
                    self.refresh()
                    self.app.refresh_dashboard()
                self._s7_guided_dialog(win, conn, saved)
            else:
                tag_dialog(None)

        ttk.Button(btn_row, text="Add Tag", command=add_tag).pack(side="left", padx=2)
        if conn.protocol == "s7comm":
            ttk.Button(btn_row, text="Add Tag (fields)", command=lambda: tag_dialog(None)).pack(side="left", padx=2)

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

    def open_quick_read(self, parent=None):
        """Read ONE value of the selected connection, the way the gateway's own interface lets you: pick the register type, enter the address,
        see the value (and keep it updating) without making a tag first."""
        sel = self.conn_tree.selection()
        if not sel:
            messagebox.showinfo("Select a connection", "Click a connection's row above first")
            return
        conn = self.app.connections[sel[0]]
        if conn.protocol == "s7comm":
            def saved():
                self.refresh()
                self.app.refresh_dashboard()
                messagebox.showinfo("Tag added", "Added to this connection's tags. It will show on Live values once polling is started.")
            return self._s7_guided_dialog(parent or self, conn, saved, quick=True)
        return self._generic_quick_read(conn, parent)

    def _generic_quick_read(self, conn, parent=None):
        """The same one-value reader for the other protocols: the tag fields of the protocol, then Read now."""
        win = tk.Toplevel(parent or self)
        win.title(f"Read a value -- {conn.name}")
        win.geometry("520x420")
        entries = {}
        for label, default in TAG_FIELDS.get(conn.protocol, []):
            row = ttk.Frame(win)
            row.pack(fill="x", padx=10, pady=3)
            ttk.Label(row, text=label, width=16).pack(side="left")
            if label in FIELD_OPTIONS:
                e = ttk.Combobox(row, values=FIELD_OPTIONS[label]["options"], width=20, state="readonly")
                e.set(default)
            else:
                e = ttk.Entry(row, width=24)
                e.insert(0, str(default))
            e.pack(side="left")
            entries[label] = e
        value_label = ttk.Label(win, text="", font=("Segoe UI", 22, "bold"))
        value_label.pack(anchor="w", padx=10, pady=(12, 0))
        result = ttk.Label(win, text="", wraplength=490, justify="left")
        result.pack(anchor="w", padx=10)
        keep_var = tk.BooleanVar(value=False)
        results = queue.Queue()
        busy = {"reading": False}

        def read_now(*_):
            if busy["reading"] or not win.winfo_exists():
                return
            tag = {"name": "quick"}
            for k, e in entries.items():
                raw = e.get().strip()
                tag[k] = raw if k in FIELD_OPTIONS else coerce(raw)
            busy["reading"] = True
            result.configure(text="Reading...", foreground="gray")
            threading.Thread(target=lambda: results.put(read_tag_once(conn, tag)), daemon=True).start()

            def check():
                if not win.winfo_exists():
                    return
                try:
                    value, error = results.get_nowait()
                except queue.Empty:
                    win.after(100, check)
                    return
                busy["reading"] = False
                stamp = time.strftime("%H:%M:%S")
                if error is None:
                    value_label.configure(text=str(show_value(value)), foreground="#2e7d32")
                    result.configure(text=f"\u25cf Read OK  ({stamp})", foreground="#2e7d32")
                else:
                    value_label.configure(text="--", foreground="#c62828")
                    result.configure(text=f"\u25cf Read failed ({stamp}): {error}", foreground="#c62828")
                if keep_var.get():
                    win.after(1000, read_now)

            win.after(100, check)

        row = ttk.Frame(win)
        row.pack(fill="x", padx=10, pady=14)
        ttk.Button(row, text="Read now", command=read_now).pack(side="left")
        ttk.Checkbutton(row, text="Keep reading (every second)", variable=keep_var,
                        command=lambda: read_now() if keep_var.get() else None).pack(side="left", padx=12)
        ttk.Button(row, text="Close", command=win.destroy).pack(side="right")
        return win

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
        win.geometry("640x850")
        win.grab_set()

        ttk.Label(
            win,
            text="Both options below run in the background and land on the 'Scan results' "
                 "tab -- you can close this window right after clicking Start and check "
                 "results whenever you like. Assumes int16 for the scan itself; check and "
                 "fix the data type after adding a tag if it's actually 32-bit or float.",
            wraplength=600, foreground="gray", justify="left",
        ).pack(anchor="w", padx=10, pady=(10, 10))

        ttk.Button(win, text="Read one value instead...", command=lambda: self.open_quick_read(win)).pack(anchor="w", padx=10, pady=(0, 4))

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

        presets = plc_catalog.scan_presets(getattr(conn, "plc_type", None) or plc_catalog.default_type(conn.protocol))
        if presets:
            preset_row = ttk.Frame(win)
            preset_row.pack(fill="x", padx=10, pady=(4, 0))
            ttk.Label(preset_row, text="Preset").pack(side="left")
            preset_cb = ttk.Combobox(preset_row, values=["(none)"] + [p.label for p in presets], state="readonly", width=56)
            preset_cb.set("(none)")
            preset_cb.pack(side="left", padx=6)
            preset_note = ttk.Label(win, text="", foreground="gray", wraplength=600, justify="left")
            preset_note.pack(anchor="w", padx=10)

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
                single_frame, text="Note: scanning I or Q shows which bits are on and which changed; use 'Add selected' "
                          "in the results to make bit tags from them. Markers (M) have no wiring to "
                          "check against, so scanning them is the way to find what moves. "
                          "db_number is ignored unless area = DB.",
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
            # On its own full-width line: the messages (an error and its hint) are long and were cut off at the window edge when they
            # shared the row with the button.
            discover_status = ttk.Label(single_frame, text="", foreground="gray", wraplength=590, justify="left")
            discover_status.pack(anchor="w", padx=8, pady=(0, 2))

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
                    run_db_discovery(conn, max_db, discover_out_queue)

                threading.Thread(target=worker, daemon=True).start()
                pump_discover()

            def pump_discover():
                try:
                    while True:
                        kind, data = discover_out_queue.get_nowait()
                        if kind == "status":
                            discover_status.configure(text=data)
                        elif kind in ("db_discovery_error", "db_discovery_done"):
                            if kind == "db_discovery_done":
                                for db in data[0]:
                                    discovered_list.insert("end", f"DB{db}")
                            text, colour = discovery_status(kind, data)
                            discover_btn.configure(state="normal")
                            discover_status.configure(text=text, foreground=colour)
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
                full_frame, text="DB auto-discovers which DB numbers exist first (a DB with optimized "
                          "block access cannot be read and is reported). I and Q rows show bits.",
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

        if presets:
            def apply_preset(*_):
                chosen = next((p for p in presets if p.label == preset_cb.get()), None)
                if chosen is None:
                    preset_note.configure(text="")
                    return
                for key, value in chosen.single.items():
                    entry = range_entries.get(key)
                    if entry is None:
                        continue
                    if isinstance(entry, ttk.Combobox):
                        entry.set(str(value))
                    else:
                        entry.delete(0, "end")
                        entry.insert(0, str(value))
                if chosen.full:
                    for key, var in checkbox_vars.items():
                        var.set(key in chosen.full.get("include", ()))
                    if "max" in chosen.full:
                        max_addr_entry.delete(0, "end")
                        max_addr_entry.insert(0, str(chosen.full["max"]))
                preset_note.configure(text=chosen.note + ("  (fills the whole-PLC section below; press Start full PLC scan)" if chosen.full else
                                                          "  (fills the single-area fields; press Start scan)"))

            preset_cb.bind("<<ComboboxSelected>>", apply_preset)

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
            if not conn.tags or not getattr(conn, "enabled", True):
                continue
            thread = PollerThread(conn, getattr(conn, "poll_interval_s", 2.0), self.app.out_queue)
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

        # One line per connection: a coloured dot and what the poller reports (green = connected and every tag reads).
        self.health_frame = ttk.Frame(self)
        self.health_frame.pack(fill="x", padx=20, pady=(0, 6))

        columns = ("connection", "tag", "address", "data_type", "value", "quality", "updated", "error")
        self.live_tree = ttk.Treeview(self, columns=columns, show="headings", height=20)
        headings = [
            ("connection", "Connection", 110), ("tag", "Tag", 140), ("address", "Address", 110),
            ("data_type", "Data type", 80), ("value", "Live value", 100), ("quality", "Quality", 70),
            ("updated", "Last updated", 90), ("error", "Error", 300),
        ]
        for col, label, w in headings:
            self.live_tree.heading(col, text=label)
            self.live_tree.column(col, width=w, anchor="w" if col == "error" else "center")
        for colour, hex_colour in HEALTH_COLOURS.items():
            self.live_tree.tag_configure(colour, foreground=hex_colour)
        self.live_tree.pack(fill="both", expand=True, padx=20, pady=8)
        self.live_tree.bind("<Double-1>", lambda e: self.edit_selected_tag())
        ttk.Label(self, text="Double-click a row to change that tag's register, address, data type, scale or unit (reading only: nothing is written to the PLC).",
                  foreground="gray").pack(anchor="w", padx=20, pady=(0, 8))

    def edit_selected_tag(self):
        selected = self.live_tree.selection()
        if not selected or "::" not in selected[0]:
            return None
        conn_name, tag_name = selected[0].split("::", 1)
        conn = self.app.connections.get(conn_name)
        tag = next((t for t in conn.tags if t["name"] == tag_name), None) if conn else None
        if not tag:
            return None
        if conn.protocol == "s7comm":
            def saved():
                self.app.restart_poller(conn)
                self.refresh()
                if "connections" in self.app.screens:
                    self.app.screens["connections"].refresh()
            return self.app.screens["connections"]._s7_guided_dialog(self, conn, saved, existing=tag)
        return self.edit_tag_dialog(conn, tag)

    def edit_tag_dialog(self, conn, tag):
        """Change how a tag is READ (data type, scale, offset, unit) while it is being polled. Nothing is ever written to the PLC."""
        win = tk.Toplevel(self)
        win.title(f"Edit tag -- {tag['name']}")
        win.geometry("420x300")
        ttk.Label(win, text=f"{conn.name}:  {tag['name']}   ({format_address(conn.protocol, tag)})", font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=10, pady=(10, 8))
        options = ["bit"] if tag.get("data_type") == "bit" else [o for o in FIELD_OPTIONS["data_type"]["options"] if o != "bit"]
        vars_ = {"data_type": tk.StringVar(value=str(tag.get("data_type", options[0]))),
                 "multiplier": tk.StringVar(value=str(tag.get("multiplier", 1.0))),
                 "offset": tk.StringVar(value=str(tag.get("offset", 0.0))),
                 "unit": tk.StringVar(value=str(tag.get("unit", "")))}
        for row, (key, label) in enumerate((("data_type", "Data type"), ("multiplier", "Scale (x)"), ("offset", "Offset (+)"), ("unit", "Unit"))):
            frame = ttk.Frame(win)
            frame.pack(fill="x", padx=10, pady=3)
            ttk.Label(frame, text=label, width=12).pack(side="left")
            if key == "data_type":
                ttk.Combobox(frame, textvariable=vars_[key], values=options, state="readonly", width=18).pack(side="left")
            else:
                ttk.Entry(frame, textvariable=vars_[key], width=20).pack(side="left")
        ttk.Label(win, text="The value shown is: raw value x scale + offset. A 32-bit type reads 4 bytes from the same address.",
                  foreground="gray", wraplength=390, justify="left").pack(anchor="w", padx=10, pady=(8, 0))

        def save():
            try:
                tag["multiplier"] = float(vars_["multiplier"].get() or 1)
                tag["offset"] = float(vars_["offset"].get() or 0)
            except ValueError:
                messagebox.showerror("Not a number", "Scale and offset must be numbers")
                return
            tag["data_type"] = vars_["data_type"].get()
            if vars_["unit"].get().strip():
                tag["unit"] = vars_["unit"].get().strip()
            else:
                tag.pop("unit", None)
            conn.latest_values.pop(tag["name"], None)
            self.app.restart_poller(conn)
            self.refresh()
            win.destroy()

        row = ttk.Frame(win)
        row.pack(fill="x", padx=10, pady=14)
        ttk.Button(row, text="Save", command=save).pack(side="right", padx=4)
        ttk.Button(row, text="Cancel", command=win.destroy).pack(side="right", padx=4)
        return win

    def refresh(self):
        for child in self.health_frame.winfo_children():
            child.destroy()
        self.live_tree.delete(*self.live_tree.get_children())
        for conn in self.app.connections.values():
            colour, text = connection_health(conn.status, getattr(conn, "health", None), len(conn.tags))
            health = getattr(conn, "health", None) or {}
            extra = f"   polls {health['polls']}, failed reads {health['errors']}" if health.get("polls") else ""
            ttk.Label(self.health_frame, text=f"\u25cf {conn.name}: {text}{extra}",
                      foreground=HEALTH_COLOURS[colour]).pack(anchor="w")
            bad = health.get("bad", {})
            for tag_fields in conn.tags:                     # every tag, also the ones that have no value yet or cannot be read
                tag_name = tag_fields["name"]
                has_value = tag_name in conn.latest_values and tag_name not in bad
                quality = "Good" if has_value else ("Bad" if (tag_name in bad or health.get("polls")) else "--")
                self.live_tree.insert("", "end", iid=f"{conn.name}::{tag_name}",
                                      tags=(("green" if has_value else "red" if quality == "Bad" else "grey"),), values=(
                    conn.name, tag_name, format_address(conn.protocol, tag_fields), tag_fields.get("data_type", ""),
                    show_value(conn.latest_values.get(tag_name, "")) if has_value else "", quality,
                    conn.last_updated.get(tag_name, ""), bad.get(tag_name, "")))


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
        self.progress_bar = ttk.Progressbar(header, length=150, maximum=1.0, mode="determinate")
        self.progress_bar.pack(side="right", padx=(0, 10))

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

        use_frame = ttk.Frame(self)
        use_frame.pack(fill="x", padx=20, pady=(0, 4))
        ttk.Label(use_frame, text="Show:").pack(side="left", padx=(0, 8))
        self.hint_filter = tk.StringVar(value="All results")
        hint_box = ttk.Combobox(use_frame, textvariable=self.hint_filter, state="readonly", width=34,
                                values=["All results", "OEE candidates (run state, counts, settings)", "Predictive candidates (trends, usage counts)"])
        hint_box.pack(side="left")
        hint_box.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        ttk.Label(use_frame, text="Suggestions are guesses from how a value moved during the scan: confirm them by running the machine.",
                  foreground="gray").pack(side="left", padx=12)

        columns = ("connection", "job", "type", "address", "changed", "raw", "latest", "float_guess", "hint", "match")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=18)
        headings = [
            ("connection", "Connection", 110), ("job", "Scan job", 120), ("type", "Type", 50),
            ("address", "Address", 100), ("changed", "Changed?", 70),
            ("raw", "Raw value", 120), ("latest", "As int16", 80),
            ("float_guess", "Possible float32 / bits on (* = changed)", 230),
            ("hint", "Suggested use (a guess from this scan: confirm it on the machine)", 420), ("match", "Search match", 160),
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
        self.progress_bar["value"] = self.app.scan_progress if self.app.scan_running else 0.0

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
            hint = hint_for_result(r)
            wanted = {"O": "oee", "P": "predictive"}.get(self.hint_filter.get()[:1], "all")
            if not signal_hints.matches(hint, wanted):
                continue

            iid = f"{r['conn_name']}::{r['job_label']}::{r['name']}::{i}"
            float_guess = r.get("float32_guess")
            float_text = f"{float_guess} (with {r.get('float32_pair_with')})" if float_guess is not None else "--"
            if r.get("bit_area"):
                float_text = "bits " + bits_summary(r["tag"]["area"], r["bits_on"], r["bits_changed"])
            elif r["tag"].get("area") == "M" and (r.get("bits_on") or r.get("bits_changed")):
                float_text = f"{float_text}  |  bits " + bits_summary("M", r["bits_on"], r["bits_changed"])
            raw = r.get("raw_value")
            raw_text = f"{raw} (0x{raw:04X})" if raw is not None else "--"
            self.tree.insert("", "end", iid=iid, values=(
                r["conn_name"], r["job_label"], type_label, r["name"],
                "Yes" if r["changed"] else "No", raw_text, r["latest"], float_text,
                hint_text(hint), match_text or "",
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
            if r.get("bit_area"):
                # I/Q rows are bits: add one bit tag per changed bit (or per bit that is on).
                pick = r["bits_changed"] or r["bits_on"]
                new_tags = []
                for byte, bit in pick:
                    nm = f"{r['tag']['area']}{byte}.{bit}"
                    new_tags.append({"name": nm, "area": r["tag"]["area"], "db_number": r["tag"].get("db_number", 1),
                                     "start_byte": byte, "bit_offset": bit, "data_type": "bit",
                                     "multiplier": 1.0, "offset": 0.0, "mqtt_key": nm})
                if not new_tags:
                    skipped += 1
                for tag in new_tags:
                    if any(t["name"] == tag["name"] for t in conn.tags):
                        skipped += 1
                    else:
                        conn.tags.append(tag)
                        added += 1
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
                     f"Check data types in Manage Tags -- the scan assumes int16 for words (I/Q rows are added as single bit tags)."
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
        self._ip_done = 0
        self._ip_found = 0
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
            return ip, ping_once(ip)

        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
            futures = [pool.submit(ping_host, n) for n in range(start, end + 1)]
            for future in concurrent.futures.as_completed(futures):
                self._ip_queue.put(future.result())
        self._ip_queue.put(None)  # sentinel: scan complete

    def _pump_ip_queue(self, total):
        try:
            while True:
                item = self._ip_queue.get_nowait()
                if item is None:
                    self._ip_scanning = False
                    self.ip_scan_btn.configure(state="normal")
                    self.ip_progress_label.configure(text=f"Done -- {total} addresses checked, {self._ip_found} reachable")
                    return
                ip, reachable = item
                self._ip_done += 1
                if reachable:
                    self._ip_found += 1
                    self.ip_tree.insert("", "end", values=(ip, "Reachable"))
        except queue.Empty:
            pass
        if self._ip_scanning:
            self.ip_progress_label.configure(text=f"Scanning {self._ip_done}/{total}... {self._ip_found} reachable so far")
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
        self.scan_progress = 0.0            # 0..1 of the job being read (all its snapshots)
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
                elif kind == "progress":
                    self.scan_progress = data
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
                            "bit_area": r.get("bit_area", False),
                            "values": r.get("values", []),
                            "bits_series": r.get("bits_series", {}),
                            "bits_on": r.get("bits_on", []),
                            "bits_changed": r.get("bits_changed", []),
                        })
                    if "scan_results" in self.screens:
                        self.screens["scan_results"].refresh()
                elif kind == "job_done":
                    self.scan_running = False
                    self.scan_progress = 0.0
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
        file_menu.add_command(label="Open workspace...", command=self.open_workspace_file)
        file_menu.add_command(label="Save workspace as...", command=self.save_workspace_as)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
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
        self.screens["commission"] = CommissioningScreen(content, self, DRIVER_FACTORY, build_range_tags)
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

    # ---- the workspace: connections and tags remembered between sessions (opt-in from main(), so tests never touch a real profile) ----
    workspace_file = None
    _workspace_blocked = False
    _workspace_last = None

    def _workspace_dict(self):
        return workspace.snapshot(self.connections.values(), APP_VERSION)

    @staticmethod
    def _workspace_key(data):
        return json.dumps({k: v for k, v in data.items() if k != "saved_at"}, sort_keys=True)

    def _apply_workspace(self, data):
        """Replace the connections with those of a loaded workspace; returns the warnings (what was left out and why)."""
        found, warnings = workspace.restore(data, set(PROTOCOL_LABELS))
        for old in self.connections.values():
            if old.thread:
                old.thread.stop()
        self.connections.clear()
        for c in found:
            conn = Connection(c["name"], c["protocol"], c["interface"], c["plc_type"], c["enabled"], c["poll_interval_s"])
            conn.tags = c["tags"]
            self.connections[conn.name] = conn
        self.screens["connections"].refresh()
        self.refresh_dashboard()
        return warnings

    def enable_workspace(self, path=None):
        """Remember connections and tags between sessions: load the saved file now, save changes every few seconds and when the window closes."""
        self.workspace_file = path or workspace.default_path()
        data, message = workspace.load(self.workspace_file)
        notes = [message] if message else []
        if data:
            try:
                notes += self._apply_workspace(data)
            except ValueError as exc:
                self._workspace_blocked = True          # never overwrite a file this version does not understand
                notes.append(f"{exc}\n\nNothing will be saved over it in this session.")
        self._workspace_last = self._workspace_key(self._workspace_dict())
        if notes:
            messagebox.showwarning("Workspace", "\n\n".join(notes))
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(5000, self._autosave_tick)

    def _save_workspace_now(self):
        if not self.workspace_file or self._workspace_blocked:
            return False
        data = self._workspace_dict()
        key = self._workspace_key(data)
        if key == self._workspace_last:
            return False
        try:
            workspace.save_atomic(self.workspace_file, data)
        except OSError as exc:
            if not getattr(self, "_workspace_error_shown", False):
                self._workspace_error_shown = True
                messagebox.showwarning("Workspace not saved", f"Could not save {self.workspace_file}:\n{exc}")
            return False
        self._workspace_last = key
        return True

    def _autosave_tick(self):
        self._save_workspace_now()
        self.after(5000, self._autosave_tick)

    def _on_close(self):
        self._save_workspace_now()
        self.destroy()

    def open_workspace_file(self):
        path = filedialog.askopenfilename(filetypes=[("ProtoProbe workspace", "*.json"), ("All files", "*.*")])
        if not path:
            return
        data, message = workspace.load(path)
        if data is None:
            messagebox.showerror("Open workspace", message or "That file could not be read.")
            return
        if self.connections and not messagebox.askyesno("Open workspace", "Replace the current connections and tags with this file?"):
            return
        try:
            warnings = self._apply_workspace(data)
        except ValueError as exc:
            messagebox.showerror("Open workspace", str(exc))
            return
        if warnings:
            messagebox.showwarning("Open workspace", "\n".join(warnings))

    def save_workspace_as(self):
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("ProtoProbe workspace", "*.json")], initialfile="workspace.json")
        if path:
            workspace.save_atomic(path, self._workspace_dict())
            messagebox.showinfo("Saved", f"Workspace saved to:\n{path}")

    def restart_poller(self, conn):
        """A running poller copied the tag list when it started: after a tag changed, start a new poller for that connection so the change is read."""
        old = conn.thread
        if old is None or not old.is_alive():
            return False
        old.stop()
        thread = PollerThread(conn, old.interval_s, self.out_queue)
        conn.thread = thread
        thread.start()
        return True

    def _poll_queue(self):
        try:
            while True:
                kind, data = self.out_queue.get_nowait()
                if kind == "conn_status":
                    name, status = data
                    if name in self.connections:
                        self.connections[name].status = status
                        self.refresh_dashboard()
                elif kind == "health":
                    name, report = data
                    if name in self.connections:
                        self.connections[name].health = report
                        if "connections" in self.screens:
                            self.screens["connections"].refresh()
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
    app.enable_workspace()
    app.mainloop()

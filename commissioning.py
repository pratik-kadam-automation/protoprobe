"""
commissioning.py -- the commissioning wizard's engine (no Tk, no network: pure logic).

The goal: on site, find each machine's PLC tags AND prove them against the HMI before the gateway goes
live, so nobody has to rediscover "that tag was wrong" on a deployed gateway weeks later.

How it works (the screen in commissioning_screen.py just drives this):

  1. QUESTIONNAIRE   a few answers (PLC make, machine type, IPs, counters that reset ...) decide the
                     protocol, a sensible scan plan, which byte orders to try first, and which signals
                     to ask the engineer to read off the HMI.
  2. MOMENTS         at a "moment" the engineer types what the HMI shows right now for each signal, and
                     the PLC memory is read once. Moments 1-2 are used to FIND each signal's address
                     ("find" readings); moments 3-4 are used to PROVE it ("verify" readings).
  3. FIND            for every signal, every address is tried against every interpretation (int16,
                     uint16, int32, uint32, float32 in all four byte orders, scales 1 / 0.1 / 0.01 ...).
                     A candidate must fit ALL the find readings, not just one, which removes almost every
                     coincidence a single "search for a known value" would produce.
  4. VERIFY          fresh readings taken AFTER the address was chosen. Readings used to find an address
                     cannot also prove it, so only verify readings can make a tag "confirmed", and only
                     when there are two, at least 30 s apart, with a different HMI value each time
                     (a setpoint or text value may stay the same). The dashboard applies the SAME rule
                     on import, whatever this tool claims.
  5. PACK            a JSON "commissioning pack" (schema commissioning-pack/1) with every tag, its
                     address, type, scale, role, unit and the evidence. The dashboard imports it, fills
                     the machine profile and Node-RED CONFIG, and keeps checking live data against it.

Everything here is a heuristic helper, not an oracle: a candidate is a suggestion until the verify
readings agree. Hardware status of the drivers is in the README (only MC/SLMP FX5U is proven).
"""

import calendar
import csv
import io
import json
import math
import re
import struct
import time
import zipfile
import xml.etree.ElementTree as ET

SCHEMA = "commissioning-pack/1"
TOOL = "ProtoProbe 0.3.0"
MIN_GAP_S = 30            # verify readings closer together than this do not count as separate moments
SCALES = (1, 0.1, 0.01, 0.001, 10, 100)
BYTE_ORDERS_32 = ("ABCD", "CDAB", "BADC", "DCBA")
MAX_CANDIDATES = 12

PROTOCOLS = ("mc", "s7", "modbus_tcp", "modbus_rtu", "opcua", "bacnet")
DATATYPES = ("bool", "int16", "uint16", "int32", "uint32", "float32", "float64", "string")
ROLES = ("state", "counter", "setpoint", "measurement", "alarm", "text", "ignore")
TAG_TYPES = ("boolean", "delta", "instantaneous", "cumulative", "string")


# --------------------------------------------------------------------------- questionnaire

class Question:
    def __init__(self, key, text, kind="text", choices=None, default="", required=False, when=None, help=""):
        self.key, self.text, self.kind = key, text, kind
        self.choices, self.default, self.required = choices or [], default, required
        self.when, self.help = when, help          # when: callable(answers) -> bool

    def visible(self, answers):
        return True if self.when is None else bool(self.when(answers))


# PLC make -> (protocol, default port, byte orders to try first)
MAKES = {
    "mitsubishi": ("mc", 5006, ("CDAB", "ABCD", "BADC", "DCBA")),
    "siemens":    ("s7", 102, ("ABCD", "CDAB", "BADC", "DCBA")),
    "modbus":     ("modbus_tcp", 502, ("ABCD", "CDAB", "BADC", "DCBA")),      # delta, drives, meters, others: unknown, try all
    "modbus_rtu": ("modbus_rtu", 0, ("ABCD", "CDAB", "BADC", "DCBA")),
    "opcua":      ("opcua", 4840, ("ABCD",)),
}

# Mirrors the dashboard's machine-type catalogue (machineProfiles.js SIGNAL_CATALOG): the signals a machine of
# this type usually has, so the wizard knows what to ask the engineer to read off the HMI.
#   kind: number | counter | bool      name: suggested tag name (editable)      range: plausible min/max
def S(key, label, name, kind, role, ttype, unit="", lo=None, hi=None, deadband_pct=None, rollover=None, drift=None):
    """drift = how far the value may move between the HMI refresh and the PLC read, as a fraction of the value.
    Counters do not drift (exact); fast process values (speed, flow) may; slow ones (temperature, level) hardly do."""
    if drift is None:
        drift = 0.0 if kind in ("counter", "bool") else 0.005
    return {"key": key, "label": label, "name": name, "kind": kind, "role": role, "type": ttype,
            "unit": unit, "min": lo, "max": hi, "deadband_pct": deadband_pct, "rollover": rollover, "drift": drift}


RUN = S("run", "Machine running (1/0)", "Machine_Run", "bool", "state", "boolean")
ALARM = S("alarm", "Any alarm active (1/0)", "Alarm_Active", "bool", "alarm", "boolean")
LENGTH = S("length", "Length / output counter", "Length_Count", "counter", "counter", "cumulative", "m")

MACHINES = {
    "ccm": {"label": "Continuous casting machine (CCM)", "dashboard_type": "Continuous casting machine (CCM)", "batch": True, "signals": [
        RUN, S("speed", "Casting speed", "Casting_Speed", "number", "measurement", "instantaneous", "m/min", 0, 6, 2),
        S("mould", "Mould level", "Mould_Level", "number", "measurement", "instantaneous", "mm", -50, 50, 2, drift=0.002),
        S("mould_water", "Mould cooling water flow", "Mould_Water_Flow", "number", "measurement", "instantaneous", "l/min", 0, 5000, 2),
        S("tundish", "Tundish temperature", "Tundish_Temp", "number", "measurement", "instantaneous", "C", 0, 1800, 1, drift=0.001),
        S("withdraw", "Withdrawal drive current", "Withdrawal_Current", "number", "measurement", "instantaneous", "A", 0, 1000, 2),
        S("billets", "Billet / cut counter", "Billet_Count", "counter", "counter", "delta", "pcs", rollover=65536), ALARM]},
    "singeing": {"label": "Singeing machine", "dashboard_type": "Singeing machine", "batch": True, "signals": [
        RUN, S("flame", "Burner / flame on (1/0)", "Flame_On", "bool", "state", "boolean"),
        S("gas", "Gas pressure", "Gas_Pressure", "number", "measurement", "instantaneous", "bar", 0, 10, 2),
        S("speed", "Fabric speed", "Fabric_Speed", "number", "measurement", "instantaneous", "m/min", 0, 200, 2), LENGTH, ALARM]},
    "cbr": {"label": "CBR (continuous bleaching range)", "dashboard_type": "CBR (continuous bleaching range)", "batch": False, "signals": [
        RUN, S("bath_temp", "Bath temperature", "Bath_Temp", "number", "measurement", "instantaneous", "C", 0, 150, 1, drift=0.001),
        S("speed", "Fabric speed", "Fabric_Speed", "number", "measurement", "instantaneous", "m/min", 0, 200, 2),
        S("ph", "pH", "Bath_pH", "number", "measurement", "instantaneous", "pH", 0, 14, 2), LENGTH, ALARM]},
    "stenter": {"label": "Stenter", "dashboard_type": "Stenter", "batch": True, "signals": [
        RUN, S("speed", "Chain speed", "Chain_Speed", "number", "measurement", "instantaneous", "m/min", 0, 100, 2),
        S("zone1", "Zone 1 temperature", "Zone1_Temp", "number", "measurement", "instantaneous", "C", 0, 260, 1, drift=0.001),
        S("fan", "Exhaust fan speed", "Fan_Speed", "number", "measurement", "instantaneous", "rpm", 0, 3000, 2),
        S("width", "Width setpoint", "Width_Set", "number", "setpoint", "instantaneous", "mm", 0, 4000), LENGTH, ALARM]},
    "furnace": {"label": "Scrap melting furnace (MS)", "dashboard_type": "Scrap melting furnace (MS)", "batch": False, "signals": [
        RUN, S("power", "Furnace power", "Furnace_Power", "number", "measurement", "instantaneous", "kW", 0, 20000, 2),
        S("melt_temp", "Melt temperature", "Melt_Temp", "number", "measurement", "instantaneous", "C", 0, 1800, 1, drift=0.001),
        S("heats", "Heat counter", "Heat_Count", "counter", "counter", "delta", "heats", rollover=65536), ALARM]},
    "general": {"label": "Other machine", "dashboard_type": "Other", "batch": False, "signals": [
        RUN, S("speed", "Speed", "Process_Speed", "number", "measurement", "instantaneous", "", 0, None, 2),
        S("temp", "Temperature", "Process_Temp", "number", "measurement", "instantaneous", "C", 0, None, 2, drift=0.001),
        S("count", "Cycle / piece counter", "Cycle_Counter", "counter", "counter", "delta", "pcs", rollover=65536), ALARM]},
}

QUESTIONS = [
    Question("client", "Client (short name, e.g. rubymill)", required=True),
    Question("gateway_id", "Gateway ID (GW_...)", help="From the gateway's label or the dashboard."),
    Question("deployment_id", "Deployment ID (DP_...)"),
    Question("machine_id", "Machine ID (MC_...)", required=True),
    Question("machine_name", "Machine name on site"),
    Question("machine_type", "What kind of machine is it?", "choice", [(k, v["label"]) for k, v in MACHINES.items()], "general", True),
    Question("plc_make", "PLC make", "choice", [("mitsubishi", "Mitsubishi (FX5U, iQ ... MC protocol)"), ("siemens", "Siemens (S7)"),
             ("modbus", "Modbus TCP device (Delta, drive, meter ...)"), ("modbus_rtu", "Modbus RTU (serial)"), ("opcua", "OPC UA server")], "mitsubishi", True),
    Question("plc_model", "PLC model (e.g. FX5U, S7-1200)"),
    Question("plc_ip", "PLC IP address", required=True, when=lambda a: a.get("plc_make") != "modbus_rtu"),
    Question("plc_port", "Port", "number", default="", when=lambda a: a.get("plc_make") != "modbus_rtu", help="Leave empty for the usual port."),
    Question("slmp_transport", "SLMP transport (press Test connection to find out)", "choice", [("udp", "UDP"), ("tcp", "TCP")], "udp",
             when=lambda a: (a.get("plc_make") or "mitsubishi") == "mitsubishi"),
    Question("local_ip", "This laptop's IP on the PLC network (only if it has more than one network card)", when=lambda a: a.get("plc_make") != "modbus_rtu"),
    Question("unit_id", "Modbus unit / slave id", "number", "1", when=lambda a: a.get("plc_make") in ("modbus", "modbus_rtu")),
    Question("rack", "S7 rack", "number", "0", when=lambda a: a.get("plc_make") == "siemens"),
    Question("slot", "S7 slot", "number", "1", when=lambda a: a.get("plc_make") == "siemens"),
    Question("db_numbers", "S7 data blocks to scan (e.g. 1,2,10)", default="1", when=lambda a: a.get("plc_make") == "siemens"),
    Question("prefix", "Tag prefix for this machine (blank if it is the only machine on the gateway; e.g. S1_)"),
    Question("hmi", "Is there an HMI that shows live values?", "bool", default="yes",
             help="You will type what it shows. Without an HMI the evidence is weaker: say who/what you compared against in the notes."),
    Question("hmi_make", "HMI make / model", when=lambda a: a.get("hmi") != "no"),
    Question("byte_order_known", "Do you already know the 32-bit byte order? (leave empty if not)", "choice",
             [("", "Not known: try all"), ("ABCD", "ABCD"), ("CDAB", "CDAB (word-swapped)"), ("BADC", "BADC"), ("DCBA", "DCBA")], "",
             help="Wrong guesses are caught by the checks, but a known order narrows the search."),
    Question("counters_reset", "Counters the operator resets at the end of a batch / heat / roll (signal names, comma separated)",
             when=lambda a: MACHINES.get(a.get("machine_type") or "general", {}).get("batch")),
    Question("shift_counter", "A counter that resets at shift change (signal name, if any)", when=lambda a: MACHINES.get(a.get("machine_type") or "general", {}).get("batch")),
    Question("notes", "Notes for the next engineer (quirks, things that were not obvious)"),
]


def derive(answers):
    """What the answers decide: protocol, port, byte-order priority, the machine's signal list, and any missing required answers."""
    a = {q.key: q.default for q in QUESTIONS if q.default != ""}
    a.update({k: v for k, v in answers.items() if v not in (None, "")})
    make = a.get("plc_make") or "mitsubishi"
    protocol, port, orders = MAKES.get(make, MAKES["modbus"])
    if a.get("byte_order_known") in BYTE_ORDERS_32:
        orders = (a["byte_order_known"],) + tuple(o for o in orders if o != a["byte_order_known"])
    try:
        port = int(a.get("plc_port")) if str(a.get("plc_port", "")).strip() else port
    except ValueError:
        port = port
    mtype = a.get("machine_type") or "general"
    machine = MACHINES.get(mtype, MACHINES["general"])
    missing = [q.key for q in QUESTIONS if q.required and q.visible(a) and not str(a.get(q.key, "")).strip()]
    transport = (a.get("slmp_transport") or "udp") if protocol == "mc" else "tcp"
    return {"protocol": protocol, "port": port, "byte_orders": orders, "machine": machine, "machine_key": mtype, "missing": missing,
            "transport": transport, "local_ip": a.get("local_ip", "")}


# --------------------------------------------------------------------------- importing the programmer's tag list (GX Works3)

# GX Works3 "data type" text -> (our datatype, words used, counts as float)
_GX_TYPES = [
    (re.compile(r"^float.*single", re.I), ("float32", 2)),
    (re.compile(r"^float.*double", re.I), ("float64", 4)),
    (re.compile(r"^double word.*unsigned", re.I), ("uint32", 2)),
    (re.compile(r"^double word", re.I), ("int32", 2)),
    (re.compile(r"^word.*unsigned", re.I), ("uint16", 1)),
    (re.compile(r"^word", re.I), ("int16", 1)),
    (re.compile(r"^bit", re.I), ("bool", 1)),
    (re.compile(r"^string", re.I), ("string", 0)),
]
_DEVICE_RE = re.compile(r"^([A-Za-z]{1,2})(\d+)$")
_ALARM = re.compile(r"alarm|fault|trip|e_?stop|emergency", re.I)
_STATE = re.compile(r"(^|_)(run|running|ready|auto|manual|enable|status|state|on|off|healthy|insert\w*running)(_|$)", re.I)
_SETPT = re.compile(r"(^|_)(sp|set|setpoint|target|man_sp|manual_sp|preset|limit|min|max|calc_sp)(_|$|\d)|_sp_|scada$|_scada", re.I)
_COUNT = re.compile(r"count|total|qty|piece|length|weight|energy|kwh|hours|cycles", re.I)
_UNIT = [(re.compile(r"temp", re.I), "C"), (re.compile(r"freq", re.I), "Hz"), (re.compile(r"current", re.I), "A"),
         (re.compile(r"volt", re.I), "V"), (re.compile(r"pressure", re.I), "bar")]    # only unambiguous words; flow/speed units differ by plant


def guess_role(name, datatype):
    """role and tag type from the NAME only: a suggestion for a person to confirm, never a fact."""
    if _ALARM.search(name):
        return "alarm", "boolean"
    if _SETPT.search(name):
        return "setpoint", "instantaneous"
    if _COUNT.search(name):
        return "counter", "cumulative"
    if _STATE.search(name) and datatype in ("bool", "int16", "uint16"):
        return "state", "boolean"
    return "measurement", "instantaneous"


def parse_gx_labels(text, byte_order="CDAB"):
    """A GX Works3 global-label / device export (tab or comma separated: name, data type, class, device) ->
    {"tags": [...], "skipped": [(name, why)]}. Each tag: name, area, number, datatype, byte_order, words, role, type,
    unit, label_type. Only word devices are kept (D, R, W ...); bit devices, strings and 64-bit floats are reported,
    not guessed. Blank lines and a header row are ignored."""
    tags, skipped, seen = [], [], set()
    for raw in str(text).splitlines():
        if not raw.strip(" \t,;"):
            continue
        parts = [p.strip() for p in (raw.split("\t") if "\t" in raw else raw.split(","))]
        if len(parts) < 4:
            skipped.append((parts[0] or raw.strip()[:30], "needs name, data type, class and device"))
            continue
        name, dtype, cls, dev = parts[0], parts[1], parts[2], parts[3]
        if name.lower() in ("name", "label name", "label") and dev.lower() in ("device", "assign", "device name"):
            continue                                              # header row
        if not _TAG_NAME.match(name):
            skipped.append((name, "the name has characters that are not allowed in a tag name"))
            continue
        m = _DEVICE_RE.match(dev)
        kind = next((v for rx, v in _GX_TYPES if rx.search(dtype)), None)
        if kind is None:
            skipped.append((name, f"data type '{dtype}' is not understood"))
            continue
        datatype, words = kind
        if not m:
            skipped.append((name, f"device '{dev}' is not a device number like D612"))
            continue
        area, number = m.group(1).upper(), int(m.group(2))
        if datatype in ("bool", "string", "float64"):
            skipped.append((name, {"bool": "bit devices are not read yet", "string": "text tags are not supported yet",
                                   "float64": "64-bit floats are not supported yet"}[datatype]))
            continue
        if area not in ("D", "R", "W"):
            skipped.append((name, f"device area {area} is not read yet (word devices D, R, W only)"))
            continue
        if name in seen:
            skipped.append((name, "listed twice"))
            continue
        seen.add(name)
        role, ttype = guess_role(name, datatype)
        unit = next((u for rx, u in _UNIT if rx.search(name)), "")
        tags.append({"name": name, "area": area, "number": number, "datatype": datatype, "words": words,
                     "byte_order": byte_order if words == 2 else "", "role": role, "type": ttype, "unit": unit,
                     "label_type": dtype, "device_class": cls})
    return {"tags": tags, "skipped": skipped}


def imported_signal(t):
    """A wizard 'signal' for an imported tag, so the reading/verify flow treats it like any other."""
    kind = "counter" if t["role"] == "counter" else "bool" if t["type"] == "boolean" else "number"
    integer_counter = t["role"] == "counter" and t["datatype"] in ("int16", "uint16", "int32", "uint32")
    drift = 0.0 if integer_counter else (0.001 if any(rx.search(t["name"]) for rx, u in _UNIT if u == "C") else 0.005)
    return {"key": t["name"], "label": t["name"], "name": t["name"], "meaning": t.get("meaning", ""), "kind": kind, "role": t["role"], "type": t["type"],
            "unit": t["unit"], "min": None, "max": None, "deadband_pct": 2 if t["type"] == "instantaneous" else None, "rollover": None,
            "drift": drift, "imported": t}


def import_regions(tags, max_gap=200):
    """The smallest memory regions that cover the imported addresses. One region per device area (and, for Siemens, per
    data block); split where the gap is bigger than max_gap words, so a list spanning D600-D900 and D3300-D3400 does
    not read 2700 words. Mitsubishi addresses count words; Siemens addresses count bytes (a word every 2 bytes)."""
    groups = {}
    for t in tags:
        groups.setdefault((t["area"], t.get("db")), []).append(t)
    regions = []
    for (area, db), items in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0)):
        step = items[0].get("step", 1)
        spans = sorted((t["number"], t["number"] + (t["words"] - 1) * step) for t in items)
        start, end = spans[0]
        blocks = []
        for a1, b1 in spans[1:]:
            if (a1 - end) / step > max_gap:
                blocks.append((start, end)); start, end = a1, b1
            else:
                end = max(end, b1)
        blocks.append((start, end))
        base = f"DB{db}" if area == "DB" else area
        for lo, hi in blocks:
            regions.append(Region(base if len(blocks) == 1 else f"{base}_{lo}", area, lo, hi, step=step, db=db))
    return regions


def candidate_for_import(t, region):
    """A Candidate pointing at the programmer's address, so decode/verify work exactly as for a found one."""
    c = Candidate(t["name"], region, t["number"], t["datatype"], t["byte_order"] if t["words"] == 2 else ("AB" if t["words"] == 1 else ""), 1, 0, 0, [])
    c.discriminating = True
    return c


def suggest_scale(pairs):
    """pairs = [(hmi_value, read_value)] from the verify readings. If the address is right but the HMI shows the value
    scaled (1.25 on the HMI, 125 in the PLC), the ratio hmi/read is a power of ten in every pair: suggest that scale.
    Returns the scale (0.1, 0.01, 0.001, 10, 100) or None."""
    ratios = []
    for h, r in pairs:
        if not isinstance(h, (int, float)) or not isinstance(r, (int, float)) or r == 0 or h == 0:
            continue
        ratios.append(h / r)
    if len(ratios) < 1:
        return None
    for sc in (0.1, 0.01, 0.001, 10, 100):
        if all(abs(x - sc) <= 0.02 * sc for x in ratios):
            return sc
    return None


# --------------------------------------------------------------------------- importing a TIA Portal tag table (S7)

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _col_index(ref):
    letters = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def read_xlsx_rows(data):
    """The first worksheet of an .xlsx (bytes) as a list of rows of text. Standard library only: an .xlsx is a zip of XML.
    Empty cells keep their column, so a column is always at the same index in every row."""
    z = zipfile.ZipFile(io.BytesIO(data))
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", _NS):
            shared.append("".join(t.text or "" for t in si.iter("{%s}t" % _NS["m"])))
    sheets = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
    if not sheets:
        raise ValueError("no worksheet found in the file")
    rows = []
    for row in ET.fromstring(z.read(sheets[0])).iter("{%s}row" % _NS["m"]):
        cells = {}
        for c in row.findall("m:c", _NS):
            kind, ref = c.get("t"), c.get("r") or ""
            v = c.find("m:v", _NS)
            if kind == "s" and v is not None:
                text = shared[int(v.text)]
            elif kind == "inlineStr":
                text = "".join(t.text or "" for t in c.iter("{%s}t" % _NS["m"]))
            else:
                text = v.text if v is not None and v.text is not None else ""
            cells[_col_index(ref) if ref else len(cells)] = text.strip()
        rows.append([cells.get(i, "") for i in range(max(cells) + 1)] if cells else [])
    return rows


def rows_from_text(text):
    """Rows from a tab-, semicolon- or comma-separated text export."""
    text = str(text)
    first = next((l for l in text.splitlines() if l.strip()), "")
    delim = max(("\t", ";", ","), key=first.count)
    return [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), delimiter=delim)]


_TIA_TYPES = {
    "int": ("int16", 1), "uint": ("uint16", 1), "word": ("uint16", 1),
    "dint": ("int32", 2), "udint": ("uint32", 2), "dword": ("uint32", 2), "real": ("float32", 2),
}
_TIA_UNSUPPORTED = {"bool": "bit tags are not read yet", "byte": "8-bit values are not read yet", "usint": "8-bit values are not read yet",
                    "sint": "8-bit values are not read yet", "char": "text is not supported yet", "string": "text tags are not supported yet",
                    "wstring": "text tags are not supported yet", "lreal": "64-bit floats are not supported yet",
                    "time": "time values are not supported yet", "date_and_time": "time values are not supported yet"}
_TIA_ABS = re.compile(r"^%?([IQM])([BWD]?)\s*(\d+)(?:\.(\d))?$", re.I)
_TIA_DB = re.compile(r"^%?DB(\d+)\.DB([XBWD])(\d+)(?:\.(\d))?$", re.I)
_SIZE_WORDS = {"W": 1, "D": 2}


def _clean_name(raw):
    name = re.sub(r"[^A-Za-z0-9_.\-\[\]]+", "_", raw.strip()).strip("_")
    return name[:100]


def parse_tia_tags(rows, byte_order="ABCD"):
    """A TIA Portal PLC tag table (Excel export of the tag table, or the same as text) -> {"tags": [...], "skipped": [(name, why)]}.
    Columns are found by their header names (Name, Data Type, Logical Address, Comment), in any order. Absolute addresses of
    the inputs, outputs and memory (%IW64, %QD8, %MD20) and of non-optimized data blocks (%DB1.DBD4) are read; tags of optimized
    blocks have no address and cannot be read this way. Names with spaces are renamed with underscores (and say so)."""
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    hdr_i = next((i for i, r in enumerate(rows) if {"name", "data type", "logical address"} <= {str(c).strip().lower() for c in r}), None)
    if hdr_i is None:
        return {"tags": [], "skipped": [], "error": "No header row with Name, Data Type and Logical Address found: export the PLC tag table to Excel from TIA Portal."}
    head = [str(c).strip().lower() for c in rows[hdr_i]]
    col = {k: head.index(k) for k in ("name", "data type", "logical address")}
    col_comment = head.index("comment") if "comment" in head else None
    tags, skipped, seen = [], [], set()
    for r in rows[hdr_i + 1:]:
        get = lambda i: (r[i].strip() if i is not None and i < len(r) else "")
        raw_name, dtype, addr = get(col["name"]), get(col["data type"]), get(col["logical address"])
        if not raw_name:
            continue
        name = _clean_name(raw_name)
        if not name:
            skipped.append((raw_name, "the name has no usable characters"))
            continue
        key = dtype.strip().lower()
        if key in _TIA_UNSUPPORTED:
            skipped.append((raw_name, _TIA_UNSUPPORTED[key]))
            continue
        if key not in _TIA_TYPES:
            skipped.append((raw_name, f"data type '{dtype}' is not understood"))
            continue
        datatype, words = _TIA_TYPES[key]
        if not addr:
            skipped.append((raw_name, "no address: a tag of an optimized block cannot be read by address"))
            continue
        m_abs, m_db = _TIA_ABS.match(addr), _TIA_DB.match(addr)
        if m_abs:
            area, size, number, bit = m_abs.group(1).upper(), m_abs.group(2).upper(), int(m_abs.group(3)), m_abs.group(4)
            db = None
        elif m_db:
            db, size, number, bit = int(m_db.group(1)), m_db.group(2).upper(), int(m_db.group(3)), m_db.group(4)
            area = "DB"
        else:
            skipped.append((raw_name, f"address '{addr}' is not an absolute address like %MW10 or %DB1.DBD4"))
            continue
        if bit is not None or size in ("", "X", "B"):
            skipped.append((raw_name, f"address {addr} is a bit or byte address, not a 16/32-bit value"))
            continue
        if _SIZE_WORDS[size] != words:
            skipped.append((raw_name, f"address {addr} is {16 * _SIZE_WORDS[size]}-bit but data type {dtype} needs {16 * words}"))
            continue
        if number % 2:
            skipped.append((raw_name, f"address {addr} starts at an odd byte: not read yet"))
            continue
        if name in seen:
            skipped.append((raw_name, "listed twice (or two names became the same)"))
            continue
        seen.add(name)
        role, ttype = guess_role(name, datatype)
        comment = get(col_comment)
        tags.append({"name": name, "area": area, "db": db, "number": number, "step": 2, "datatype": datatype, "words": words,
                     "byte_order": byte_order if words == 2 else "", "role": role, "type": ttype,
                     "unit": next((u for rx, u in _UNIT if rx.search(name)), ""), "label_type": dtype, "device_class": "",
                     "meaning": comment[:200], "renamed_from": raw_name if name != raw_name else ""})
    return {"tags": tags, "skipped": skipped}


# --------------------------------------------------------------------------- importing a SIMATIC Manager symbol table (S7-300/400, STEP 7 V5.x)

_SYM_ADDR = r"(?:P?[IQM][BWD]?|DB|FC|FB|OB|SFC|SFB|UDT|VAT|T|C)\s*\d+(?:\.\d)?"
_SYM_ADDR_RE = re.compile(r"^" + _SYM_ADDR + r"$", re.I)
_SYM_LINE_RE = re.compile(r"^(\S.*?)\s{2,}(" + _SYM_ADDR + r")\s+(\S+)\s*(.*)$", re.I)


def _sym_to_tia_address(addr):
    """'MW 10' -> '%MW10', 'M 10.0' -> '%M10.0'. Block entries (DB 1, FC 2) and peripheral addresses are left as they are."""
    a = re.sub(r"\s+", "", addr.upper())
    return "%" + a if re.match(r"^[IQM][BWD]?\d+(\.\d)?$", a) else a


def parse_simatic_symbols(text, byte_order="ABCD"):
    """A SIMATIC Manager symbol table, exported as .sdf (comma separated, quoted), .asc (columns) or tab/semicolon text:
    Symbol, Address, Data type, Comment. Reads the symbols of the inputs, outputs and memory (I/Q/M with W or D) and
    reports the rest (bits, bytes, blocks, timers, counters, peripheral addresses) as skipped. Then the same rules as a TIA Portal list apply."""
    lines = [l for l in str(text).splitlines() if l.strip()]
    rows = []
    for r in rows_from_text("\n".join(lines)):
        if len(r) >= 3 and _SYM_ADDR_RE.match(r[1].strip()):
            rows.append([r[0], r[1], r[2], r[3] if len(r) > 3 else ""])
    if not rows:                                   # .asc: columns separated by runs of spaces
        for l in lines:
            m = _SYM_LINE_RE.match(l.strip())
            if m:
                rows.append([m.group(1).strip(), m.group(2), m.group(3), m.group(4).strip()])
    if not rows:
        return {"tags": [], "skipped": [], "error": "No symbols found. In SIMATIC Manager open the symbol table and use Table > Export, "
                                                   "as .sdf or .asc (columns: Symbol, Address, Data type, Comment)."}
    tia = [["Name", "Path", "Data Type", "Logical Address", "Comment"]]
    for name, addr, dtype, comment in rows:
        tia.append([name, "", dtype, _sym_to_tia_address(addr), comment])
    return parse_tia_tags(tia, byte_order)


def parse_tag_file(path, protocol, byte_order=None):
    """Read an exported tag list from disk: .xlsx (TIA Portal) or text (.tsv/.csv/.txt, GX Works3 or TIA)."""
    with open(path, "rb") as f:
        data = f.read()
    if protocol == "s7":
        if data[:2] == b"PK":
            try:
                rows = read_xlsx_rows(data)
            except (zipfile.BadZipFile, ValueError, KeyError, ET.ParseError) as e:
                return {"tags": [], "skipped": [], "error": f"Could not read the Excel file: {e}"}
        else:
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = data.decode("cp1252", errors="replace")          # SIMATIC Manager exports are in the Windows code page
            if "logical address" not in text.lower():
                return parse_simatic_symbols(text, byte_order or "ABCD")
            rows = rows_from_text(text)
        return parse_tia_tags(rows, byte_order or "ABCD")
    text = data.decode("utf-8-sig", errors="replace")
    return parse_gx_labels(text, byte_order or "CDAB")


# --------------------------------------------------------------------------- scan plan

class Region:
    """One block of PLC memory to read. step = how far the address moves per 16-bit word (1 for MC/Modbus,
    2 for Siemens byte addressing). bit_area = every address is a single bit (Modbus coils)."""
    def __init__(self, name, area, start, end, step=1, bit_area=False, db=None, enabled=True):
        self.name, self.area, self.start, self.end, self.step = name, area, start, end, step
        self.bit_area, self.db, self.enabled = bit_area, db, enabled

    def __repr__(self):
        return f"Region({self.name} {self.start}-{self.end} step {self.step})"


def scan_plan(answers):
    d = derive(answers)
    p = d["protocol"]
    if p == "mc":
        return [Region("D", "D", 0, 1999)]
    if p == "s7":
        regions = []
        for n in re.findall(r"\d+", str(answers.get("db_numbers") or "1")):
            regions.append(Region(f"DB{n}", "DB", 0, 398, step=2, db=int(n)))
        return regions
    if p in ("modbus_tcp", "modbus_rtu"):
        return [Region("holding", "holding", 0, 199), Region("input", "input", 0, 199), Region("coil", "coil", 0, 199, bit_area=True, enabled=False)]
    return []     # OPC UA / BACnet expose real names: browse them, no scan


# --------------------------------------------------------------------------- reading what the HMI shows

def parse_hmi_value(text, drift=0.005):
    """'1.25' -> (1.25, tolerance). The tolerance covers the HMI rounding (half the last digit shown) plus `drift`
    (a fraction of the value) for the machine moving between the HMI refresh and the PLC read.
    Raises ValueError if it is not a number."""
    t = str(text).strip().replace(",", ".")
    if t.lower() in ("on", "true", "running", "yes"):
        return 1.0, 0.0
    if t.lower() in ("off", "false", "stopped", "no"):
        return 0.0, 0.0
    v = float(t)
    if not math.isfinite(v):
        raise ValueError("not a number")
    decimals = len(t.split(".")[1]) if "." in t else 0
    return v, 0.5 * 10 ** (-decimals) + drift * abs(v)


def hmi_reading(signal, text):
    """What the engineer typed for a signal, with that signal's own drift allowance."""
    return parse_hmi_value(text, signal.get("drift", 0.005))


def distinguishable(moments, signal_key):
    """True if at least two readings of this signal differ by more than their combined tolerance. If not, no
    reading tells candidates apart (a constant register would 'match' all of them): the screen asks for another
    reading after the value has moved."""
    vals = [m.hmi[signal_key] for m in moments if signal_key in m.hmi]
    return any(abs(a[0] - b[0]) >= a[1] + b[1] and a[0] != b[0] for i, a in enumerate(vals) for b in vals[i + 1:])


# --------------------------------------------------------------------------- decoding

def _regs_to_bytes(w0, w1):
    return bytes([(w0 >> 8) & 255, w0 & 255, (w1 >> 8) & 255, w1 & 255])


def _order(b, order):
    idx = {"ABCD": (0, 1, 2, 3), "CDAB": (2, 3, 0, 1), "BADC": (1, 0, 3, 2), "DCBA": (3, 2, 1, 0)}[order]
    return bytes(b[i] for i in idx)


def interpretations(w0, w1=None, orders=BYTE_ORDERS_32):
    """Every way the word (and the next one) can be read: [(datatype, byte_order, n_words, raw_value)]."""
    out = []
    s16 = w0 - 0x10000 if w0 >= 0x8000 else w0
    out.append(("int16", "AB", 1, s16))
    out.append(("uint16", "AB", 1, w0))
    sw = ((w0 & 255) << 8) | (w0 >> 8)
    out.append(("int16", "BA", 1, sw - 0x10000 if sw >= 0x8000 else sw))
    out.append(("uint16", "BA", 1, sw))
    if w1 is not None:
        raw = _regs_to_bytes(w0, w1)
        for o in orders:
            b = _order(raw, o)
            out.append(("int32", o, 2, struct.unpack(">i", b)[0]))
            out.append(("uint32", o, 2, struct.unpack(">I", b)[0]))
            f = struct.unpack(">f", b)[0]
            if math.isfinite(f):
                out.append(("float32", o, 2, f))
    return out


# --------------------------------------------------------------------------- moments and candidates

class Moment:
    """One instant: what the HMI showed and what the PLC memory held.
    hmi = {signal_key: (value, tolerance)}; memory = {region_name: {address: 16-bit word}}."""
    def __init__(self, at, hmi, memory, phase="find"):
        self.at, self.hmi, self.memory, self.phase = at, hmi, memory, phase


class Candidate:
    def __init__(self, signal_key, region, addr, datatype, order, scale, matches, total, values):
        self.signal_key, self.region, self.addr = signal_key, region, addr
        self.datatype, self.byte_order, self.scale = datatype, order, scale
        self.matches, self.total, self.values = matches, total, values   # values: engineering value per find moment
        self.discriminating = True      # False when the find readings were too close to tell candidates apart

    @property
    def strong(self):
        return self.matches == self.total

    def label(self):
        bo = f" {self.byte_order}" if self.datatype.endswith("32") else ""
        sc = f" x{self.scale:g}" if self.scale != 1 else ""
        return f"{self.region.name} {self.addr}  {self.datatype}{bo}{sc}"


def _simplicity(c, orders, kind="number"):
    """Lower = more likely: scale 1 first, 16-bit before 32-bit, the make's usual byte order first; when two
    readings are equally right (small values read the same signed or unsigned) a counter is unsigned, a measurement signed."""
    scale_rank = {1: 0, 0.1: 1, 0.01: 2, 0.001: 3, 10: 4, 100: 5}.get(c.scale, 6)
    width = 1 if c.datatype in ("int16", "uint16") else 2
    order_rank = orders.index(c.byte_order) if c.byte_order in orders else (0 if width == 1 else 9)
    unsigned = c.datatype.startswith("uint")
    sign_rank = 0 if unsigned == (kind == "counter") else 1
    return (scale_rank, order_rank, width, sign_rank)


def find_numeric(moments, signal_key, regions, orders=BYTE_ORDERS_32, scales=SCALES, kind="number"):
    """Candidate addresses for a number or counter. Uses every moment that has an HMI value for this signal.
    Returns the strong candidates (fit ALL readings) best first; if there are none, the best partial fits."""
    used = [m for m in moments if signal_key in m.hmi]
    if not used:
        return []
    out = []
    for region in regions:
        if not region.enabled or region.bit_area:
            continue
        addrs = sorted(set.intersection(*[set(m.memory.get(region.name, {})) for m in used]))
        have = set(addrs)
        for a in addrs:
            nxt = a + region.step
            n_ok = nxt in have
            per_moment = []
            for m in used:
                words = m.memory[region.name]
                per_moment.append({(d, o): raw for d, o, _, raw in interpretations(words[a], words[nxt] if n_ok else None, orders)})
            # line the readings up by (datatype, byte order): a float32 that is NaN in one reading must not hide the 16-bit ones
            for (d, o) in set.intersection(*[set(x) for x in per_moment]):
                for sc in (scales if d != "float32" else (1,)):
                    vals, hits = [], 0
                    for mi, m in enumerate(used):
                        eng = per_moment[mi][(d, o)] * sc
                        vals.append(eng)
                        hmi, tol = m.hmi[signal_key]
                        if abs(eng - hmi) <= tol:
                            hits += 1
                    if hits:
                        out.append(Candidate(signal_key, region, a, d, o, sc, hits, len(used), vals))
    strong = [c for c in out if c.strong]
    pool = strong if strong else sorted(out, key=lambda c: -c.matches)[:MAX_CANDIDATES * 4]
    discriminating = distinguishable(used, signal_key)
    for c in pool:
        c.discriminating = discriminating

    def key(c):
        return (-c.matches, _simplicity(c, orders, kind), c.region.name, c.addr)
    return sorted(pool, key=key)[:MAX_CANDIDATES]


def find_bits(moments, signal_key, regions):
    """Candidate bits for an on/off signal. Needs the HMI to have shown BOTH states across the readings,
    otherwise almost every bit would qualify. Returns [(region, address, bit_or_None)] best first."""
    used = [m for m in moments if signal_key in m.hmi]
    states = {int(round(m.hmi[signal_key][0])) for m in used}
    if len(used) < 2 or states != {0, 1}:
        return []
    found = []
    for region in regions:
        if not region.enabled:
            continue
        addrs = sorted(set.intersection(*[set(m.memory.get(region.name, {})) for m in used]))
        for a in addrs:
            if region.bit_area:
                if all(int(m.memory[region.name][a]) & 1 == int(round(m.hmi[signal_key][0])) for m in used):
                    found.append((region, a, None))
            else:
                for b in range(16):
                    if all(((m.memory[region.name][a] >> b) & 1) == int(round(m.hmi[signal_key][0])) for m in used):
                        found.append((region, a, b))
    return found[:MAX_CANDIDATES * 3]


def decode_candidate(memory, c):
    """The engineering value of a candidate in one memory snapshot (None if the address is missing)."""
    words = memory.get(c.region.name, {})
    if c.addr not in words:
        return None
    w0 = words[c.addr]
    w1 = words.get(c.addr + c.region.step)
    for d, o, _, raw in interpretations(w0, w1, BYTE_ORDERS_32):
        if d == c.datatype and o == c.byte_order:
            return raw * c.scale
    return None


def decode_bit(memory, region, addr, bit):
    words = memory.get(region.name, {})
    if addr not in words:
        return None
    return int(words[addr]) & 1 if bit is None else (int(words[addr]) >> bit) & 1


# --------------------------------------------------------------------------- addresses

def format_address(protocol, region, addr, datatype, bit=None):
    if protocol == "mc":
        return f"{region.area}{addr}" + (f".{bit:X}" if bit is not None else "")
    if protocol == "s7":
        w = "D" if datatype in ("int32", "uint32", "float32") else "W"
        base = f"DB{region.db}." if region.area == "DB" else region.area
        return f"{base}DBX{addr}.{bit}" if bit is not None and region.area == "DB" else f"{base}DB{w}{addr}" if region.area == "DB" else f"{base}{w}{addr}"
    if protocol in ("modbus_tcp", "modbus_rtu"):
        return f"{region.area}[{addr}]" + (f".{bit}" if bit is not None else "")
    return str(addr)


# --------------------------------------------------------------------------- evidence, the gate, the pack

def evidence(hmi_value, read_value, at, phase, who):
    return {"hmi_value": hmi_value, "read_value": read_value, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at)),
            "confirmed_by": who or "", "source": "hmi", "phase": phase}


def _epoch(s):
    return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))


def _matches(e, tag):
    h, r = e["hmi_value"], e["read_value"]
    if isinstance(h, str) or isinstance(r, str):
        return str(h) == str(r)
    tol = e.get("tolerance")
    if tol is None:
        tol = tag.get("tolerance")
    if tol is None:
        tol = max(abs(h) * 0.01, 0.05)
    return abs(h - r) <= tol


def judge(tag):
    """'confirmed' | 'probable' | 'unverified'. Identical rule to the dashboard's commissioningPack.js judge()."""
    matching = [e for e in tag.get("evidence", []) if _matches(e, tag)]
    if not matching:
        return "unverified"
    good = sorted((e for e in matching if e.get("phase", "verify") != "find"), key=lambda e: e["at"])
    constant_ok = tag.get("role") in ("setpoint", "text") or tag.get("type") == "string"
    for i in range(len(good)):
        for j in range(i + 1, len(good)):
            if _epoch(good[j]["at"]) - _epoch(good[i]["at"]) < MIN_GAP_S:
                continue
            if constant_ok or str(good[i]["hmi_value"]) != str(good[j]["hmi_value"]):
                return "confirmed"
    return "probable"


def make_tag(signal, address, datatype, byte_order, scale, evid, name=None, unit=None, lo=None, hi=None):
    tag = {
        "name": name or signal["name"], "address": address, "datatype": datatype,
        "role": signal["role"], "type": signal["type"], "unit": signal["unit"] if unit is None else unit,
        "meaning": (signal.get("meaning") or signal["label"])[:200], "evidence": list(evid),
    }
    if byte_order and datatype.endswith("32"):
        tag["byte_order"] = byte_order
    if signal["type"] == "instantaneous":
        tag["scale"] = scale if scale else 1
        tag["offset"] = 0
        tag["min"] = signal["min"] if lo is None else lo
        tag["max"] = signal["max"] if hi is None else hi
        if signal.get("deadband_pct") is not None:
            tag["deadband_pct"] = signal["deadband_pct"]
    elif signal["type"] == "delta":
        tag["scale"] = scale if scale else 1
        if signal.get("rollover"):
            tag["rollover"] = 65536 if datatype in ("int16", "uint16") else 4294967296
    tag["confidence"] = judge(tag)
    return {k: v for k, v in tag.items() if v is not None}


def build_pack(answers, tags, who=""):
    d = derive(answers)
    a = answers
    plc = {"id": "plc1", "protocol": d["protocol"], "model": a.get("plc_model", "")}
    if a.get("plc_ip"):
        plc["ip"] = a["plc_ip"]
    if d["port"]:
        plc["port"] = d["port"]
    if d["protocol"] == "mc":
        plc["transport"] = d["transport"]
    if a.get("prefix"):
        plc["prefix"] = a["prefix"]
    for k in ("unit_id", "rack", "slot"):
        if str(a.get(k, "")).strip():
            try:
                plc[k] = int(a[k])
            except ValueError:
                pass
    machine = {"machine_id": a.get("machine_id", ""), "name": a.get("machine_name", ""), "type": d["machine"]["dashboard_type"], "plc": plc, "tags": tags}
    names = [t.strip() for t in re.split(r"[,;]", a.get("counters_reset") or "") if t.strip()]
    if names:
        machine["batch"] = {"resetTags": names}
        if str(a.get("shift_counter", "")).strip():
            machine["batch"]["shiftTag"] = a["shift_counter"].strip()
    questionnaire = {k: v for k, v in a.items() if re.fullmatch(r"[a-z][a-z0-9_]{0,39}", k) and k not in ("plc_ip", "local_ip") and v not in (None, "")}
    return {
        "schema": SCHEMA, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "created_by": who, "tool": TOOL,
        "site": {"client": a.get("client", ""), "gateway_id": a.get("gateway_id", ""), "deployment_id": a.get("deployment_id", "")},
        "questionnaire": {k: (v if isinstance(v, bool) else str(v)[:300]) for k, v in questionnaire.items()},
        "machines": [machine],
    }


def summary(pack):
    out = {"confirmed": 0, "probable": 0, "unverified": 0}
    for m in pack["machines"]:
        for t in m["tags"]:
            out[judge(t)] += 1
    return out


# --------------------------------------------------------------------------- local pack checks (the dashboard re-checks everything)

_TAG_RE = re.compile(r"^[A-Za-z0-9_.\-\[\]]{1,100}$")
_TAG_NAME = _TAG_RE


def validate_pack(pack):
    """The basics the dashboard will insist on, so a bad pack is caught on site and not at import. Returns a list of problems."""
    errs = []
    if not isinstance(pack, dict) or pack.get("schema") != SCHEMA:
        return [f'schema must be "{SCHEMA}"']
    ms = pack.get("machines")
    if not isinstance(ms, list) or not ms:
        return ["machines: at least one machine"]
    for mi, m in enumerate(ms):
        p = f"machines[{mi}]"
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", str(m.get("machine_id", ""))):
            errs.append(f"{p}.machine_id: required (letters, digits, . _ -)")
        if (m.get("plc") or {}).get("protocol") not in PROTOCOLS:
            errs.append(f"{p}.plc.protocol: one of {', '.join(PROTOCOLS)}")
        names = set()
        for ti, t in enumerate(m.get("tags") or []):
            tp = f"{p}.tags[{ti}]"
            if not _TAG_RE.match(str(t.get("name", ""))):
                errs.append(f"{tp}.name: letters, digits, _ . - [ ]")
            elif t["name"] in names:
                errs.append(f"{tp}.name: listed twice")
            names.add(t.get("name"))
            if not t.get("address"):
                errs.append(f"{tp}.address: required")
            if t.get("datatype") not in DATATYPES:
                errs.append(f"{tp}.datatype: one of {', '.join(DATATYPES)}")
            if t.get("role") not in ROLES:
                errs.append(f"{tp}.role: one of {', '.join(ROLES)}")
            if t.get("type") not in TAG_TYPES:
                errs.append(f"{tp}.type: one of {', '.join(TAG_TYPES)}")
            if t.get("scale") == 0:
                errs.append(f"{tp}.scale: cannot be 0")
        if not m.get("tags"):
            errs.append(f"{p}.tags: at least one tag")
    return errs


def save_pack(pack, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pack, f, indent=2)

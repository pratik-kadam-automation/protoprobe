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
import json
import math
import re
import struct
import time

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
    return {"protocol": protocol, "port": port, "byte_orders": orders, "machine": machine, "machine_key": mtype, "missing": missing}


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
        "meaning": signal["label"], "evidence": list(evid),
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
    questionnaire = {k: v for k, v in a.items() if re.fullmatch(r"[a-z][a-z0-9_]{0,39}", k) and k not in ("plc_ip",) and v not in (None, "")}
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

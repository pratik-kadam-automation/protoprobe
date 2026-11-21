"""plc_catalog.py -- the PLC types of the PUSR M300 gateway, each with its own address types, as the gateway's "Add node" dialog shows them.

Data and small helpers only (no screens, no network), so it can be tested anywhere. Two things are kept apart on purpose:
  * what the PLC / the gateway has (`areas`, taken from the owner's screenshots of the gateway dialog, see docs/m300-protocols.md), and
  * what ProtoProbe's own drivers can read today (`Area.readable` and `PlcType.driver`). A PLC type whose `driver` is None is listed so the
    catalog matches the gateway, but ProtoProbe cannot connect to it yet.
Nothing here is a verified address map: real addresses come from the PLC manual. `verified` says whether the area list was seen on the gateway.
"""
from dataclasses import dataclass, field

WIDTHS = ("bit", "word", "dword")

# The gateway's protocol list, in its own order and spelling (screenshots 2026-11-19).
GATEWAY_PROTOCOLS = (
    "Virtual Slave", "Modbus_TCP", "Modbus_RTU", "Modbus_ASC", "DVP_RTU", "FATEK_COM", "FX1S_COM", "FX2N(1N)_COM", "FX3U_COM", "FX5U_COM",
    "FX5U_TCP", "FX3U_ADP_TCP", "Q_TCP", "S7_200 Smart", "S7_1200", "S7_200 PPI", "S7_1500", "FINS_TCP", "FINS_COM", "S7-300", "S7-400",
    "DLT-645-2007", "IEC_104", "BACnet IP", "IEC61850", "h3_5u_tcp", "DNP3", "EtherNet/IP(Unconnected)", "EtherNet/IP(Compactlogix)",
)


@dataclass(frozen=True)
class Area:
    flag: str                 # what the gateway's Register dropdown shows: "0", "X", "WX", "M8000" ...
    name: str                 # "Coils status", "Input relay" ...
    width: str                # "bit" | "word" | "dword"
    readable: bool = False    # can ProtoProbe's driver read this area today?
    pp: tuple = ()            # how ProtoProbe names it in a tag: (field, value), e.g. ("register_type", "holding")


@dataclass(frozen=True)
class PlcType:
    key: str                  # the gateway's name: "Modbus_TCP"
    label: str
    family: str
    link: str                 # "ethernet" | "serial" | "other"
    driver: str = None        # ProtoProbe protocol id (protoprobe.PROTOCOL_LABELS), None = not built yet
    interface: dict = field(default_factory=dict)   # defaults that differ from the protocol's usual ones
    areas: tuple = ()
    verified: bool = False    # True when the area list was read off the gateway's dialog
    complete: bool = True     # False when the dropdown scrolled and not everything was seen
    notes: tuple = ()


def _modbus_areas(readable=True):
    """The four Modbus areas. `readable=False` for a variant ProtoProbe has no driver for (Modbus ASCII)."""
    return (
        Area("0", "Coils status", "bit", readable, ("register_type", "coil") if readable else ()),
        Area("1", "Discrete inputs", "bit", False),                       # ProtoProbe's Modbus driver has no discrete-input read
        Area("3", "Input registers", "word", readable, ("register_type", "input") if readable else ()),
        Area("4", "Holding registers", "word", readable, ("register_type", "holding") if readable else ()),
    )


_S7_AREAS = (
    Area("DB", "Data block", "word", True, ("area", "DB")), Area("I", "Inputs", "bit", True, ("area", "I")),
    Area("Q", "Outputs", "bit", True, ("area", "Q")), Area("M", "Markers", "bit", True, ("area", "M")),
)
_S7_NOTES = ("Absolute DB reads need 'Permit access with PUT/GET' on, and 'Optimized block access' OFF for that DB.",)
_MC_AREAS = (   # what ProtoProbe's Mitsubishi driver reads (word path); the PLC has more (X, Y, S, T, C ...)
    Area("D", "Data register", "word", True, ("device_code", "D")), Area("M", "Internal relay", "word", True, ("device_code", "M")),
    Area("R", "File register", "word", True, ("device_code", "R")), Area("W", "Link register", "word", True, ("device_code", "W")),
)
_FX_OCTAL = "On Mitsubishi FX the X and Y numbers are written in octal (X0-X7, then X10): check the manual before converting."

_DETAILED = (
    PlcType("Modbus_TCP", "Modbus TCP", "modbus", "ethernet", "modbus_tcp", areas=_modbus_areas(), verified=True,
            notes=("The gateway counts Modbus references from 1 (preview 00001 for number 1).",)),
    PlcType("Modbus_RTU", "Modbus RTU (serial)", "modbus", "serial", "modbus_rtu", areas=_modbus_areas(), verified=True),
    PlcType("Modbus_ASC", "Modbus ASCII (serial)", "modbus", "serial", None, areas=_modbus_areas(readable=False), verified=True),
    PlcType("DVP_RTU", "Delta DVP (serial)", "delta_dvp", "serial", None, verified=True, areas=(
        Area("X", "Input relay", "bit"), Area("Y", "Output relay", "bit"), Area("M", "Auxiliary relay", "bit"), Area("S", "Step relay", "bit"),
        Area("T", "Timer relay", "bit"), Area("TV", "Timer value", "word"), Area("C", "Counter relay", "bit"), Area("CV", "Counter value", "word"),
        Area("D", "Data register", "word")), notes=("The gateway labels CV and D as 'Timer Value': a typing mistake in its dialog.",)),
    PlcType("FATEK_COM", "Fatek (serial)", "fatek", "serial", None, verified=True, complete=False, areas=(
        Area("X", "Input relay", "bit"), Area("WX", "Input relay", "word"), Area("DWX", "Input relay", "dword"),
        Area("Y", "Output relay", "bit"), Area("WY", "Output relay", "word"), Area("DWY", "Output relay", "dword"),
        Area("M", "Internal relay", "bit"), Area("WM", "Internal relay", "word"), Area("DWM", "Internal relay", "dword"),
        Area("S", "Step relay", "bit"), Area("WS", "Step relay", "word"), Area("DWS", "Step relay", "dword"),
        Area("T", "Timer relay", "bit"), Area("WT", "Timer relay", "word"), Area("DWT", "Timer relay", "dword"),
        Area("C", "Counter relay", "bit"), Area("WC", "Counter relay", "word"), Area("DWC", "Counter relay", "dword"),
        Area("RT", "Timer present value", "word"), Area("DRT", "Timer present value", "dword"),
        Area("RC", "Counter present value", "word"), Area("DRC", "Counter present value", "dword"),
        Area("R", "Data register HR", "word"), Area("DR", "Data register HR", "dword"),
        Area("D", "Data register DR", "word"), Area("DD", "Data register DR", "dword"))),
    PlcType("FX1S_COM", "Mitsubishi FX1S (serial)", "mitsubishi_fx_serial", "serial", None, verified=True, areas=(
        Area("X", "Input relay", "bit"), Area("Y", "Output relay", "bit"), Area("M0", "Auxiliary relay", "bit"),
        Area("M8000", "Special auxiliary relay", "bit"), Area("S", "Status relay", "bit"), Area("T", "Timer flag", "bit"),
        Area("C", "Counter flag", "bit"), Area("CV0", "Counter value C0-C199", "word"), Area("CV200", "Counter value C200 and up", "dword"),
        Area("D0", "Data register", "word"), Area("D8000", "Special data register", "word")), notes=(_FX_OCTAL,)),
    PlcType("FX2N(1N)_COM", "Mitsubishi FX2N/FX1N (serial)", "mitsubishi_fx_serial", "serial", None, verified=True, complete=False, areas=(
        Area("X", "Input relay", "bit"), Area("Y", "Output relay", "bit"), Area("M0", "Auxiliary relay", "bit"),
        Area("M8000", "Special auxiliary relay", "bit"), Area("S", "Status relay", "bit"), Area("T", "Timer flag", "bit")),
        notes=("Only the top of the list was seen; assume the rest matches FX1S_COM until a screenshot says otherwise.", _FX_OCTAL)),
    PlcType("S7_1200", "Siemens S7-1200", "s7", "ethernet", "s7comm", {"rack": "0", "slot": "1"}, _S7_AREAS, notes=_S7_NOTES),
    PlcType("S7_1500", "Siemens S7-1500", "s7", "ethernet", "s7comm", {"rack": "0", "slot": "1"}, _S7_AREAS, notes=_S7_NOTES),
    PlcType("S7-300", "Siemens S7-300", "s7", "ethernet", "s7comm", {"rack": "0", "slot": "2"}, _S7_AREAS),
    PlcType("S7-400", "Siemens S7-400", "s7", "ethernet", "s7comm", {"rack": "0"}, _S7_AREAS, notes=("The CPU slot depends on the rack layout: check the hardware configuration.",)),
    PlcType("FX5U_TCP", "Mitsubishi FX5U (Ethernet, SLMP)", "mitsubishi_mc", "ethernet", "mc_protocol_3e", areas=_MC_AREAS, complete=False,
            notes=("ProtoProbe reads D, M, R, W only; the PLC also has X, Y, S, T, C ... (not built).", _FX_OCTAL)),
    PlcType("Q_TCP", "Mitsubishi Q series (Ethernet, MC)", "mitsubishi_mc", "ethernet", "mc_protocol_3e", areas=_MC_AREAS, complete=False,
            notes=("ProtoProbe reads D, M, R, W only.",)),
)

_DETAILED = _DETAILED + (
    PlcType("BACnet IP", "BACnet/IP (building automation)", "bacnet", "ethernet", "bacnet_ip",
            notes=("Addresses are an object type and an instance number (for example analogInput 1), not memory areas. "
                   "ProtoProbe's BACnet reader needs this PC's own IP and the device's instance number.",)),
)

_BY_KEY = {p.key: p for p in _DETAILED}
CATALOG = tuple(_BY_KEY.get(k) or PlcType(k, k, "listed", "other") for k in GATEWAY_PROTOCOLS)    # every gateway protocol, detailed or not
_CAT_BY_KEY = {p.key: p for p in CATALOG}


def get(key):
    return _CAT_BY_KEY[key]


def connectable():
    """The PLC types ProtoProbe can connect to today."""
    return [p for p in CATALOG if p.driver]


def readable_areas(key):
    return [a for a in get(key).areas if a.readable]


def address_preview(key, flag, number):
    """The text the gateway shows under the Register box. Verified against the dialog only for Modbus (flag 0, number 1 -> '00001(bit)')
    and the X relay style ('X0(bit)'); for the other areas it is the same pattern, not a verified rule."""
    p = get(key)
    area = next((a for a in p.areas if a.flag == flag), None)
    if area is None:
        raise KeyError(f"{key} has no area {flag!r}")
    n = int(number)
    text = f"{flag}{n:04d}" if p.family == "modbus" else f"{flag}{n}"
    return f"{text}({area.width})"


@dataclass(frozen=True)
class ScanPreset:
    """A ready-made scan for the Scan for live tags window. `single` fills the single area/range fields (the window's own field names);
    `full` sets the whole-PLC checkboxes (`include`) and the max address (`max`). Ranges are starting points, not the PLC's real sizes:
    the Siemens scan stops at the end of the memory the PLC really has."""
    label: str
    single: dict = field(default_factory=dict)
    full: dict = field(default_factory=dict)
    note: str = ""


_DEFAULT_TYPE_FOR_PROTOCOL = {"s7comm": "S7_1200", "mc_protocol_3e": "Q_TCP", "modbus_tcp": "Modbus_TCP", "modbus_rtu": "Modbus_RTU"}


def default_type(protocol):
    """The catalog type whose presets to use for a connection that was made by choosing only a protocol."""
    return _DEFAULT_TYPE_FOR_PROTOCOL.get(protocol)


def scan_presets(key):
    """Scan presets for a PLC type ProtoProbe can connect to; [] for the others."""
    p = _CAT_BY_KEY.get(key)
    if p is None or not p.driver:
        return []
    if p.driver not in ("s7comm", "mc_protocol_3e", "modbus_tcp", "modbus_rtu"):
        return []                                        # BACnet and the like name objects, not address ranges: nothing to scan
    if p.driver == "s7comm":
        return [
            ScanPreset("Inputs I, bytes 0-63", {"area": "I", "start": 0, "end": 63},
                       note="Digital inputs: the results show which bits are on, and which changed. Operate a switch or sensor during the scan."),
            ScanPreset("Outputs Q, bytes 0-63", {"area": "Q", "start": 0, "end": 63},
                       note="Digital outputs: the results show which bits are on, and which changed."),
            ScanPreset("Markers M, bytes 0-255", {"area": "M", "start": 0, "end": 255},
                       note="Internal flags and counters. Clock-memory bits blink on their own, so many rows may show 'changed'."),
            ScanPreset("Everything except data blocks (I, Q, M)", full={"include": ("I", "Q", "M"), "max": 2000},
                       note="One background job over inputs, outputs and markers. The scan stops where the PLC's memory ends."),
            ScanPreset("A data block: DB1, bytes 0-99", {"area": "DB", "db_number": 1, "start": 0, "end": 99},
                       note="Only works for a DB with 'Optimized block access' OFF and PUT/GET allowed. Use Discover DBs to find DB numbers."),
        ]
    if p.driver == "mc_protocol_3e":
        return [
            ScanPreset("Data registers D0-D199", {"device_code": "D", "start": 0, "end": 199}),
            ScanPreset("Internal relays M0-M99 (as words)", {"device_code": "M", "start": 0, "end": 99}),
            ScanPreset("File registers R0-R99", {"device_code": "R", "start": 0, "end": 99}),
            ScanPreset("Link registers W0-W99", {"device_code": "W", "start": 0, "end": 99}),
            ScanPreset("D and M, up to 2000", full={"include": ("D", "M"), "max": 2000},
                       note="One background job. A big range takes minutes: check the estimate before you confirm."),
        ]
    return [
        ScanPreset("Holding registers 0-99", {"register_type": "holding", "start": 0, "end": 99}),
        ScanPreset("Holding registers 100-199", {"register_type": "holding", "start": 100, "end": 199}),
        ScanPreset("Input registers 0-99", {"register_type": "input", "start": 0, "end": 99}),
        ScanPreset("Holding and input registers, up to 500", full={"include": ("holding", "input"), "max": 500}),
    ]


def dropdown_entries():
    """[(text, key)] for a PLC-type dropdown: every gateway protocol, in the gateway's order. Types ProtoProbe cannot connect to yet say so."""
    return [(f"{p.key}  --  {p.label}" + ("" if p.driver else "   [not supported yet]"), p.key) for p in CATALOG]


def describe(key):
    """A few plain lines for the Add Connection window: can ProtoProbe connect, what the PLC's address areas are, and what to watch out for."""
    p = get(key)
    lines = []
    if p.driver:
        readable = ", ".join(f"{a.flag} ({a.name})" for a in p.areas if a.readable)
        lines.append(f"ProtoProbe can connect to this type. It reads: {readable}." if readable else "ProtoProbe can connect to this type.")
        not_read = [a for a in p.areas if not a.readable]
        if not_read:
            lines.append("The PLC also has (not readable by ProtoProbe yet): " + ", ".join(a.flag for a in not_read) + ".")
    else:
        lines.append(f"{p.label}: ProtoProbe cannot connect to this type yet (no driver). It is listed so the list matches the gateway's.")
        if p.areas:
            lines.append("Address areas on the gateway: " + ", ".join(f"{a.flag} ({a.name}, {a.width})" for a in p.areas) + ".")
    if not p.verified and p.areas:
        lines.append("Area list not seen on the gateway: check the PLC manual.")
    if not p.complete:
        lines.append("The list may be incomplete (only part of it was seen).")
    lines.extend(p.notes)
    lines.extend(SETUP_HINTS.get(key, []))
    return "\n".join(lines)


# Help shown in the Add Connection window for types that need setup on the PLC first. NOT part of the shared contract (the exported catalog does not carry it).
SETUP_HINTS = {
    "Q_TCP": [
        "Which module is it? A name like Q64PN is the POWER SUPPLY. The CPU is the module printed Q03UDE, Q06UDEH, Q03UDV ... (the bigger one next to it).",
        "Ethernet: a CPU with E or V in its name (Q03UDE, Q04UDEH, Q06UDEH, Q03UDV ...) has an Ethernet port built in; the others need an Ethernet module such as QJ71E71-100.",
        "Port: there is NO default. It is the port someone set under 'Open settings' in GX Works2 (MC protocol, binary code, TCP or UDP). GX Works2 shows it in HEX: 1388 = 5000, 1389 = 5001, "
        "138A = 5002, 1393 = 5011. Usual choices: 5000, 5001, 5006, 5007. Not known? Network tools > Port scanner, ports 5000-5020 (the PLC IP must answer first).",
        "If nothing answers: the Open settings were never written to the PLC (write the parameters, then power the PLC off and on once), or the CPU has no Ethernet port.",
        "On Q series the X and Y numbers are hexadecimal (X0 to XF, then X10). ProtoProbe reads D, M, R, W.",
    ],
    "FX5U_TCP": [
        "FX5U: the SLMP port is set in GX Works3 (Ethernet port > external device communication); 5007 and 5006 are the usual choices.",
    ],
}


# What a deployment needs, per ProtoProbe driver id: the protocol name the dashboard's commissioning pack uses, the Python package(s) the reader needs,
# and the usual port. Part of the shared contract (`plc-catalog/1`): the private backend keeps an identical copy of the exported file.
_DRIVER_PACKAGES = {
    "s7comm": {"pack_protocol": "s7", "python_packages": ["python-snap7==3.3.1"], "default_port": 102},
    "mc_protocol_3e": {"pack_protocol": "mc", "python_packages": [], "default_port": 5006},
    "modbus_tcp": {"pack_protocol": "modbus_tcp", "python_packages": ["pymodbus"], "default_port": 502},
    "modbus_rtu": {"pack_protocol": "modbus_rtu", "python_packages": ["pymodbus", "pyserial"], "default_port": None},
    "bacnet_ip": {"pack_protocol": "bacnet", "python_packages": ["BAC0"], "default_port": 47808},
}


def export_contract():
    """The catalog as plain data for the other repos: {"schema": "plc-catalog/1", "types": [...]}. Types without a driver are listed with driver null,
    so a deployment tool can say 'not supported yet' instead of guessing."""
    types = []
    for p in CATALOG:
        extra = _DRIVER_PACKAGES.get(p.driver, {})
        types.append({
            "key": p.key, "label": p.label, "family": p.family, "link": p.link, "driver": p.driver,
            "pack_protocol": extra.get("pack_protocol"), "python_packages": extra.get("python_packages", []),
            "default_port": extra.get("default_port"), "interface_defaults": dict(p.interface),
            "areas": [{"flag": a.flag, "name": a.name, "width": a.width, "readable": a.readable} for a in p.areas],
            "notes": list(p.notes),
        })
    return {"schema": "plc-catalog/1", "types": types}


def modbus_zero_based(number):
    """ASSUMPTION, to be proven against a simulator: the gateway's number is the 1-based Modbus reference (its preview shows 00001 for 1),
    while ProtoProbe's Modbus tags use the protocol's 0-based address."""
    n = int(number)
    if n < 1:
        raise ValueError("the gateway counts Modbus references from 1")
    return n - 1

#!/usr/bin/env python3
"""
plcread.py -- connect to a PLC from the command line, check it answers, and read your tags. No screen, no dashboard.

  python plcread.py probe 192.168.100.13                       which port and transport (UDP/TCP) really answer SLMP
  python plcread.py read  --make mitsubishi --ip 192.168.100.13 --tags labels.tsv     read every tag in your exported list
  python plcread.py read  --make siemens --ip 10.0.0.5 --tags plc_tags.xlsx --samples 5 --interval 2
  python plcread.py raw   --make mitsubishi --ip 192.168.100.13 --start D612 --count 40  look at raw registers

--make mitsubishi: SLMP/MC (UDP or TCP, found automatically unless you say), tag list = GX Works3 label export
--make siemens:    S7 (needs "pip install python-snap7"), tag list = TIA Portal PLC tag table exported to Excel

Everything is READ-ONLY. Values are shown the way ProtoProbe decodes them: 32-bit floats word-swapped (CDAB) for Mitsubishi,
big-endian (ABCD) for Siemens (--byte-order overrides). A tag is flagged when it cannot be a sane value:
  NO DATA          the PLC reply did not contain this address
  BAD FLOAT        the two registers are not a valid float: usually the wrong word order or the wrong address
  SUSPECT          a float that is absurdly huge or tiny but not zero: the classic sign of the wrong word order
  FLAT             unchanged in every sample (only judged with --samples 3 or more; a stopped machine is flat too)
Exit code: 0 all fine, 2 some tags flagged, 1 could not connect or read.
"""
import argparse
import csv
import io
import json
import math
import re
import sys
import time

import commissioning as cm
import slmp_probe
from plc_reader import DriverReader, PROTOCOL_KEY

MAKES = {"mitsubishi": "mc", "siemens": "s7", "modbus": "modbus_tcp"}


# --------------------------------------------------------------------------- connection

def build_interface(args, protocol, transport=None, port=None):
    if protocol == "mc":
        iface = {"ip": args.ip, "port": port or args.port or 5006, "transport": transport or "udp", "timeout_s": args.timeout}
        if args.local_ip:
            iface["local_ip"] = args.local_ip
        return iface
    if protocol == "s7":
        return {"ip": args.ip, "rack": args.rack, "slot": args.slot}
    return {"ip": args.ip, "port": args.port or 502, "unit_id": args.unit_id}


def choose_slmp(args, out):
    """UDP or TCP, which port: as given, or found by sending a real read to each common port."""
    if args.transport != "auto" and args.port:
        return args.transport, args.port
    ports = (args.port,) if args.port else slmp_probe.DEFAULT_PORTS
    transports = ("udp", "tcp") if args.transport == "auto" else (args.transport,)
    print(f"Looking for an SLMP answer from {args.ip} ...", file=out)
    res = slmp_probe.probe(args.ip, ports=ports, local_ip=args.local_ip or None, timeout=args.timeout, transports=transports)
    b = slmp_probe.best(res)
    if not b:
        print(slmp_probe.describe(res), file=out)
        return None, None
    print(f"Using {b['transport'].upper()} port {b['port']}.", file=out)
    return b["transport"], b["port"]


# --------------------------------------------------------------------------- flags

def judge_value(value, memory_has_address, datatype):
    """('', ok) or (flag, False)."""
    if not memory_has_address:
        return "NO DATA"
    if value is None:
        return "BAD FLOAT" if datatype == "float32" else "NO DATA"
    if datatype == "float32" and value != 0 and (abs(value) > 1e9 or abs(value) < 1e-6):
        return "SUSPECT"
    return ""


def fmt(value, datatype):
    if value is None:
        return "-"
    if datatype == "float32":
        return f"{value:.3f}"
    return str(int(value)) if float(value).is_integer() else f"{value:.3f}"


def table(rows, headers):
    widths = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]
    line = lambda r: "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)).rstrip()
    return "\n".join([line(headers), line(["-" * w for w in widths])] + [line(r) for r in rows])


# --------------------------------------------------------------------------- commands

def cmd_probe(args, out, driver_factory):
    ports = tuple(int(p) for p in args.ports.split(",")) if args.ports else slmp_probe.DEFAULT_PORTS
    res = slmp_probe.probe(args.ip, ports=ports, local_ip=args.local_ip or None, timeout=args.timeout)
    rows = [[r["transport"].upper(), r["port"], r["status"], f"0x{r['end_code']:04X}" if r["end_code"] is not None else "", r["text"]] for r in res
            if r["status"] in ("ok", "answered", "other") or args.all]
    if rows:
        print(table(rows, ["Transport", "Port", "Result", "End code", "Note"]), file=out)
    print(slmp_probe.describe(res), file=out)
    return 0 if slmp_probe.best(res) else 1


def cmd_read(args, out, driver_factory):
    protocol = MAKES[args.make]
    if protocol == "modbus_tcp":
        print("Reading a tag list is available for Mitsubishi and Siemens. For Modbus use: plcread.py raw --make modbus ...", file=out)
        return 1
    order = args.byte_order or ("CDAB" if protocol == "mc" else "ABCD")
    parsed = cm.parse_tag_file(args.tags, protocol, order)
    if parsed.get("error"):
        print(parsed["error"], file=out)
        return 1
    tags = parsed["tags"]
    if not tags:
        print("No usable tags in that file.", file=out)
        for n, why in parsed["skipped"][:10]:
            print(f"  {n}: {why}", file=out)
        return 1
    transport = port = None
    if protocol == "mc":
        transport, port = (args.transport, args.port) if args.transport != "auto" and args.port else choose_slmp(args, out)
        if transport is None:
            return 1
    iface = build_interface(args, protocol, transport, port)
    regions = cm.import_regions(tags)
    reader = DriverReader(protocol, iface, driver_factory)
    cands = {t["name"]: cm.candidate_for_import(t, next(g for g in regions if g.area == t["area"] and g.db == t.get("db") and g.start <= t["number"] <= g.end)) for t in tags}
    samples = []
    for i in range(args.samples):
        if i:
            time.sleep(args.interval)
        try:
            memory = {g.name: reader.read_region(g) for g in regions}
        except Exception as e:      # noqa: BLE001 -- the driver's own message is the useful part
            print(f"Could not read the PLC: {type(e).__name__}: {e}".rstrip(": "), file=out)
            print("Check the IP, that the PLC allows this access (SLMP on / PUT-GET allowed) and, with two network cards, --local-ip.", file=out)
            return 1
        samples.append(memory)
    result, flagged = [], 0
    for t in tags:
        c = cands[t["name"]]
        vals, flags = [], set()
        for mem in samples:
            words = mem.get(c.region.name, {})
            has = c.addr in words and (t["words"] == 1 or (c.addr + c.region.step) in words)
            v = cm.decode_candidate(mem, c) if has else None
            vals.append(v)
            f = judge_value(v, has, t["datatype"])
            if f:
                flags.add(f)
        if len(samples) >= 3 and None not in vals and len(set(vals)) == 1:
            flags.add("FLAT")
        flagged += 1 if flags else 0
        label = cm.format_address(protocol, c.region, t["number"], t["datatype"])
        result.append({"name": t["name"], "address": label, "type": t["datatype"], "values": vals, "value": vals[-1], "flags": sorted(flags)})
    if args.json:
        print(json.dumps({"ip": args.ip, "tags": result, "skipped": parsed["skipped"]}, indent=2), file=out)
    elif args.csv:
        w = csv.writer(out, lineterminator="\n")
        w.writerow(["name", "address", "type", "value", "flags"])
        for r in result:
            w.writerow([r["name"], r["address"], r["type"], "" if r["value"] is None else r["value"], " ".join(r["flags"])])
    else:
        header = ["Name", "Address", "Type", "Value" if len(samples) == 1 else f"Value (last of {len(samples)})", "Flags"]
        print(table([[r["name"], r["address"], r["type"], fmt(r["value"], r["type"]), " ".join(r["flags"])] for r in result], header), file=out)
        for n, why in parsed["skipped"]:
            print(f"skipped {n}: {why}", file=out)
        print(f"\n{len(result)} tags read, {len(result) - flagged} fine, {flagged} flagged.", file=out)
    return 2 if flagged else 0


def parse_start(text, protocol):
    """D612 / R10 (Mitsubishi), M20 / DB1.4 (Siemens) -> (area, db, number)."""
    t = text.strip().upper().replace("%", "")
    m = re.match(r"^DB(\d+)\.(?:DB[WD]?)?(\d+)$", t)
    if protocol == "s7" and m:
        return "DB", int(m.group(1)), int(m.group(2))
    m = re.match(r"^([A-Z]{1,2})(\d+)$", t)
    if m:
        return m.group(1), None, int(m.group(2))
    raise ValueError(f"cannot read the start address '{text}' (examples: D612, M20, DB1.4)")


def cmd_raw(args, out, driver_factory):
    protocol = MAKES[args.make]
    try:
        area, db, number = parse_start(args.start, protocol)
    except ValueError as e:
        print(str(e), file=out)
        return 1
    transport = port = None
    if protocol == "mc":
        transport, port = (args.transport, args.port) if args.transport != "auto" and args.port else choose_slmp(args, out)
        if transport is None:
            return 1
    step = 2 if protocol == "s7" else 1
    region = cm.Region(area if db is None else f"DB{db}", area, number, number + (args.count - 1) * step, step=step, db=db)
    try:
        words = DriverReader(protocol, build_interface(args, protocol, transport, port), driver_factory).read_region(region)
    except Exception as e:      # noqa: BLE001
        print(f"Could not read the PLC: {type(e).__name__}: {e}".rstrip(": "), file=out)
        return 1
    rows = []
    for a in sorted(words):
        w = words[a]
        signed = w - 65536 if w >= 32768 else w
        pair = ""
        if a + step in words:
            for order in (("ABCD", "CDAB") if protocol == "s7" else ("CDAB", "ABCD")):
                v = [x for d, o, _, x in cm.interpretations(w, words[a + step], (order,)) if d == "float32" and o == order]
                if v and (v[0] == 0 or 1e-6 < abs(v[0]) < 1e9):
                    pair += f"{order}={v[0]:.4g}  "
        rows.append([f"{area}{a}" if db is None else f"DB{db}.{a}", w, signed, f"0x{w:04X}", pair.strip()])
    print(table(rows, ["Address", "Unsigned", "Signed", "Hex", "As float32 with the next word (plausible only)"]), file=out)
    return 0


# --------------------------------------------------------------------------- entry

def make_parser():
    p = argparse.ArgumentParser(prog="plcread", description="Connect to a PLC, check it answers, read tags. Read-only.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--ip", required=True, help="the PLC's IP address")
        sp.add_argument("--make", choices=sorted(MAKES), default="mitsubishi")
        sp.add_argument("--port", type=int, help="port (Mitsubishi: found automatically if not given)")
        sp.add_argument("--transport", choices=("auto", "udp", "tcp"), default="auto", help="Mitsubishi SLMP transport")
        sp.add_argument("--local-ip", help="this computer's IP on the PLC network, if it has more than one network card")
        sp.add_argument("--timeout", type=float, default=1.0, help="seconds to wait for each answer")
        sp.add_argument("--rack", type=int, default=0); sp.add_argument("--slot", type=int, default=1)
        sp.add_argument("--unit-id", type=int, default=1)
        sp.add_argument("--byte-order", choices=cm.BYTE_ORDERS_32, help="32-bit order (default CDAB Mitsubishi, ABCD Siemens)")

    pr = sub.add_parser("probe", help="find out which port and transport answer SLMP")
    pr.add_argument("ip"); pr.add_argument("--ports", help="comma separated, default 5000-5007 and 5010")
    pr.add_argument("--local-ip"); pr.add_argument("--timeout", type=float, default=1.0)
    pr.add_argument("--all", action="store_true", help="also list the ports that did not answer")
    rd = sub.add_parser("read", help="read every tag in an exported tag list")
    common(rd)
    rd.add_argument("--tags", required=True, help="GX Works3 label export (Mitsubishi) or TIA Portal tag table .xlsx (Siemens)")
    rd.add_argument("--samples", type=int, default=1, help="read this many times")
    rd.add_argument("--interval", type=float, default=2.0, help="seconds between samples")
    g = rd.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true"); g.add_argument("--csv", action="store_true")
    rw = sub.add_parser("raw", help="show raw registers from an address")
    common(rw)
    rw.add_argument("--start", required=True, help="e.g. D612 (Mitsubishi), M20 or DB1.4 (Siemens)")
    rw.add_argument("--count", type=int, default=20, help="how many 16-bit words")
    return p


def main(argv=None, out=None, driver_factory=None):
    out = out or sys.stdout
    args = make_parser().parse_args(argv)
    handler = {"probe": cmd_probe, "read": cmd_read, "raw": cmd_raw}[args.cmd]
    return handler(args, out, driver_factory)


if __name__ == "__main__":
    sys.exit(main())

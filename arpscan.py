"""arpscan.py -- who is on the network, by MAC address: the IP sweep's ping answers joined with this PC's ARP table and a vendor name from the IEEE list.

Why ARP: a PLC that ignores ping still answers ARP (the PC asks "who has 192.168.0.51?" before it sends anything), so after a sweep the ARP table also
holds devices a ping alone would miss. No administrator rights, no driver (Npcap) and nothing sent beyond ordinary pings; it only READS the table.
Limits: only the PC's own network segment is visible, and a device that was not asked (outside the swept range) is not listed.

Pure functions (parsing, joining, lookup) plus `read_arp_table`, which runs `arp` and is replaceable in tests. The vendor list is NOT built in: the IEEE
registry (oui.csv) is large and a wrong built-in entry would be worse than none. Put the file from the IEEE Registration Authority next to the workspace.
"""
import csv
import os
import re
import subprocess
import sys

_WIN_ROW = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,3}){3})\s+([0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})\s+(\w+)", re.M)
_NIX_ROW = re.compile(r"\((\d{1,3}(?:\.\d{1,3}){3})\)\s+at\s+([0-9a-fA-F]{1,2}(?::[0-9a-fA-F]{1,2}){5})")
_IP_NEIGH = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})\s+dev\s+\S+\s+lladdr\s+([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})\s+(\w+)", re.M)

# What a vendor name often means for the next step (a hint to try, never a fact). Matched on words in the IEEE organisation name.
VENDOR_HINTS = [
    (("siemens",), "Siemens: S7 PLCs listen on port 102 (check PUT/GET and rack/slot)"),
    (("rockwell", "allen-bradley", "allen bradley"), "Rockwell / Allen-Bradley: EtherNet/IP on port 44818"),
    (("omron",), "Omron: FINS (port 9600) or EtherNet/IP (44818)"),
    (("mitsubishi",), "Mitsubishi: MC/SLMP, often port 5006-5010 (set in the PLC's Ethernet parameters)"),
    (("schneider", "modicon", "telemecanique"), "Schneider: Modbus TCP on port 502"),
    (("wago",), "WAGO: Modbus TCP on port 502"),
    (("delta electronics", "delta networks"), "Delta: Modbus TCP on port 502"),
]


def _norm_mac(text):
    return ":".join(part.zfill(2) for part in re.split(r"[-:]", text.lower()))


def _usable(ip, mac):
    """Drop broadcast, multicast and the half-empty entries: they are not devices."""
    first = int(ip.split(".")[0])
    if mac == "ff:ff:ff:ff:ff:ff" or mac == "00:00:00:00:00:00":
        return False
    if first >= 224 or int(mac.split(":")[0], 16) & 1:          # multicast IP range, or a multicast MAC (the low bit of the first byte)
        return False
    return not ip.endswith(".255")


def parse_arp_table(text, platform=None):
    """[{ip, mac}] from `arp -a` (Windows), `arp -an` (macOS/Linux) or `ip neigh` output; unusable entries are dropped."""
    found, seen = [], set()
    for ip, mac in [(m.group(1), _norm_mac(m.group(2))) for m in _WIN_ROW.finditer(text) if m.group(3).lower() != "invalid"] + \
                   [(m.group(1), _norm_mac(m.group(2))) for m in _NIX_ROW.finditer(text)] + \
                   [(m.group(1), _norm_mac(m.group(2))) for m in _IP_NEIGH.finditer(text) if m.group(3).upper() not in ("FAILED", "INCOMPLETE")]:
        if ip not in seen and _usable(ip, mac):
            seen.add(ip)
            found.append({"ip": ip, "mac": mac})
    return found


def read_arp_table(platform=None, runner=None, hidden_kwargs=None):
    """This PC's ARP table, [] if it cannot be read. `runner` replaces subprocess.run in tests; `hidden_kwargs` keeps the child window hidden on Windows."""
    platform = platform or sys.platform
    cmd = ["arp", "-a"] if platform.startswith("win") else ["arp", "-an"]
    try:
        out = (runner or subprocess.run)(cmd, capture_output=True, text=True, timeout=10, **(hidden_kwargs or {}))
        return parse_arp_table(out.stdout or "")
    except Exception:
        return []


def load_oui(path):
    """{ 'AABBCC': 'Organisation' } from the IEEE oui.csv (Registry,Assignment,Organization Name,Organization Address); {} if there is no such file."""
    table = {}
    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as f:
            for row in csv.reader(f):
                if len(row) >= 3 and re.fullmatch(r"[0-9A-Fa-f]{6}", row[1].strip()):
                    table[row[1].strip().upper()] = row[2].strip()
    except OSError:
        return {}
    return table


def vendor_of(mac, oui):
    """The registered organisation of a MAC address, or '' (unknown, or no list loaded). Locally administered MACs (a virtual machine, a randomised phone) have none."""
    if not oui:
        return ""
    if int(mac.split(":")[0], 16) & 2:
        return ""
    return oui.get("".join(mac.split(":")[:3]).upper(), "")


def hint_for_vendor(org):
    low = (org or "").lower()
    for words, hint in VENDOR_HINTS:
        if any(w in low for w in words):
            return hint
    return ""


def merge(pinged, arp, oui, prefix):
    """Rows for the table: every swept address that answered a ping, plus addresses in the same /24 that are only in the ARP table.
    pinged: {ip: True/False}; prefix: '192.168.0.'. Each row: ip, status ('Answers ping' / 'ARP only: does not answer ping'), mac, vendor, hint."""
    by_ip = {e["ip"]: e["mac"] for e in arp}
    rows = []
    for ip in sorted(set(ip for ip, ok in pinged.items() if ok) | {ip for ip in by_ip if ip.startswith(prefix)}, key=lambda s: tuple(int(p) for p in s.split("."))):
        mac = by_ip.get(ip, "")
        org = vendor_of(mac, oui) if mac else ""
        rows.append({"ip": ip, "status": "Answers ping" if pinged.get(ip) else "ARP only: does not answer ping", "mac": mac,
                     "vendor": org or ("(no vendor list)" if mac and not oui else ("(unknown)" if mac else "")), "hint": hint_for_vendor(org)})
    return rows


def default_oui_paths(workspace_dir=None):
    here = os.path.dirname(os.path.abspath(__file__))
    return [p for p in (os.path.join(workspace_dir, "oui.csv") if workspace_dir else None, os.path.join(here, "oui.csv")) if p]

"""Capture mode: a small Wireshark for ONE PLC. It records the IP packets between this PC and the PLC's address, decodes the common
industrial protocols (Mitsubishi MC / SLMP, Siemens S7, Modbus TCP) and says in words why a read failed.

READ-ONLY and passive: it listens, it never sends anything and never changes a setting. On Windows it needs the program to run as
administrator (a raw socket, no Npcap or other driver); a normal user gets a clear message. Packets can be saved as a .pcap that
Wireshark opens (link type RAW IP). Parsing and analysis work on bytes and are tested without a network; only `Capture` touches a socket.
Limit: Windows may not show every OUTGOING packet on every version; the analysis says "no request seen" in that case, not "no answer".
"""
import socket
import struct
import sys
import threading
import time

PROTO_ICMP, PROTO_TCP, PROTO_UDP = 1, 6, 17
TCP_FLAGS = ((0x01, "FIN"), (0x02, "SYN"), (0x04, "RST"), (0x08, "PSH"), (0x10, "ACK"))


def parse_ipv4(data, ts=None):
    """A decoded packet dict, or None if the bytes are not an IPv4 packet."""
    if len(data) < 20 or data[0] >> 4 != 4:
        return None
    ihl = (data[0] & 0x0F) * 4
    total = struct.unpack("!H", data[2:4])[0]
    proto = data[9]
    pkt = {"ts": ts if ts is not None else time.time(), "src": socket.inet_ntoa(data[12:16]), "dst": socket.inet_ntoa(data[16:20]),
           "proto": proto, "raw": bytes(data[:total] if 0 < total <= len(data) else data), "payload": b"", "sport": None, "dport": None,
           "flags": "", "icmp": None}
    body = data[ihl:total if 0 < total <= len(data) else len(data)]
    if proto == PROTO_TCP and len(body) >= 20:
        pkt["sport"], pkt["dport"] = struct.unpack("!HH", body[:4])
        off = (body[12] >> 4) * 4
        pkt["flags"] = "+".join(name for bit, name in TCP_FLAGS if body[13] & bit)
        pkt["payload"] = bytes(body[off:])
    elif proto == PROTO_UDP and len(body) >= 8:
        pkt["sport"], pkt["dport"] = struct.unpack("!HH", body[:4])
        pkt["payload"] = bytes(body[8:])
    elif proto == PROTO_ICMP and len(body) >= 2:
        pkt["icmp"] = (body[0], body[1])
        pkt["payload"] = bytes(body[8:])
    return pkt


# ---- protocol decoders: payload bytes -> short text ("" when not this protocol) --------------------------------------------------

def decode_slmp(p):
    if len(p) >= 15 and p[:2] == b"\x50\x00":
        cmd = struct.unpack("<H", p[11:13])[0] if len(p) >= 13 else 0
        return f"MC request, command 0x{cmd:04X}" + (" (batch read)" if cmd == 0x0401 else "")
    if len(p) >= 11 and p[:2] == b"\xD0\x00":
        end = struct.unpack("<H", p[9:11])[0]
        return "MC reply, OK" if end == 0 else f"MC reply, END CODE 0x{end:04X} (the PLC refused the request)"
    return ""


def decode_s7(p):
    if len(p) < 7 or p[0] != 3:
        return ""
    cotp = p[5]
    if p[4] and p[5] == 0xE0:
        return "S7 connection request (COTP)"
    if p[5] == 0xD0:
        return "S7 connection confirm (COTP): the PLC accepted the connection"
    if len(p) >= 17 and p[7] == 0x32:
        rosctr = p[8]
        if rosctr == 1:
            func = p[17] if len(p) > 17 else None
            return "S7 job" + {0xF0: " (setup communication)", 0x04: " (read)", 0x05: " (write)"}.get(func, "")
        if rosctr == 3 and len(p) >= 19:
            ecls, ecode = p[17], p[18]
            return "S7 ack, OK" if (ecls, ecode) == (0, 0) else f"S7 ack, ERROR class 0x{ecls:02X} code 0x{ecode:02X}"
    return f"S7/TPKT frame (COTP type 0x{cotp:02X})"


def decode_modbus(p):
    if len(p) >= 8 and p[2:4] == b"\x00\x00" and struct.unpack("!H", p[4:6])[0] == len(p) - 6:
        func = p[7]
        if func & 0x80:
            return f"Modbus exception reply, function {func & 0x7F}, code {p[8] if len(p) > 8 else '?'}"
        return f"Modbus, function {func}"
    return ""


def annotate(pkt):
    """Protocol name and a short text for a packet."""
    pl = pkt["payload"]
    if pkt["icmp"]:
        t, c = pkt["icmp"]
        if (t, c) == (3, 3):
            return "ICMP", "destination port unreachable (nothing listens on that UDP port)"
        if t == 3:
            return "ICMP", f"destination unreachable (code {c})"
        return "ICMP", {8: "echo request (ping)", 0: "echo reply"}.get(t, f"type {t}")
    if pkt["proto"] == PROTO_TCP and not pl:
        return "TCP", pkt["flags"]
    for name, fn in (("MC", decode_slmp), ("S7", decode_s7), ("Modbus", decode_modbus)):
        text = fn(pl)
        if text:
            return name, text
    return ("TCP" if pkt["proto"] == PROTO_TCP else "UDP" if pkt["proto"] == PROTO_UDP else str(pkt["proto"])), f"{len(pl)} bytes"


def line(pkt, local_ip, t0):
    name, text = annotate(pkt)
    arrow = "->" if pkt["src"] == local_ip else "<-"
    ports = f"{pkt['sport']}>{pkt['dport']}" if pkt["sport"] is not None else ""
    flags = f" [{pkt['flags']}]" if pkt["flags"] and name not in ("TCP",) else ""
    return f"{pkt['ts'] - t0:8.3f}  {arrow} {name:6} {ports:12}{flags} {text}"


# ---- analysis: what do the packets say about why a read failed? --------------------------------------------------------------------

def analyze(packets, local_ip, plc_ip):
    """Plain-words findings from the packets between local_ip and plc_ip. Returns a list of lines."""
    mine = [p for p in packets if p["src"] == local_ip and p["dst"] == plc_ip]
    theirs = [p for p in packets if p["src"] == plc_ip and p["dst"] == local_ip]
    out = []
    if not mine and not theirs:
        return ["No packet to or from the PLC was seen. Start the capture first, then press Read (or Start in ProtoProbe). If nothing shows even "
                "then, ProtoProbe is not sending (a wrong IP) or this PC's adapter was not the one captured."]
    if theirs and not mine:
        out.append("Packets from the PLC were seen but none sent by this PC: this Windows may not show outgoing packets in a capture. Do not read "
                   "'no request' as a fault.")
    syns = [p for p in mine if p["proto"] == PROTO_TCP and "SYN" in p["flags"] and "ACK" not in p["flags"]]
    synacks = [p for p in theirs if p["proto"] == PROTO_TCP and "SYN" in p["flags"] and "ACK" in p["flags"]]
    rsts = [p for p in theirs if p["proto"] == PROTO_TCP and "RST" in p["flags"]]
    for port in sorted({p["dport"] for p in syns}):
        opened = any(p["sport"] == port for p in synacks)
        reset = any(p["sport"] == port for p in rsts)
        if opened:
            out.append(f"TCP port {port}: the connection opened (the PLC accepted it).")
        elif reset:
            out.append(f"TCP port {port}: the PLC answered with a reset: it is reachable but nothing listens on that port over TCP (wrong port, or the "
                       "PLC uses UDP there).")
        else:
            out.append(f"TCP port {port}: connection attempts got NO answer at all: a firewall (on this PC, on the way, or the PLC) drops them, or "
                       "the IP/port is wrong, or the PLC's Ethernet setting is not active (Open settings not written / PLC not power-cycled).")
    udp_req = [p for p in mine if p["proto"] == PROTO_UDP]
    for port in sorted({p["dport"] for p in udp_req}):
        reply = any(p["proto"] == PROTO_UDP and p["sport"] == port for p in theirs)
        unreach = any(p["icmp"] == (3, 3) for p in theirs)
        if unreach:
            out.append(f"UDP port {port}: the PLC said 'port unreachable': nothing listens on that UDP port.")
        elif reply:
            out.append(f"UDP port {port}: the PLC replied.")
        else:
            out.append(f"UDP port {port}: requests were sent and NOTHING came back: UDP is not enabled on that port in the PLC, or a firewall drops the "
                       "reply (for UDP the reply returns to a different port of this PC, so a rule for the program may be needed).")
    for p in theirs:
        name, text = annotate(p)
        if "END CODE" in text or "ERROR" in text or "exception" in text:
            out.append(f"The PLC answered but refused: {text}.")
            break
    if any(annotate(p)[1] == "S7 connection request (COTP)" for p in mine) and not any("connection confirm" in annotate(p)[1] for p in theirs):
        out.append("S7: a connection request was sent but the PLC did not confirm it (wrong rack/slot, or the connection is blocked).")
    if any(annotate(p)[1].startswith("MC reply, OK") or annotate(p)[1] == "S7 ack, OK" or annotate(p)[1].startswith("Modbus, function") for p in theirs):
        out.append("The PLC answered a request correctly, so the network and the PLC settings are fine for that request.")
    return out or ["Packets were seen, but nothing that points to a cause: send the saved capture."]


# ---- pcap file (opens in Wireshark) ------------------------------------------------------------------------------------------------

def write_pcap(path, packets):
    with open(path, "wb") as f:
        f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 101))      # link type 101 = raw IP
        for p in packets:
            sec = int(p["ts"])
            f.write(struct.pack("<IIII", sec, int((p["ts"] - sec) * 1e6), len(p["raw"]), len(p["raw"])) + p["raw"])


# ---- the live capture (the only part that touches a socket) --------------------------------------------------------------------

def is_admin():
    if sys.platform.startswith("win"):
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    import os
    return os.geteuid() == 0


def local_ip_for(plc_ip):
    """The address of this PC's adapter that routes to the PLC."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((plc_ip, 9))
        return s.getsockname()[0]
    finally:
        s.close()


class Capture:
    """start() -> None or an error text in words; packets to/from plc_ip are appended to self.packets (and passed to on_packet)."""

    def __init__(self, plc_ip, on_packet=None):
        self.plc_ip, self.on_packet = plc_ip, on_packet
        self.packets, self.local_ip, self.t0 = [], None, time.time()
        self._sock, self._stop, self._thread = None, threading.Event(), None

    def start(self):
        if not is_admin():
            return ("Capture needs the program to run as administrator (right-click ProtoProbe.exe > Run as administrator). "
                    "Without that, use the Connection doctor.")
        try:
            self.local_ip = local_ip_for(self.plc_ip)
            if sys.platform.startswith("win"):
                s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_IP)
                s.bind((self.local_ip, 0))
                s.setsockopt(socket.IPPROTO_IP, socket.IP_HDRINCL, 1)
                s.ioctl(socket.SIO_RCVALL, socket.RCVALL_ON)
                self._ethernet = False
            else:                                                       # Linux (testing, or a Linux laptop): needs root
                s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(3))
                self._ethernet = True
            s.settimeout(0.5)
        except Exception as exc:
            return f"Capture could not start: {exc}"
        self._sock = s
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return None

    def _loop(self):
        while not self._stop.is_set():
            try:
                data = self._sock.recv(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            if self._ethernet:
                if len(data) < 14 or data[12:14] != b"\x08\x00":
                    continue
                data = data[14:]
            pkt = parse_ipv4(data)
            if pkt and self.plc_ip in (pkt["src"], pkt["dst"]):
                self.packets.append(pkt)
                if self.on_packet:
                    self.on_packet(pkt)

    def stop(self):
        self._stop.set()
        try:
            if self._sock is not None:
                if sys.platform.startswith("win"):
                    self._sock.ioctl(socket.SIO_RCVALL, socket.RCVALL_OFF)
                self._sock.close()
        except Exception:
            pass

"""
protocol_detect.py -- "which industrial protocol does this IP really speak?"

An open TCP port proves little (port 102 or 502 can be anything) and a UDP port cannot be proven open by connecting. So each
detector here sends ONE small, READ-ONLY request that only that protocol answers, and checks the reply:

    S7comm (Siemens)      TCP 102    the ISO-on-TCP connection request   -> a connection confirm
    Modbus TCP            TCP 502    read 1 holding register (unit 1)    -> a reply to it (or a Modbus exception: still Modbus)
    OPC UA                TCP 4840   the binary "Hello" message           -> "Acknowledge" (or an OPC UA error)
    BACnet/IP             UDP 47808  a "Who-Is" to that address           -> an "I-Am"
    SLMP / MC (Mitsubishi) UDP + TCP, ports 5000-5010   a 3E read of D0   -> a 3E reply (slmp_probe.py)

Each result: {"protocol", "transport", "port", "status", "text"} with status
    answers   the device speaks this protocol on this port
    open      a TCP connection was accepted but the reply was not this protocol (something else listens there)
    none      nothing came back (closed, filtered, switched off, or the wrong IP)

Modbus RTU and BACnet MS/TP are serial (RS-485): there is no IP to scan, so they are not detected here.
Nothing is ever written to a device.
"""
import concurrent.futures
import socket
import struct

import slmp_probe

DEFAULT_PORTS = {"s7": 102, "modbus": 502, "opcua": 4840, "bacnet": 47808}

# TPKT + COTP connection request from TSAP 01.00 to the CPU's TSAP. The destination depends on the slot: an S7-1200/1500 only
# accepts 01.01 (rack 0 slot 1) and closes the connection for anything else; an S7-300 is usually slot 2, an S7-400 slot 2 or 3.
S7_SLOTS = (1, 2, 0, 3)


def s7_connection_request(slot, rack=0):
    return bytes.fromhex("0300001611e00000000100c0010ac1020100c20201") + bytes([(rack << 5) | slot])
# MBAP (transaction 1, protocol 0, length 6) + unit 1, function 3, address 0, quantity 1
MODBUS_READ = bytes.fromhex("000100000006010300000001")
# BACnet: BVLC original-unicast + NPDU (version 1, no routing) + unconfirmed Who-Is
BACNET_WHOIS = bytes.fromhex("810a000801001008")


def _r(protocol, transport, port, status, text=""):
    return {"protocol": protocol, "transport": transport, "port": port, "status": status, "text": text}


def _tcp_exchange(ip, port, payload, timeout):
    """('refused'|'timeout'|'error', text) or ('data', bytes) or ('silent', b'') when it connected but sent nothing."""
    sock = None
    try:
        sock = socket.create_connection((ip, port), timeout=timeout)
        sock.settimeout(timeout)
        sock.sendall(payload)
        try:
            data = sock.recv(4096)
            return ("data", data) if data else ("closed", b"")      # b"" = the peer closed the connection without replying
        except socket.timeout:
            return "silent", b""
        except ConnectionResetError:
            return "closed", b""
    except ConnectionRefusedError:
        return "refused", ""
    except socket.timeout:
        return "timeout", ""
    except OSError as e:
        return "error", f"{type(e).__name__}: {e}"
    finally:
        if sock is not None:
            sock.close()


# --------------------------------------------------------------------------- the detectors

def detect_s7(ip, port=102, timeout=1.0):
    """Tries the usual slots (1, 2, 0, 3): a PLC that answers any of them speaks S7, and the answer says which slot it wants."""
    seen_open = False
    for slot in S7_SLOTS:
        kind, data = _tcp_exchange(ip, port, s7_connection_request(slot), timeout)
        if kind == "data" and len(data) >= 6 and data[0] == 0x03 and data[5] == 0xD0:
            return _r("S7comm (Siemens)", "tcp", port, "answers", f"the PLC accepted an S7 connection (rack 0, slot {slot})")
        if kind in ("data", "silent", "closed"):
            seen_open = True
        elif not seen_open:
            return _r("S7comm (Siemens)", "tcp", port, "none", kind if kind != "error" else data)
    return _r("S7comm (Siemens)", "tcp", port, "open",
              "the port is open but no S7 connection was accepted for slot 0, 1, 2 or 3 (another service, or an S7 CPU that refuses it)")


def detect_modbus_tcp(ip, port=502, timeout=1.0):
    kind, data = _tcp_exchange(ip, port, MODBUS_READ, timeout)
    if kind == "data" and len(data) >= 9 and data[0:2] == b"\x00\x01" and data[2:4] == b"\x00\x00" and data[7] in (0x03, 0x83):
        text = "answered a read of holding register 0" if data[7] == 0x03 else f"answered with Modbus exception {data[8]} (it speaks Modbus; that address or unit is refused)"
        return _r("Modbus TCP", "tcp", port, "answers", text)
    if kind in ("data", "silent"):
        return _r("Modbus TCP", "tcp", port, "open", "the port is open but did not answer like Modbus")
    return _r("Modbus TCP", "tcp", port, "none", kind if kind != "error" else data)


def _opcua_hello(ip, port):
    url = f"opc.tcp://{ip}:{port}".encode()
    body = struct.pack("<IIIII", 0, 65536, 65536, 0, 0) + struct.pack("<i", len(url)) + url
    return b"HELF" + struct.pack("<I", 8 + len(body)) + body


def detect_opcua(ip, port=4840, timeout=1.0):
    kind, data = _tcp_exchange(ip, port, _opcua_hello(ip, port), timeout)
    if kind == "data" and data[:4] in (b"ACKF", b"ERRF"):
        return _r("OPC UA", "tcp", port, "answers", "the server acknowledged an OPC UA hello" if data[:4] == b"ACKF"
                  else "the server answered the hello with an OPC UA error (it speaks OPC UA)")
    if kind in ("data", "silent"):
        return _r("OPC UA", "tcp", port, "open", "the port is open but did not answer like OPC UA")
    return _r("OPC UA", "tcp", port, "none", kind if kind != "error" else data)


def _is_bacnet_iam(data):
    """BVLC (4 bytes) + NPDU (optionally with destination / source routing) + APDU 0x10 0x00 = I-Am."""
    if len(data) < 8 or data[0] != 0x81:
        return False
    i = 4
    if data[i] != 0x01:
        return False
    ctrl = data[i + 1]
    i += 2
    if ctrl & 0x20:                       # destination present: DNET(2) DLEN(1) DADR(DLEN)
        if len(data) < i + 3:
            return False
        i += 3 + data[i + 2]
    if ctrl & 0x08:                       # source present: SNET(2) SLEN(1) SADR(SLEN)
        if len(data) < i + 3:
            return False
        i += 3 + data[i + 2]
    if ctrl & 0x20:                       # hop count
        i += 1
    return len(data) >= i + 2 and data[i] == 0x10 and data[i + 1] == 0x00


def detect_bacnet_ip(ip, port=47808, timeout=1.0, bind_port=None):
    """bind_port: the local UDP port to listen on (default: the BACnet port itself, which many devices reply to; 0 = any)."""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            sock.bind(("", port if bind_port is None else bind_port))   # many devices answer to port 47808, not to the sender's port
        except OSError:
            pass                          # in use (another BACnet program): an ordinary port still gets the usual replies
        sock.sendto(BACNET_WHOIS, (ip, port))
        for _ in range(8):                # other BACnet devices may chatter on the same port: look at a few packets
            data, addr = sock.recvfrom(2048)
            if addr[0] == ip and _is_bacnet_iam(data):
                return _r("BACnet/IP", "udp", port, "answers", "the device answered a Who-Is with an I-Am")
        return _r("BACnet/IP", "udp", port, "none", "")
    except socket.timeout:
        return _r("BACnet/IP", "udp", port, "none", "")
    except OSError as e:
        return _r("BACnet/IP", "udp", port, "none", f"{type(e).__name__}: {e}")
    finally:
        if sock is not None:
            sock.close()


def detect_slmp(ip, ports=slmp_probe.DEFAULT_PORTS, timeout=1.0, local_ip=None):
    """One result per SLMP answer (UDP and/or TCP, per port); a single 'none' result when nothing answered."""
    res = slmp_probe.probe(ip, ports=ports, local_ip=local_ip, timeout=timeout)
    out = []
    for r in res:
        if r["status"] in ("ok", "answered"):
            out.append(_r("SLMP / MC protocol (Mitsubishi)", r["transport"], r["port"], "answers",
                          "the PLC answered a read of D0" if r["status"] == "ok" else r["text"]))
    return out or [_r("SLMP / MC protocol (Mitsubishi)", "udp+tcp", "/".join(str(p) for p in (ports[0], ports[-1])), "none", "")]


# --------------------------------------------------------------------------- all of them

def detect_all(ip, timeout=1.0, ports=None, slmp_ports=slmp_probe.DEFAULT_PORTS, local_ip=None, progress=None):
    """Runs every detector in parallel. Returns the results that say something (answers / open), answers first.
    ports overrides the default ports: {"s7": 102, "modbus": 502, "opcua": 4840, "bacnet": 47808}."""
    p = dict(DEFAULT_PORTS)
    p.update(ports or {})
    jobs = [
        lambda: [detect_s7(ip, p["s7"], timeout)],
        lambda: [detect_modbus_tcp(ip, p["modbus"], timeout)],
        lambda: [detect_opcua(ip, p["opcua"], timeout)],
        lambda: [detect_bacnet_ip(ip, p["bacnet"], timeout)],
        lambda: detect_slmp(ip, slmp_ports, timeout, local_ip),
    ]
    out = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        for fut in concurrent.futures.as_completed([pool.submit(j) for j in jobs]):
            for r in fut.result():
                out.append(r)
                if progress:
                    progress(r)
    keep = [r for r in out if r["status"] in ("answers", "open")]
    return sorted(keep, key=lambda r: (r["status"] != "answers", r["protocol"], r["transport"]))


def summary(results):
    """A plain-language line for the screen."""
    hits = [r for r in results if r["status"] == "answers"]
    if not hits:
        return ("No protocol answered. Check the IP and the cable. Modbus RTU and BACnet MS/TP are serial (RS-485): "
                "they cannot be found over the network.")
    return "Speaks: " + "; ".join(f"{r['protocol']} ({r['transport'].upper()} {r['port']})" for r in hits)

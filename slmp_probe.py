"""
slmp_probe.py -- "is this PLC really answering SLMP, and on which port and transport?"

An open port proves nothing, and a UDP port cannot be proven open by connecting. So this sends one real, READ-ONLY
SLMP/MC 3E binary batch read (1 word of D0, the same frame the driver and the field scripts use) to each candidate
port over UDP and over TCP, and reports what came back:

    ok        a valid 3E reply with end code 0  (the PLC answered and the address is readable)
    answered  a valid 3E reply with a non-zero end code (the PLC speaks SLMP here; the request was refused, e.g. D0 not allowed)
    timeout   nothing came back (closed, filtered, SLMP not switched on for this port/transport, or the wrong IP)
    refused   TCP only: the connection was refused
    other     anything else (the text says what)

It never writes to the PLC. local_ip binds the sending socket to one network adapter (a laptop on two networks).
"""
import concurrent.futures
import socket
import struct

DEFAULT_PORTS = (5000, 5001, 5002, 5003, 5004, 5005, 5006, 5007, 5010)


def read_frame(device_code=0xA8, start=0, points=1):
    """3E binary frame, command 0401 (batch read, word units): identical to MCProtocolDriver._build_read_frame."""
    return (b"\x50\x00" + b"\x00\xFF\xFF\x03\x00" + struct.pack("<H", 12) + struct.pack("<H", 16)
            + b"\x01\x04\x00\x00" + start.to_bytes(3, "little") + bytes([device_code]) + struct.pack("<H", points))


def parse_reply(data):
    """-> (valid, end_code). A valid 3E reply starts with D0 00 and carries the end code at bytes 9-10."""
    if len(data) >= 11 and data[0:2] == b"\xD0\x00":
        return True, struct.unpack("<H", data[9:11])[0]
    return False, None


def _result(transport, port, status, end_code=None, text="", reply=b""):
    return {"transport": transport, "port": port, "status": status, "end_code": end_code, "text": text, "reply_hex": reply.hex()}


def _classify(transport, port, reply):
    valid, end = parse_reply(reply)
    if not valid:
        return _result(transport, port, "other", None, "a reply that is not an SLMP 3E frame", reply)
    return _result(transport, port, "ok" if end == 0 else "answered", end,
                   "" if end == 0 else f"the PLC answered with end code 0x{end:04X}", reply)


def probe_one(ip, port, transport, local_ip=None, timeout=1.0, frame=None):
    frame = frame or read_frame()
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM if transport == "udp" else socket.SOCK_STREAM)
        if local_ip:
            sock.bind((local_ip, 0))
        sock.settimeout(timeout)
        if transport == "udp":
            sock.sendto(frame, (ip, port))
            return _classify("udp", port, sock.recv(1024))
        sock.connect((ip, port))
        sock.sendall(frame)
        return _classify("tcp", port, sock.recv(1024))
    except socket.timeout:
        return _result(transport, port, "timeout")
    except ConnectionRefusedError:
        return _result(transport, port, "refused")
    except OSError as e:                      # includes the ICMP "port unreachable" a UDP socket reports as ConnectionReset
        name = type(e).__name__
        return _result(transport, port, "refused" if "Refused" in name or "Reset" in name else "other", None, f"{name}: {e}")
    finally:
        if sock is not None:
            sock.close()


def probe(ip, ports=DEFAULT_PORTS, local_ip=None, timeout=1.0, transports=("udp", "tcp"), progress=None):
    """Every port over every transport, in parallel. Returns the results sorted by port, udp before tcp."""
    jobs = [(p, t) for p in ports for t in transports]
    out = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(probe_one, ip, p, t, local_ip, timeout) for p, t in jobs]
        for f in concurrent.futures.as_completed(futs):
            r = f.result()
            out.append(r)
            if progress:
                progress(r)
    return sorted(out, key=lambda r: (r["port"], r["transport"] != "udp"))


def best(results):
    """The first transport/port that really read a word, else the first that at least answered SLMP, else None."""
    for wanted in ("ok", "answered"):
        for r in results:
            if r["status"] == wanted:
                return r
    return None


def describe(results):
    """A short, plain-language summary for the screen."""
    hit = [r for r in results if r["status"] in ("ok", "answered")]
    if not hit:
        return ("No SLMP answer on any port, over UDP or TCP. Check the IP and the cable, that SLMP is switched on in the PLC's "
                "Ethernet settings, and (on a laptop with two network cards) the adapter IP.")
    lines = [f"{r['transport'].upper()} {r['port']}: " + ("PLC answered and D0 is readable" if r["status"] == "ok" else r["text"]) for r in hit]
    b = best(results)
    lines.append(f"Use {b['transport'].upper()} port {b['port']}.")
    return "\n".join(lines)

"""A ModScan-style Modbus TCP master for testing a device on site: read coils / discrete inputs / holding / input registers, see every
value in decimal, hex, binary and as 32-bit numbers, see the raw request and response bytes, the PLC's exception in words, the response
time and counters, and find which unit IDs answer.

READ ONLY: only function codes 1 to 4 exist here. There is no function that writes (standing rule 5). Modbus TCP only (RTU over serial is
not built). Written from the Modbus specification; OpenModScan (MIT, C++/Qt) was used as a feature reference only, no code was copied.
"""
import socket
import struct
import time

FUNCTIONS = {1: "Coils (01)", 2: "Discrete inputs (02)", 3: "Holding registers (03)", 4: "Input registers (04)"}
MAX_COUNT = {1: 2000, 2: 2000, 3: 125, 4: 125}
EXCEPTIONS = {
    1: "Illegal function: the device does not support this function code.",
    2: "Illegal data address: the device has no register at that address (try the other address base 0/1, or a smaller count).",
    3: "Illegal data value: the count or a value is not accepted.",
    4: "Device failure: the device could not do it (it is faulty or busy).",
    5: "Acknowledge: the device accepted but is still working on it.",
    6: "Device busy: try again in a moment.",
    8: "Memory parity error in the device.",
    10: "Gateway path unavailable: the gateway cannot reach the device behind it.",
    11: "Gateway target did not answer: the device behind the gateway is off or the unit ID is wrong.",
}
WORD_ORDERS = {"ABCD": "ABCD (big-endian)", "CDAB": "CDAB (words swapped)", "BADC": "BADC (bytes swapped)", "DCBA": "DCBA (little-endian)"}


class ModbusError(Exception):
    """A problem in words: the PLC's exception, a timeout, a refused connection, a damaged answer."""


def exception_text(code):
    return EXCEPTIONS.get(code, f"Exception code {code}.")


def build_request(tid, unit, fn, start, count):
    """The Modbus TCP frame for a READ (functions 1 to 4 only)."""
    if fn not in FUNCTIONS:
        raise ValueError("only the read functions 1 to 4 exist in this tool")
    if not 0 <= start <= 65535 or not 1 <= count <= MAX_COUNT[fn] or not 0 <= unit <= 255:
        raise ValueError(f"address 0-65535, unit 0-255 and count 1-{MAX_COUNT[fn]} for function {fn}")
    pdu = struct.pack(">BHH", fn, start, count)
    return struct.pack(">HHHB", tid & 0xFFFF, 0, len(pdu) + 1, unit) + pdu


def parse_response(frame, fn, count):
    """-> list of values: bits (0/1) for functions 1 and 2, unsigned 16-bit words for 3 and 4. Raises ModbusError in words."""
    if len(frame) < 9:
        raise ModbusError(f"the answer is too short ({len(frame)} bytes): not a Modbus TCP answer")
    rfn = frame[7]
    if rfn == fn | 0x80:
        raise ModbusError(f"the device answered with an exception: {exception_text(frame[8])}")
    if rfn != fn:
        raise ModbusError(f"the answer is for function {rfn}, not {fn}")
    n = frame[8]
    data = frame[9:9 + n]
    if len(data) != n:
        raise ModbusError("the answer is cut short")
    if fn in (1, 2):
        return [(data[i // 8] >> (i % 8)) & 1 for i in range(count) if i // 8 < len(data)]
    return list(struct.unpack(">" + "H" * (n // 2), data[:n - n % 2]))


class Stats:
    def __init__(self):
        self.sent = self.ok = self.exceptions = self.timeouts = self.errors = 0
        self.last_ms = None

    def line(self):
        return (f"sent {self.sent}   ok {self.ok}   exceptions {self.exceptions}   timeouts {self.timeouts}   other errors {self.errors}"
                + (f"   last answer {self.last_ms:.0f} ms" if self.last_ms is not None else ""))


def read(host, port, unit, fn, start, count, timeout=2.0, stats=None, tid=1):
    """One read. -> {"values", "request", "response", "ms"}; raises ModbusError in words. `stats` (a Stats) is updated."""
    req = build_request(tid, unit, fn, start, count)
    if stats:
        stats.sent += 1
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(req)
            head = _recv(s, 7)
            length = struct.unpack(">H", head[4:6])[0]
            body = _recv(s, max(length - 1, 0))
    except socket.timeout:
        if stats:
            stats.timeouts += 1
        raise ModbusError(f"no answer within {timeout:g} s: wrong IP or port, a firewall, or the unit ID is not served (some devices stay silent)")
    except ConnectionRefusedError:
        if stats:
            stats.errors += 1
        raise ModbusError("connection refused: nothing listens on that port (Modbus is usually 502)")
    except OSError as exc:
        if stats:
            stats.errors += 1
        raise ModbusError(f"network error: {exc}")
    ms = (time.perf_counter() - t0) * 1000
    frame = head + body
    if stats:
        stats.last_ms = ms
    try:
        values = parse_response(frame, fn, count)
    except ModbusError as exc:
        if stats:
            if "exception" in str(exc):
                stats.exceptions += 1
            else:
                stats.errors += 1
        exc.request, exc.response = req, frame
        raise
    if stats:
        stats.ok += 1
    return {"values": values, "request": req, "response": frame, "ms": ms}


def _recv(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ModbusError("the device closed the connection before it answered")
        buf += chunk
    return buf


def hexdump(b):
    return " ".join(f"{x:02X}" for x in b)


# --- showing the registers ---------------------------------------------------------------------------------------
def _pair(hi, lo, order):
    """Four bytes from two words in the given word/byte order, returned as big-endian bytes ready for struct."""
    a, b = hi >> 8, hi & 255
    c, d = lo >> 8, lo & 255
    return {"ABCD": bytes((a, b, c, d)), "CDAB": bytes((c, d, a, b)), "BADC": bytes((b, a, d, c)), "DCBA": bytes((d, c, b, a))}[order]


def describe_words(values, start, base=0, order="ABCD"):
    """One row per register: address (shown in the chosen base), unsigned, signed, hex, binary, and the 32-bit readings of this word with the next."""
    rows = []
    for i, w in enumerate(values):
        row = {"address": start + i + base, "unsigned": w, "signed": w - 65536 if w >= 32768 else w, "hex": f"0x{w:04X}", "binary": f"{w:016b}"}
        if i + 1 < len(values):
            raw = _pair(w, values[i + 1], order)
            row["uint32"] = struct.unpack(">I", raw)[0]
            row["int32"] = struct.unpack(">i", raw)[0]
            f = struct.unpack(">f", raw)[0]
            row["float32"] = f if f == f and abs(f) < 3.4e38 else None
        else:
            row["uint32"] = row["int32"] = row["float32"] = None
        rows.append(row)
    return rows


def describe_bits(values, start, base=0):
    return [{"address": start + i + base, "value": v} for i, v in enumerate(values)]


# --- which unit IDs answer ---------------------------------------------------------------------------------------
def sweep_units(host, port, units, fn=3, address=0, timeout=0.6, stop=lambda: False, progress=None):
    """Ask a one-register read of every unit ID. A device that answers with an EXCEPTION is alive too (it just has no such register).
    -> [(unit, "answers" | "exception: ...")]; silence is not listed."""
    found = []
    for n, u in enumerate(units):
        if stop():
            break
        try:
            read(host, port, u, fn, address, 1, timeout=timeout, tid=u + 1)
            found.append((u, "answers"))
        except ModbusError as exc:
            if "exception" in str(exc):
                found.append((u, str(exc)))
        if progress:
            progress(n + 1, len(units))
    return found

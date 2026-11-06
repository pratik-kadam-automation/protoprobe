"""SLMP over UDP and TCP against local simulators of the PLC. The request bytes are checked against the frame of the
field script that works on a real FX5U (an FX5U on the plant network), so a change to the framing cannot slip in."""
import asyncio
import os
import socket
import struct
import sys
import threading
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "edge-gateway"))
import slmp_probe as sp
from drivers.mc_driver import MCProtocolDriver, MCProtocolError

# the frame from the field script: D100.., 5 points
START, POINTS = 100, 5
FIELD_FRAME = (b"\x50\x00" b"\x00\xFF\xFF\x03\x00" + struct.pack("<H", 12) + struct.pack("<H", 16) + b"\x01\x04\x00\x00"
               + START.to_bytes(3, "little") + b"\xA8" + struct.pack("<H", POINTS))
# what the PLC sent back in the field test for that read: D100..D104 all zero
FIELD_REPLY_HEX = "d00000ffff03000c00000000000000000000000000"


def reply_for(memory, request, end_code=0):
    """A 3E binary reply to a batch read of word devices."""
    start = int.from_bytes(request[15:18], "little")
    n = int.from_bytes(request[19:21], "little")
    words = b"".join(struct.pack("<H", memory.get(start + i, 0)) for i in range(n)) if end_code == 0 else b""
    body = struct.pack("<H", end_code) + words
    return b"\xD0\x00\x00\xFF\xFF\x03\x00" + struct.pack("<H", len(body)) + body


class SimUdp(threading.Thread):
    def __init__(self, memory, end_code=0, silent=False, drop_first=False, junk=False):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0)); self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.memory, self.end_code, self.silent, self.drop_first, self.junk = memory, end_code, silent, drop_first, junk
        self.requests, self.stop = [], False

    def run(self):
        dropped = False
        while not self.stop:
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            self.requests.append(data)
            if self.silent or (self.drop_first and not dropped):
                dropped = dropped or self.drop_first
                continue
            self.sock.sendto(b"\x01\x02\x03" if self.junk else reply_for(self.memory, data, self.end_code), addr)

    def close(self):
        self.stop = True
        self.sock.close()


class SimTcp(threading.Thread):
    def __init__(self, memory):
        super().__init__(daemon=True)
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.bind(("127.0.0.1", 0)); self.srv.listen(2); self.srv.settimeout(0.2)
        self.port = self.srv.getsockname()[1]
        self.memory, self.stop = memory, False

    def run(self):
        while not self.stop:
            try:
                c, _ = self.srv.accept()
            except (socket.timeout, OSError):
                continue
            c.settimeout(1)
            try:
                while True:
                    req = c.recv(2048)
                    if not req:
                        break
                    c.sendall(reply_for(self.memory, req))
            except OSError:
                pass
            finally:
                c.close()

    def close(self):
        self.stop = True
        self.srv.close()


def float_words(value):
    """A float32 stored the way an FX5U does: two D registers, low word first (little-endian), i.e. byte order CDAB."""
    b = struct.pack("<f", value)
    return struct.unpack("<HH", b)


def read(iface, tags):
    async def run():
        d = MCProtocolDriver("t", {"interface": iface, "tags": tags})
        await d.connect()
        try:
            return await d.poll()
        finally:
            await d.disconnect()
    return asyncio.run(run())


def tag(name, n, dt="int16"):
    return {"name": name, "device_code": "D", "device_number": n, "data_type": dt, "multiplier": 1.0, "offset": 0.0, "mqtt_key": name}


class Frame(unittest.TestCase):
    def test_driver_and_probe_frames_equal_the_field_script(self):
        self.assertEqual(MCProtocolDriver._build_read_frame("D", START, POINTS), FIELD_FRAME)
        self.assertEqual(sp.read_frame(0xA8, START, POINTS), FIELD_FRAME)

    def test_the_real_plc_reply_from_the_field_test_parses(self):
        valid, end = sp.parse_reply(bytes.fromhex(FIELD_REPLY_HEX))
        self.assertTrue(valid); self.assertEqual(end, 0)
        self.assertEqual(sp.parse_reply(b"junk"), (False, None))


class Udp(unittest.TestCase):
    def setUp(self):
        mem = {612: 0, 618: 0}
        mem[668], mem[669] = float_words(1459.853)           # Primary_Act_Flow
        mem[676], mem[677] = float_words(30.375)
        mem[904], mem[905] = float_words(5.026)
        mem[1050] = (-7) & 0xFFFF
        self.plc = SimUdp(mem); self.plc.start()
        self.iface = {"ip": "127.0.0.1", "port": self.plc.port, "transport": "udp", "timeout_s": 0.4}

    def tearDown(self):
        self.plc.close()

    def test_floats_and_words_read_over_udp(self):
        got = read(self.iface, [tag("Primary_Act_Flow", 668, "float32_le"), tag("Primary_Inlet_Temp", 676, "float32_le"),
                                tag("Auto_Insertion_Count", 904, "float32_le"), tag("INT_data", 1050)])
        self.assertAlmostEqual(got["Primary_Act_Flow"], 1459.853, places=2)
        self.assertAlmostEqual(got["Primary_Inlet_Temp"], 30.375, places=3)
        self.assertAlmostEqual(got["Auto_Insertion_Count"], 5.026, places=3)
        self.assertEqual(got["INT_data"], -7)

    def test_the_request_on_the_wire_is_the_field_frame(self):
        read(self.iface, [tag("x", START)])
        self.plc.requests.clear()
        read(self.iface, [tag("a", 100)])
        self.assertEqual(self.plc.requests[0], MCProtocolDriver._build_read_frame("D", 100, 1))
        self.assertEqual(self.plc.requests[0][:15], FIELD_FRAME[:15])

    def test_a_lost_datagram_is_sent_again_once(self):
        self.plc.close()
        plc = SimUdp({100: 42}, drop_first=True); plc.start()
        try:
            got = read({"ip": "127.0.0.1", "port": plc.port, "transport": "udp", "timeout_s": 0.3, "retries": 1}, [tag("a", 100)])
            self.assertEqual(got["a"], 42); self.assertEqual(len(plc.requests), 2)
        finally:
            plc.close()

    def test_a_silent_plc_times_out_and_a_plc_error_is_an_error(self):
        silent = SimUdp({}, silent=True); silent.start()
        try:
            with self.assertRaises(asyncio.TimeoutError):
                read({"ip": "127.0.0.1", "port": silent.port, "transport": "udp", "timeout_s": 0.2, "retries": 0}, [tag("a", 1)])
        finally:
            silent.close()
        bad = SimUdp({}, end_code=0xC059); bad.start()
        try:
            self.assertEqual(read({"ip": "127.0.0.1", "port": bad.port, "transport": "udp", "timeout_s": 0.3}, [tag("a", 1)]), {},
                             "a refused address is skipped like any bad tag (end code error), it does not stop the others")
        finally:
            bad.close()

    def test_junk_is_not_taken_for_a_reply(self):
        j = SimUdp({}, junk=True); j.start()
        try:
            self.assertEqual(read({"ip": "127.0.0.1", "port": j.port, "transport": "udp", "timeout_s": 0.3}, [tag("a", 1)]), {})
        finally:
            j.close()

    def test_transport_must_be_tcp_or_udp(self):
        with self.assertRaises(ValueError):
            read({"ip": "127.0.0.1", "port": 1, "transport": "carrier-pigeon"}, [tag("a", 1)])


class TcpStillWorks(unittest.TestCase):
    def test_the_proven_tcp_path_is_unchanged(self):
        plc = SimTcp({100: 123}); plc.start()
        try:
            self.assertEqual(read({"ip": "127.0.0.1", "port": plc.port, "timeout_s": 1.0}, [tag("a", 100)])["a"], 123)       # no transport key = tcp
            self.assertEqual(read({"ip": "127.0.0.1", "port": plc.port, "transport": "tcp", "timeout_s": 1.0}, [tag("a", 100)])["a"], 123)
        finally:
            plc.close()


class Probe(unittest.TestCase):
    def test_finds_a_udp_only_plc_like_the_ccm(self):
        udp = SimUdp({0: 0}); udp.start()
        try:
            res = sp.probe("127.0.0.1", ports=(udp.port,), timeout=0.5)
            by = {(r["transport"], r["port"]): r["status"] for r in res}
            self.assertEqual(by[("udp", udp.port)], "ok")
            self.assertIn(by[("tcp", udp.port)], ("refused", "timeout"))
            b = sp.best(res)
            self.assertEqual((b["transport"], b["port"]), ("udp", udp.port))
            self.assertIn(f"UDP port {udp.port}", sp.describe(res))
        finally:
            udp.close()

    def test_a_plc_that_refuses_the_request_still_counts_as_answering(self):
        bad = SimUdp({}, end_code=0xC05C); bad.start()
        try:
            res = sp.probe("127.0.0.1", ports=(bad.port,), timeout=0.5, transports=("udp",))
            self.assertEqual(res[0]["status"], "answered"); self.assertEqual(res[0]["end_code"], 0xC05C)
        finally:
            bad.close()

    def test_nothing_listening_says_so_in_plain_words(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        res = sp.probe("127.0.0.1", ports=(port,), timeout=0.3)
        self.assertIsNone(sp.best(res)); self.assertIn("No SLMP answer", sp.describe(res))

    def test_probe_is_read_only(self):
        # one-word read of D0: the command bytes are 04 01 (batch read), never 14 01 (write)
        self.assertEqual(sp.read_frame()[11:13], b"\x01\x04")


if __name__ == "__main__":
    unittest.main()

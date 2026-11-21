import os
import socket
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import capture  # noqa: E402

ME, PLC = "192.168.3.93", "192.168.3.39"


def ip(src, dst, proto, body):
    hdr = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(body), 0, 0, 64, proto, 0, socket.inet_aton(src), socket.inet_aton(dst))
    return hdr + body


def tcp(src, dst, sport, dport, flags, payload=b""):
    bits = {"SYN": 2, "ACK": 0x10, "RST": 4, "FIN": 1, "PSH": 8}
    f = sum(bits[x] for x in flags.split("+")) if flags else 0
    return capture.parse_ipv4(ip(src, dst, 6, struct.pack("!HHIIBBHHH", sport, dport, 0, 0, 5 << 4, f, 8192, 0, 0) + payload), ts=1.0)


def udp(src, dst, sport, dport, payload=b""):
    return capture.parse_ipv4(ip(src, dst, 17, struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload), ts=1.0)


def icmp(src, dst, t, c):
    return capture.parse_ipv4(ip(src, dst, 1, bytes([t, c, 0, 0, 0, 0, 0, 0])), ts=1.0)


MC_REQ = bytes.fromhex("500000ffff03000c00100001040000000000a80100")
MC_OK = bytes.fromhex("d00000ffff03000400000000")
MC_ERR = bytes.fromhex("d00000ffff0300020059c0")


class Parse(unittest.TestCase):
    def test_tcp_fields(self):
        p = tcp(ME, PLC, 50000, 1388, "SYN")
        self.assertEqual((p["sport"], p["dport"], p["flags"]), (50000, 1388, "SYN"))

    def test_not_ipv4(self):
        self.assertIsNone(capture.parse_ipv4(b"\x60" + b"\x00" * 40))
        self.assertIsNone(capture.parse_ipv4(b"\x45"))

    def test_udp_payload(self):
        self.assertEqual(udp(ME, PLC, 1, 2, b"abc")["payload"], b"abc")


class Decode(unittest.TestCase):
    def test_mc(self):
        self.assertIn("batch read", capture.decode_slmp(MC_REQ))
        self.assertEqual(capture.decode_slmp(MC_OK), "MC reply, OK")
        self.assertIn("C059", capture.decode_slmp(MC_ERR))

    def test_s7(self):
        cr = bytes.fromhex("0300001611e00000000100c0010ac1020100c2020101")
        self.assertIn("connection request", capture.decode_s7(cr))
        cc = bytes.fromhex("0300001611d00000000100c0010ac1020100c2020101")
        self.assertIn("accepted", capture.decode_s7(cc))
        job = bytes.fromhex("0300001902f08032010000000100080000f0000001000101e0")
        self.assertIn("setup communication", capture.decode_s7(job))
        ack_err = bytes.fromhex("0300001502f08032030000000100020000" "8104")
        self.assertIn("ERROR", capture.decode_s7(ack_err))

    def test_modbus(self):
        self.assertEqual(capture.decode_modbus(bytes.fromhex("000100000006010300000002")), "Modbus, function 3")
        self.assertIn("exception", capture.decode_modbus(bytes.fromhex("00010000000301830" "2")))
        self.assertEqual(capture.decode_modbus(b"hello world"), "")

    def test_icmp_port_unreachable(self):
        self.assertIn("port unreachable", capture.annotate(icmp(PLC, ME, 3, 3))[1])


class Analyze(unittest.TestCase):
    def text(self, pkts):
        return " ".join(capture.analyze(pkts, ME, PLC))

    def test_syn_without_answer(self):
        self.assertIn("NO answer", self.text([tcp(ME, PLC, 50000, 1388, "SYN"), tcp(ME, PLC, 50000, 1388, "SYN")]))

    def test_syn_then_reset(self):
        t = self.text([tcp(ME, PLC, 50000, 1388, "SYN"), tcp(PLC, ME, 1388, 50000, "RST+ACK")])
        self.assertIn("reset", t)
        self.assertIn("nothing listens", t)

    def test_tcp_opened_and_mc_ok(self):
        t = self.text([tcp(ME, PLC, 50000, 1388, "SYN"), tcp(PLC, ME, 1388, 50000, "SYN+ACK"), tcp(ME, PLC, 50000, 1388, "PSH+ACK", MC_REQ),
                       tcp(PLC, ME, 1388, 50000, "PSH+ACK", MC_OK)])
        self.assertIn("accepted", t)
        self.assertIn("answered a request correctly", t)

    def test_udp_no_reply(self):
        self.assertIn("NOTHING came back", self.text([udp(ME, PLC, 60000, 1388, MC_REQ)]))

    def test_udp_reply(self):
        self.assertIn("the PLC replied", self.text([udp(ME, PLC, 60000, 1388, MC_REQ), udp(PLC, ME, 1388, 60000, MC_OK)]))

    def test_udp_unreachable(self):
        self.assertIn("unreachable", self.text([udp(ME, PLC, 60000, 1388, MC_REQ), icmp(PLC, ME, 3, 3)]))

    def test_end_code(self):
        self.assertIn("refused", self.text([udp(ME, PLC, 60000, 1388, MC_REQ), udp(PLC, ME, 1388, 60000, MC_ERR)]))

    def test_nothing_seen(self):
        self.assertIn("No packet", self.text([]))

    def test_only_plc_packets_do_not_blame(self):
        self.assertIn("outgoing", self.text([udp(PLC, ME, 1388, 60000, MC_OK)]))


class Files(unittest.TestCase):
    def test_pcap_header_and_records(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.pcap")
            pkts = [udp(ME, PLC, 1, 2, b"x"), udp(PLC, ME, 2, 1, b"y")]
            capture.write_pcap(path, pkts)
            with open(path, "rb") as f:
                data = f.read()
        self.assertEqual(data[:4], bytes.fromhex("d4c3b2a1"))
        self.assertEqual(struct.unpack("<I", data[20:24])[0], 101)
        self.assertEqual(len(data), 24 + sum(16 + len(p["raw"]) for p in pkts))

    def test_line_shows_direction(self):
        text = capture.line(udp(ME, PLC, 1, 1388, MC_REQ), ME, 1.0)
        self.assertIn("->", text)
        self.assertIn("MC", text)


class Admin(unittest.TestCase):
    def test_not_admin_message(self):
        with mock.patch.object(capture, "is_admin", return_value=False):
            msg = capture.Capture(PLC).start()
        self.assertIn("administrator", msg)


if __name__ == "__main__":
    unittest.main()

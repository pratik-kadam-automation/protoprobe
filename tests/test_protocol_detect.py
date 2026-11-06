"""Protocol detection against small local stand-ins for each device (no real hardware needed). A real device with the same
frames answers the same way: the request bytes are the ones the protocols define, checked here against fixed hex."""
import os
import socket
import sys
import threading
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "edge-gateway"))
import protocol_detect as pd

S7_CC = bytes.fromhex("0300001611d00001000100c0010ac1020100c2020101")


def s7_like(only_slot):
    """A CPU that answers the connection request only when the destination TSAP is for its slot, and otherwise hangs up."""
    def reply(req):
        return S7_CC if len(req) >= 22 and req[21] == only_slot else None
    return reply


class TcpSim(threading.Thread):
    """Accepts connections, reads what comes, answers with reply(request) (None = say nothing)."""
    def __init__(self, reply):
        super().__init__(daemon=True)
        self.srv = socket.socket(); self.srv.bind(("127.0.0.1", 0)); self.srv.listen(5); self.srv.settimeout(0.2)
        self.port, self.reply, self.seen, self.stop = self.srv.getsockname()[1], reply, [], False
        self.start()

    def run(self):
        while not self.stop:
            try:
                c, _ = self.srv.accept()
            except (socket.timeout, OSError):
                continue
            try:
                c.settimeout(1)
                req = c.recv(4096)
                self.seen.append(req)
                out = self.reply(req)
                if out:
                    c.sendall(out)
                    threading.Event().wait(0.05)
            except OSError:
                pass
            finally:
                c.close()

    def close(self):
        self.stop = True; self.srv.close()


class UdpSim(threading.Thread):
    def __init__(self, reply):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); self.sock.bind(("127.0.0.1", 0)); self.sock.settimeout(0.2)
        self.port, self.reply, self.seen, self.stop = self.sock.getsockname()[1], reply, [], False
        self.start()

    def run(self):
        while not self.stop:
            try:
                data, addr = self.sock.recvfrom(2048)
            except (socket.timeout, OSError):
                continue
            self.seen.append(data)
            out = self.reply(data)
            if out:
                self.sock.sendto(out, addr)

    def close(self):
        self.stop = True; self.sock.close()


def free_tcp_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class DetectTests(unittest.TestCase):
    def test_s7_answers_and_sends_the_real_connection_request(self):
        sim = TcpSim(lambda req: S7_CC)
        try:
            r = pd.detect_s7("127.0.0.1", sim.port, timeout=1)
            self.assertEqual(r["status"], "answers")
            self.assertEqual(sim.seen[0], bytes.fromhex("0300001611e00000000100c0010ac1020100c2020101"))   # slot 1 first
        finally:
            sim.close()

    def test_s7_cpu_that_wants_a_particular_slot_is_found_whichever_slot_it_is(self):
        # the first real run: an S7-1214C closes the connection for slot 2 and only accepts slot 1
        for slot in (1, 2, 0, 3):
            sim = TcpSim(s7_like(slot))
            try:
                r = pd.detect_s7("127.0.0.1", sim.port, timeout=0.6)
                self.assertEqual(r["status"], "answers", slot)
                self.assertIn(f"slot {slot}", r["text"])
            finally:
                sim.close()
        self.assertEqual(pd.s7_connection_request(1).hex(), "0300001611e00000000100c0010ac1020100c2020101")
        self.assertEqual(pd.s7_connection_request(2).hex(), "0300001611e00000000100c0010ac1020100c2020102")

    def test_a_port_that_is_open_but_not_s7_is_not_called_s7(self):
        sim = TcpSim(lambda req: b"HTTP/1.1 400 Bad Request\r\n\r\n")
        try:
            self.assertEqual(pd.detect_s7("127.0.0.1", sim.port, 1)["status"], "open")
        finally:
            sim.close()
        silent = TcpSim(lambda req: None)
        try:
            self.assertEqual(pd.detect_s7("127.0.0.1", silent.port, 0.5)["status"], "open")
        finally:
            silent.close()

    def test_a_closed_port_says_none(self):
        for fn in (pd.detect_s7, pd.detect_modbus_tcp, pd.detect_opcua):
            self.assertEqual(fn("127.0.0.1", free_tcp_port(), 0.5)["status"], "none", fn.__name__)

    def test_modbus_tcp_answer_and_exception(self):
        def ok(req):
            return req[0:2] + b"\x00\x00\x00\x05\x01\x03\x02\x00\x2a"
        def exc(req):
            return req[0:2] + b"\x00\x00\x00\x03\x01\x83\x02"
        sim = TcpSim(ok)
        try:
            r = pd.detect_modbus_tcp("127.0.0.1", sim.port, 1)
            self.assertEqual(r["status"], "answers"); self.assertIn("holding register", r["text"])
            self.assertEqual(sim.seen[0], bytes.fromhex("000100000006010300000001"))
        finally:
            sim.close()
        sim = TcpSim(exc)
        try:
            r = pd.detect_modbus_tcp("127.0.0.1", sim.port, 1)
            self.assertEqual(r["status"], "answers"); self.assertIn("exception 2", r["text"])
        finally:
            sim.close()
        wrong = TcpSim(lambda req: b"\x00\x09\x00\x00\x00\x03\x01\x03\x00")      # another transaction id: not our request
        try:
            self.assertEqual(pd.detect_modbus_tcp("127.0.0.1", wrong.port, 1)["status"], "open")
        finally:
            wrong.close()

    def test_opcua_acknowledge_error_and_the_hello_frame(self):
        sim = TcpSim(lambda req: b"ACKF" + b"\x1c\x00\x00\x00" + b"\x00" * 20)
        try:
            self.assertEqual(pd.detect_opcua("127.0.0.1", sim.port, 1)["status"], "answers")
            hello = sim.seen[0]
            self.assertEqual(hello[:4], b"HELF")
            self.assertEqual(int.from_bytes(hello[4:8], "little"), len(hello))          # the size field matches
            self.assertIn(b"opc.tcp://127.0.0.1:%d" % sim.port, hello)
        finally:
            sim.close()
        err = TcpSim(lambda req: b"ERRF" + b"\x10\x00\x00\x00" + b"\x00" * 8)
        try:
            self.assertEqual(pd.detect_opcua("127.0.0.1", err.port, 1)["status"], "answers")
        finally:
            err.close()

    def test_bacnet_who_is_gets_an_i_am(self):
        i_am = bytes.fromhex("810a001401001000c4020000012205c4910021") + b"\x3c"
        sim = UdpSim(lambda req: i_am if req == bytes.fromhex("810a000801001008") else None)
        try:
            self.assertEqual(pd.detect_bacnet_ip("127.0.0.1", sim.port, 1, bind_port=0)["status"], "answers")
        finally:
            sim.close()
        # an I-Am that came through a router carries a source address in the NPDU: still an I-Am
        routed = bytes.fromhex("810a001c0108000a0102") + bytes.fromhex("1000c4020000012205c4910021")
        self.assertTrue(pd._is_bacnet_iam(routed))
        self.assertFalse(pd._is_bacnet_iam(bytes.fromhex("810a000c0100301001")))     # a confirmed-request APDU, not an I-Am
        self.assertFalse(pd._is_bacnet_iam(b"garbage"))
        silent = UdpSim(lambda req: None)
        try:
            self.assertEqual(pd.detect_bacnet_ip("127.0.0.1", silent.port, 0.5, bind_port=0)["status"], "none")
        finally:
            silent.close()

    def test_detect_all_lists_only_what_answers_and_the_summary_names_it(self):
        s7 = TcpSim(lambda req: S7_CC)
        mb = TcpSim(lambda req: req[0:2] + b"\x00\x00\x00\x05\x01\x03\x02\x00\x01")
        try:
            res = pd.detect_all("127.0.0.1", timeout=0.6,
                                ports={"s7": s7.port, "modbus": mb.port, "opcua": free_tcp_port(), "bacnet": free_tcp_port()},
                                slmp_ports=(free_tcp_port(),))
            self.assertEqual([r["protocol"] for r in res], ["Modbus TCP", "S7comm (Siemens)"])
            self.assertTrue(all(r["status"] == "answers" for r in res))
            s = pd.summary(res)
            self.assertIn("S7comm (Siemens)", s); self.assertIn("Modbus TCP", s)
            self.assertIn("serial", pd.summary([]))
        finally:
            s7.close(); mb.close()


if __name__ == "__main__":
    unittest.main()

"""The "Discover DBs" button of Connections > Scan for live tags (S7). The first real use showed "Probing..." for ever with the button
disabled: a failed connection killed the background thread without a word. These tests use a stand-in driver (no PLC, no snap7)."""
import os
import queue
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "edge-gateway"))
import protoprobe                                   # noqa: E402  (imports tkinter; needs no display)
from drivers import s7_driver                       # noqa: E402


class FakeConn:
    interface = {"ip": "192.0.2.10", "rack": 0, "slot": 1}


def fake_driver(connect_error=None, existing=(), read_error="b'CPU : Address out of range'"):
    class Client:
        def db_read(self, db, start, size):
            if db not in existing:
                raise RuntimeError(read_error)
            return bytearray(size)

    class Driver:
        def __init__(self, name, cfg):
            self.client = Client()

        async def connect(self):
            if connect_error:
                raise RuntimeError(connect_error)

        async def disconnect(self):
            pass
    return Driver


def run(max_db, **kw):
    q = queue.Queue()
    with mock.patch.object(s7_driver, "S7Driver", fake_driver(**kw)):
        protoprobe.run_db_discovery(FakeConn(), max_db, q)
    msgs = []
    while not q.empty():
        msgs.append(q.get_nowait())
    return msgs


class DiscoverDbs(unittest.TestCase):
    def test_a_failed_connection_reaches_the_screen_instead_of_hanging(self):
        msgs = run(4, connect_error="b'ISO : An error occurred during recv TCP : Connection timed out'")
        self.assertEqual(msgs[-1][0], "db_discovery_error")
        text, colour = protoprobe.discovery_status(*msgs[-1])
        self.assertIn("Could not connect", text)
        self.assertIn("Connection timed out", text)
        self.assertIn("IP address", text)                          # the hint: network, not S7 settings
        self.assertEqual(colour, "red")

    def test_found_dbs_are_listed(self):
        msgs = run(4, existing=(1, 3))
        kind, (dbs, errs) = msgs[-1]
        self.assertEqual((kind, dbs), ("db_discovery_done", [1, 3]))
        self.assertEqual([e[0] for e in errs], [2, 4])
        text, colour = protoprobe.discovery_status(kind, (dbs, errs))
        self.assertEqual((text, colour), ("Done -- 2 DB(s) found", "gray"))

    def test_zero_found_says_why(self):
        msgs = run(3, existing=(), read_error="b'CPU : Function not available'")
        text, colour = protoprobe.discovery_status(*msgs[-1])
        self.assertIn("0 DB(s) found", text)
        self.assertIn("Function not available", text)
        self.assertIn("PUT/GET", text)
        self.assertEqual(colour, "red")

    def test_zero_found_because_the_dbs_do_not_exist_or_are_optimized(self):
        text, _ = protoprobe.discovery_status(*run(2, existing=())[-1])
        self.assertIn("Optimized block access", text.replace("optimized block access", "Optimized block access"))

    def test_progress_is_reported_per_db_before_the_final_message(self):
        msgs = run(3, existing=(1,))
        self.assertEqual([m for m in msgs if m[0] == "status"], [("status", "Probing DB1..."), ("status", "Probing DB2..."), ("status", "Probing DB3...")])
        self.assertTrue(msgs[-1][0].startswith("db_discovery_"))

    def test_the_hints_for_the_usual_s7_errors(self):
        e = protoprobe.explain_s7_error
        self.assertIn("rack and slot", e("ISO : Error during PDU negotiating"))
        self.assertIn("port 102", e("TCP : Unreachable peer"))
        self.assertIn("PUT/GET", e("CPU : Access to object not allowed"))
        self.assertIsNone(e("something nobody has seen"))


if __name__ == "__main__":
    unittest.main()

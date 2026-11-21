"""The ModScan-style Modbus TCP reader (modbus_scan.py) against a small fake device. Read only: there is no write function to test."""
import os
import socket
import struct
import sys
import threading
import unittest

HERE = os.path.dirname(__file__)
sys.path[:0] = [os.path.join(HERE, "..")]
import modbus_scan as ms  # noqa: E402


class FakeDevice:
    """Unit 1 serves holding registers 0..9 (value = 1000 + address) and coils; any other unit is silent; address >= 10 is an illegal address."""
    def __init__(self, silent_units=()):
        self.srv = socket.socket(); self.srv.bind(("127.0.0.1", 0)); self.srv.listen(5)
        self.port = self.srv.getsockname()[1]; self.stop = False; self.silent = set(silent_units)
        self.t = threading.Thread(target=self.run, daemon=True); self.t.start()

    def run(self):
        self.srv.settimeout(0.2)
        while not self.stop:
            try:
                c, _ = self.srv.accept()
            except OSError:
                continue
            try:
                req = c.recv(64)
                tid, _, _, unit, fn, start, count = struct.unpack(">HHHBBHH", req[:12])
                if unit != 1 or unit in self.silent:
                    c.close(); continue
                if start + count > 10:
                    pdu = bytes((fn | 0x80, 2))
                elif fn in (3, 4):
                    pdu = bytes((fn, count * 2)) + b"".join(struct.pack(">H", 1000 + start + i) for i in range(count))
                else:
                    bits = bytearray((count + 7) // 8)
                    for i in range(count):
                        if (start + i) % 2 == 0:
                            bits[i // 8] |= 1 << (i % 8)
                    pdu = bytes((fn, len(bits))) + bytes(bits)
                c.sendall(struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu)
            except (OSError, struct.error):
                pass
            finally:
                c.close()

    def close(self):
        self.stop = True; self.t.join(2); self.srv.close()


class Frames(unittest.TestCase):
    def test_request_bytes_are_the_specification(self):
        self.assertEqual(ms.hexdump(ms.build_request(1, 1, 3, 0, 2)), "00 01 00 00 00 06 01 03 00 00 00 02")

    def test_only_reads_exist_and_ranges_are_checked(self):
        for fn in (5, 6, 15, 16):
            with self.assertRaises(ValueError):
                ms.build_request(1, 1, fn, 0, 1)
        with self.assertRaises(ValueError):
            ms.build_request(1, 1, 3, 0, 126)
        self.assertFalse(any(name.startswith("write") for name in dir(ms)))

    def test_exception_and_short_answers_in_words(self):
        with self.assertRaises(ms.ModbusError) as cm:
            ms.parse_response(bytes.fromhex("0001 0000 0003 01 83 02"), 3, 1)
        self.assertIn("Illegal data address", str(cm.exception))
        with self.assertRaises(ms.ModbusError):
            ms.parse_response(b"\x00\x01", 3, 1)


class Decode(unittest.TestCase):
    def test_words_in_every_format_and_word_order(self):
        # 100.5 as float32 is 0x42C90000: high word first (ABCD), or swapped (CDAB)
        rows = ms.describe_words([0x42C9, 0x0000, 0xFFFF], start=10, base=1, order="ABCD")
        self.assertEqual(rows[0]["address"], 11)
        self.assertEqual(rows[0]["float32"], 100.5)
        self.assertEqual(rows[2]["signed"], -1)
        self.assertEqual(rows[2]["hex"], "0xFFFF")
        self.assertIsNone(rows[2]["uint32"])                       # no next word
        swapped = ms.describe_words([0x0000, 0x42C9], 0, order="CDAB")
        self.assertEqual(swapped[0]["float32"], 100.5)

    def test_bits(self):
        self.assertEqual(ms.describe_bits([1, 0], 5, base=1)[1], {"address": 7, "value": 0})


class Live(unittest.TestCase):
    def setUp(self):
        self.dev = FakeDevice()

    def tearDown(self):
        self.dev.close()

    def test_holding_registers_and_stats(self):
        st = ms.Stats()
        r = ms.read("127.0.0.1", self.dev.port, 1, 3, 2, 3, stats=st)
        self.assertEqual(r["values"], [1002, 1003, 1004])
        self.assertEqual(ms.hexdump(r["request"])[:11], "00 01 00 00")
        self.assertGreaterEqual(r["ms"], 0)
        self.assertEqual((st.sent, st.ok), (1, 1))

    def test_coils(self):
        self.assertEqual(ms.read("127.0.0.1", self.dev.port, 1, 1, 0, 4)["values"], [1, 0, 1, 0])

    def test_illegal_address_is_explained_and_counted(self):
        st = ms.Stats()
        with self.assertRaises(ms.ModbusError) as cm:
            ms.read("127.0.0.1", self.dev.port, 1, 3, 9, 5, stats=st)
        self.assertIn("Illegal data address", str(cm.exception))
        self.assertEqual((st.exceptions, st.ok), (1, 0))
        self.assertTrue(cm.exception.response)                       # the raw bytes are kept for the screen

    def test_silent_unit_times_out_in_words(self):
        st = ms.Stats()
        with self.assertRaises(ms.ModbusError) as cm:
            ms.read("127.0.0.1", self.dev.port, 7, 3, 0, 1, timeout=0.4, stats=st)
        self.assertTrue("closed" in str(cm.exception) or "no answer" in str(cm.exception))

    def test_refused_port(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        with self.assertRaises(ms.ModbusError) as cm:
            ms.read("127.0.0.1", port, 1, 3, 0, 1)
        self.assertIn("refused", str(cm.exception))

    def test_unit_sweep_finds_the_one_that_answers(self):
        found = ms.sweep_units("127.0.0.1", self.dev.port, range(0, 5), timeout=0.4)
        self.assertEqual([u for u, _ in found], [1])


class Screen(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        import protoprobe as pp
        self.tk = tk
        self._saved_root = tk._default_root
        tk._default_root = None
        try:
            self.app = pp.App()
        except tk.TclError as exc:
            tk._default_root = self._saved_root
            self.skipTest(f"no display: {exc}")
        self.dev = FakeDevice()

    def tearDown(self):
        self.dev.close()
        self.app.destroy()
        self.tk._default_root = self._saved_root

    def _wait(self, screen, text):
        import time
        for _ in range(60):
            self.app.update()
            if text in screen.status.cget("text"):
                return True
            time.sleep(0.05)
        return False

    def test_read_and_error_on_the_screen(self):
        s = self.app.screens["modbus_test"]
        s.host.insert(0, "127.0.0.1"); s.port.delete(0, "end"); s.port.insert(0, str(self.dev.port))
        s.count.delete(0, "end"); s.count.insert(0, "3")
        s.read_once()
        self.assertTrue(self._wait(s, "OK: 3 value(s)"))
        rows = [s.tree.item(i)["values"] for i in s.tree.get_children()]
        self.assertEqual([r[1] for r in rows], [1000, 1001, 1002])
        self.assertIn("Request :", s.raw.get("1.0", "end"))
        s.start.delete(0, "end"); s.start.insert(0, "9")
        s.read_once()
        self.assertTrue(self._wait(s, "Illegal data address"))
        self.assertIn("exceptions 1", s.stats_label.cget("text"))
        self.assertEqual([k for k in self.app.screens if k == "modbus_test"], ["modbus_test"])


if __name__ == "__main__":
    unittest.main()

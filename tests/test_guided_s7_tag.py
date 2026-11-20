"""The guided Add Siemens tag dialog and the one-tag Test read."""
import logging
import os
import sys
import tkinter as tk
import unittest
from tkinter import ttk

HERE = os.path.dirname(__file__)
sys.path[:0] = [os.path.join(HERE, ".."), os.path.join(HERE, "..", "edge-gateway")]
import protoprobe as pp  # noqa: E402
from tkinter import messagebox  # noqa: E402


class FakeS7:
    """Answers the tag named Good; the tag Bad is logged as the real driver does and left out."""
    def __init__(self, cfg):
        self.cfg = cfg
        self.log = logging.getLogger("edge_collector.s7")

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def poll(self):
        out = {}
        for t in self.cfg["tags"]:
            if t["name"] == "Bad":
                self.log.warning("[%s] tag %s decode error: %s", "g", t["name"], "Read operation failed: Object does not exist (0x0a)")
            else:
                out[t["mqtt_key"]] = 12.5
        return out


class ReadTagOnce(unittest.TestCase):
    conn = pp.Connection("c", "s7comm", {"ip": "192.0.2.1", "rack": 0, "slot": 1})

    def test_value_comes_back(self):
        value, error = pp.read_tag_once(self.conn, {"name": "Good", "area": "M", "start_byte": 0, "data_type": "int16"}, {"s7comm": FakeS7})
        self.assertEqual((value, error), (12.5, None))

    def test_the_reason_comes_back(self):
        value, error = pp.read_tag_once(self.conn, {"name": "Bad", "area": "DB", "db_number": 1, "start_byte": 0, "data_type": "int16"},
                                        {"s7comm": FakeS7})
        self.assertIsNone(value)
        self.assertIn("Object does not exist", error)

    def test_connection_failure_is_a_message_not_a_crash(self):
        class Refuses(FakeS7):
            async def connect(self):
                raise ConnectionError("timed out")
        value, error = pp.read_tag_once(self.conn, {"name": "Good", "area": "M", "start_byte": 0, "data_type": "int16"}, {"s7comm": Refuses})
        self.assertEqual((value, error), (None, "timed out"))


def walk(w):
    yield w
    for c in w.winfo_children():
        yield from walk(c)


class GuidedDialog(unittest.TestCase):
    def setUp(self):
        self._saved_root = tk._default_root
        tk._default_root = None
        try:
            self.app = pp.App()
        except tk.TclError as exc:
            tk._default_root = self._saved_root
            self.skipTest(f"no display: {exc}")
        self._orig = messagebox.showerror
        self.errors = []
        messagebox.showerror = lambda *a, **k: self.errors.append(a)
        self.conn = pp.Connection("c", "s7comm", {"ip": "192.0.2.1", "rack": 0, "slot": 1})
        self.saved = []
        self.win = self.app.screens["connections"]._s7_guided_dialog(self.app, self.conn, lambda: self.saved.append(1))
        self.app.update()

    def tearDown(self):
        messagebox.showerror = self._orig
        self.app.destroy()
        tk._default_root = self._saved_root

    def button(self, text):
        return next(w for w in walk(self.win) if w.winfo_class() in ("TRadiobutton", "TButton") and w.cget("text") == text)

    def entries(self):
        return [w for w in walk(self.win) if w.winfo_class() == "TEntry"]      # name, db, byte, bit, scale, offset, unit

    def set_entry(self, widget, text):
        widget.delete(0, "end")
        widget.insert(0, text)
        self.win.update()

    def test_byte_buttons_are_disabled(self):
        for text in ("IB", "QB", "MB", "DBn.DBB"):
            self.assertTrue(str(self.button(text).cget("state")) == "disabled", text)

    def test_dint_in_markers(self):
        e = self.entries()
        self.set_entry(e[0], "HMI_line_speed")
        self.button("MD").invoke()
        self.set_entry(e[2], "60")
        self.win.update()
        preview = next(w for w in walk(self.win) if w.winfo_class() == "TLabel" and "MD60" in str(w.cget("text")))
        self.assertIn("MD60", str(preview.cget("text")))
        next(w for w in walk(self.win) if w.winfo_class() == "TCombobox").set("int32")
        self.button("OK").invoke()
        tag = self.conn.tags[0]
        self.assertEqual((tag["name"], tag["area"], tag["start_byte"], tag["data_type"]), ("HMI_line_speed", "M", 60, "int32"))
        self.assertEqual(self.saved, [1])

    def test_bit_in_outputs(self):
        e = self.entries()
        self.set_entry(e[0], "Tower_lamp_O")
        self.button("Q").invoke()
        self.set_entry(e[2], "0")
        self.set_entry(e[3], "5")
        self.button("OK").invoke()
        tag = self.conn.tags[0]
        self.assertEqual((tag["area"], tag["start_byte"], tag["bit_offset"], tag["data_type"]), ("Q", 0, 5, "bit"))

    def test_a_bad_bit_is_refused_with_a_message(self):
        e = self.entries()
        self.button("M").invoke()
        self.set_entry(e[3], "9")
        self.button("OK").invoke()
        self.assertEqual(self.conn.tags, [])
        self.assertTrue(self.errors)


if __name__ == "__main__":
    unittest.main()

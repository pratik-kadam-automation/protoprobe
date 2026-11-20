"""Choosing a preset in the Scan for live tags window fills the fields (Tk; skipped without a display)."""
import os
import sys
import tkinter as tk
import unittest
from tkinter import ttk

HERE = os.path.dirname(__file__)
sys.path[:0] = [os.path.join(HERE, ".."), os.path.join(HERE, "..", "edge-gateway")]
import protoprobe as pp  # noqa: E402


def walk(w):
    yield w
    for c in w.winfo_children():
        yield from walk(c)


class ScanPresetDialog(unittest.TestCase):
    def setUp(self):
        # Another test may leave its own Tk window as tkinter's default root; checkbox variables would then live in that window's interpreter.
        # Use a clean default root for this test and give the old one back afterwards.
        self._saved_root = tk._default_root
        tk._default_root = None
        try:
            self.app = pp.App()
        except tk.TclError as exc:
            tk._default_root = self._saved_root
            self.skipTest(f"no display: {exc}")

    def tearDown(self):
        self.app.destroy()
        tk._default_root = self._saved_root

    def open_for(self, protocol, interface, plc_type=None):
        conn = pp.Connection("c1", protocol, interface, plc_type)
        self.app.connections["c1"] = conn
        scr = self.app.screens["connections"]
        scr.refresh()
        scr.conn_tree.selection_set("c1")
        scr.open_scan_dialog()
        self.app.update()
        self.top = [w for w in walk(self.app) if isinstance(w, tk.Toplevel)][0]
        self.combos = [w for w in walk(self.top) if isinstance(w, ttk.Combobox)]
        return self.combos[0]            # the preset box is the first combobox in the window

    def choose(self, box, prefix):
        box.set(next(v for v in box["values"] if v.startswith(prefix)))
        box.event_generate("<<ComboboxSelected>>")
        self.top.update()

    def test_s7_single_preset_fills_area_and_range(self):
        box = self.open_for("s7comm", {"ip": "192.0.2.1", "rack": 0, "slot": 1}, "S7_1200")
        self.choose(box, "Markers M")
        self.assertEqual([c.get() for c in self.combos[1:2]], ["M"])
        entries = [e.get() for e in walk(self.top) if e.winfo_class() == "TEntry"]
        self.assertIn("255", entries)

    def test_s7_whole_plc_preset_unticks_db(self):
        box = self.open_for("s7comm", {"ip": "192.0.2.1", "rack": 0, "slot": 1})        # protocol only: default type's presets
        self.choose(box, "Everything except")
        boxes = {w.cget("text"): bool(int(self.top.getvar(str(w.cget("variable"))))) for w in walk(self.top) if w.winfo_class() == "TCheckbutton"}
        self.assertEqual(boxes, {"DB": False, "I": True, "Q": True, "M": True})

    def test_no_preset_box_for_opcua(self):
        conn = pp.Connection("c2", "opcua", {"endpoint": "opc.tcp://x"})
        self.app.connections["c2"] = conn
        scr = self.app.screens["connections"]
        scr.refresh()
        scr.conn_tree.selection_set("c2")
        shown = []
        pp.messagebox.showinfo = lambda *a, **k: shown.append(a)
        scr.open_scan_dialog()
        self.assertTrue(shown)                 # "not needed for this protocol", no scan window


if __name__ == "__main__":
    unittest.main()

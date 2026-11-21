"""The PLC-type dropdown in Add Connection: choosing a type sets the protocol and defaults, a type without a driver cannot be saved."""
import os
import sys
import tkinter as tk
import unittest

HERE = os.path.dirname(__file__)
sys.path[:0] = [os.path.join(HERE, ".."), os.path.join(HERE, "..", "edge-gateway")]
import protoprobe as pp  # noqa: E402
from tkinter import messagebox  # noqa: E402


def walk(w):
    yield w
    for c in w.winfo_children():
        yield from walk(c)


class AddConnectionType(unittest.TestCase):
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
        self.shown = []
        self._orig = messagebox.showinfo
        messagebox.showinfo = lambda *a, **k: self.shown.append(a)
        self.app.screens["connections"].open_add_connection()
        self.app.update()
        self.top = [w for w in walk(self.app) if isinstance(w, tk.Toplevel)][0]
        self.type_cb, self.proto_cb = [w for w in walk(self.top) if w.winfo_class() == "TCombobox"][:2]
        self.save = [w for w in walk(self.top) if w.winfo_class() == "TButton" and w.cget("text") == "Save"][0]

    def tearDown(self):
        messagebox.showinfo = self._orig
        self.app.destroy()
        tk._default_root = self._saved_root

    def pick(self, prefix):
        text = next(v for v in self.type_cb["values"] if v.startswith(prefix))
        self.type_cb.set(text)
        self.type_cb.event_generate("<<ComboboxSelected>>")
        self.top.update()

    def fields(self):
        return [e.get() for e in walk(self.top) if e.winfo_class() == "TEntry"][1:]

    def test_siemens_types_set_protocol_and_slot(self):
        self.pick("S7_1200")
        self.assertEqual(self.proto_cb.get(), pp.PROTOCOL_LABELS["s7comm"])
        self.assertEqual(self.fields()[-3:-1], ["0", "1"])          # rack, slot (the last box is the poll interval)
        self.pick("S7-300")
        self.assertEqual(self.fields()[-3:-1], ["0", "2"])

    def test_type_without_driver_is_not_saved(self):
        self.pick("FINS_TCP")
        self.save.invoke()
        self.assertEqual(list(self.app.connections), [])
        self.assertTrue(self.shown)

    def test_supported_type_is_saved_with_its_type(self):
        self.pick("Q_TCP")
        self.save.invoke()
        conn = next(iter(self.app.connections.values()))
        self.assertEqual((conn.protocol, conn.plc_type), ("mc_protocol_3e", "Q_TCP"))


if __name__ == "__main__":
    unittest.main()

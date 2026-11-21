"""The Capture traffic window opens for a connection with an IP, and without administrator rights says so in words (no crash)."""
import os
import sys
import tkinter as tk
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import capture  # noqa: E402
import protoprobe as pp  # noqa: E402


class CaptureWindow(unittest.TestCase):
    def test_opens_and_reports_missing_admin(self):
        app = pp.App()
        try:
            app.connections["c"] = pp.Connection("c", "mc_protocol_3e", {"ip": "192.168.3.39", "port": 1388, "transport": "udp"})
            screen = app.screens["connections"]
            screen.refresh()
            screen.conn_tree.selection_set("c")
            with mock.patch.object(capture, "is_admin", return_value=False):
                screen.open_capture()
                app.update()
                tops = [w for w in screen.winfo_children() if isinstance(w, tk.Toplevel)]
                self.assertTrue(tops)
                top = tops[0]
                start = [b for f in top.winfo_children() if isinstance(f, pp.ttk.Frame) for b in f.winfo_children() if b.cget("text") == "Start"][0]
                start.invoke()
                app.update()
                texts = [w.get("1.0", "end") for w in top.winfo_children() if isinstance(w, tk.Text)]
                self.assertTrue(any("administrator" in t for t in texts))
                top.destroy()
        finally:
            app.destroy()


if __name__ == "__main__":
    unittest.main()

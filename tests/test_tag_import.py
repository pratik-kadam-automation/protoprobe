"""Tag candidates from an Excel/CSV tag list and from a text PDF (tag_import.py). All data is synthetic."""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(__file__)
sys.path[:0] = [os.path.join(HERE, ".."), os.path.join(HERE, "..", "edge-gateway"), HERE]
import tag_import as ti  # noqa: E402
from test_tia_import import make_xlsx  # noqa: E402

ROWS = [["Project X (synthetic)"], [],
        ["Symbol", "Address", "Data Type", "Comment"],
        ["Machine_Running", "%Q0.5", "Bool", "Main motor running"],
        ["Parts_Count", "%MD20", "DInt", "Good parts produced"],
        ["Bearing_Temp", "%MW10", "Int", "Bearing temperature C"],
        ["Spare", "%I0", "Bool", ""],
        ["Pump", "DB1.DBW4", "Int", "pump speed rpm"]]


class Table(unittest.TestCase):
    def test_excel_with_a_title_above_the_header(self):
        path = make_xlsx(ROWS)
        res = ti.load(path)
        self.assertIsNone(res["error"])
        by = {c["name"]: c for c in res["candidates"]}
        self.assertEqual(by["Machine_Running"]["tag"]["bit_offset"], 5)
        self.assertEqual(by["Machine_Running"]["tag"]["area"], "Q")
        self.assertEqual(by["Parts_Count"]["tag"]["data_type"], "int32")
        self.assertEqual(by["Parts_Count"]["role"], "production")
        self.assertEqual(by["Machine_Running"]["role"], "state")
        self.assertEqual(by["Bearing_Temp"]["role"], "predictive")
        self.assertEqual(by["Pump"]["tag"]["db_number"], 1)
        self.assertIsNone(by["Spare"]["tag"])                       # I0 has no width: said, not guessed
        self.assertIn("how wide", by["Spare"]["why_not"])

    def test_csv_and_sorting(self):
        path = os.path.join(tempfile.mkdtemp(), "t.csv")
        open(path, "w", encoding="utf-8").write("Name;Address;Comment\nMotor_Run;D100;running\nTemp;D101;temperature\nOther;D102;x\n")
        res = ti.load(path)
        best = ti.sort_best_first(res["candidates"])
        self.assertEqual(best[-1]["name"], "Other")
        self.assertEqual(res["candidates"][0]["protocol"], "mc_protocol_3e")

    def test_modbus_addresses(self):
        c = ti.from_lines(["Level 40005 tank level"])[0]
        self.assertEqual((c["tag"]["address"], c["tag"]["register_type"]), (4, "holding"))

    def test_unknown_extension_and_broken_file_give_a_message(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x.docx"); open(p, "wb").write(b"x")
        self.assertIn("reads .xlsx", ti.load(p)["error"])
        p = os.path.join(d, "bad.xlsx"); open(p, "wb").write(b"not a zip")
        self.assertIn("could not be read", ti.load(p)["error"])


class Lines(unittest.TestCase):
    def test_a_printed_list_in_free_layout(self):
        lines = ["I/O LIST rev 3", "Motor_Run    %Q0.5   Bool   Main motor running", "%MW10  Speed_Act  Int  rpm", "page 1 of 2",
                 "Parts_Count | %MD20 | DInt | good parts"]
        got = {c["name"]: c for c in ti.from_lines(lines)}
        self.assertEqual(set(got), {"Motor_Run", "Speed_Act", "Parts_Count"})
        self.assertEqual(got["Speed_Act"]["tag"]["start_byte"], 10)
        self.assertEqual(got["Motor_Run"]["comment"], "Main motor running")

    def test_duplicates_are_dropped(self):
        c = ti.from_lines(["A %MW10 Int", "A %MW10 Int"])
        self.assertEqual(len(ti.dedupe(c)), 1)


class Pdf(unittest.TestCase):
    def _pdf(self, lines):
        from reportlab.pdfgen import canvas
        path = os.path.join(tempfile.mkdtemp(), "list.pdf")
        c = canvas.Canvas(path)
        y = 800
        for l in lines:
            c.drawString(50, y, l); y -= 18
        c.save()
        return path

    def test_text_pdf(self):
        try:
            import pypdf  # noqa: F401
            import reportlab  # noqa: F401
        except ImportError:
            self.skipTest("pypdf/reportlab not installed")
        res = ti.load(self._pdf(["Machine_Running   %Q0.5   Bool   Main motor running", "Parts_Count   %MD20   DInt   good parts"]))
        self.assertIsNone(res["error"])
        self.assertEqual({c["name"] for c in res["candidates"]}, {"Machine_Running", "Parts_Count"})

    def test_pdf_without_text_is_refused_in_words(self):
        try:
            import pypdf  # noqa: F401
            import reportlab  # noqa: F401
        except ImportError:
            self.skipTest("pypdf/reportlab not installed")
        res = ti.load(self._pdf([]))
        self.assertIn("no text", res["error"])
        self.assertEqual(res["candidates"], [])


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
        self.conn = pp.Connection("plc", "s7comm", {"ip": "192.0.2.1", "rack": 0, "slot": 1})
        self.app.connections["plc"] = self.conn
        self.screen = self.app.screens["connections"]
        self.screen.refresh()

    def tearDown(self):
        self.app.destroy()
        self.tk._default_root = self._saved_root

    def test_import_dialog_adds_only_what_fits(self):
        from tkinter import messagebox
        orig = messagebox.showinfo
        messagebox.showinfo = lambda *a, **k: None
        try:
            path = os.path.join(tempfile.mkdtemp(), "t.csv")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("Name;Address;Comment\nMachine_Running;%Q0.5;running\nPlc_D;D100;a mitsubishi register\n")
            self.screen.conn_tree.selection_set("plc")
            win = self.screen.open_import_dialog(path)
            self.app.update()
            tree = win.import_tree
            tree.selection_set(tree.get_children())
            win.import_add(True)
            self.assertEqual([t["name"] for t in self.conn.tags], ["Machine_Running"])
            self.assertEqual(self.conn.tags[0]["output"], "Machine_Running")
            win.destroy()
        finally:
            messagebox.showinfo = orig


if __name__ == "__main__":
    unittest.main()

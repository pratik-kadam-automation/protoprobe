"""TIA Portal tag-table import: the .xlsx reader, the parser and the whole Siemens verify flow (fake S7 PLC)."""
import os
import struct
import sys
import tempfile
import unittest
import zipfile
from xml.sax.saxutils import escape

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import commissioning as cm

try:
    import tkinter as tk
    root = tk.Tk(); root.withdraw()
except Exception:
    tk = None


def col_letter(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def make_xlsx(rows, first_row=1, first_col=0):
    """A small but real .xlsx (shared strings, cell references with gaps), written with the standard library."""
    strings, sheet = [], []
    for ri, row in enumerate(rows, start=first_row):
        cells = []
        for ci, val in enumerate(row, start=first_col):
            if val in (None, ""):
                continue
            ref = f"{col_letter(ci)}{ri}"
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                cells.append(f'<c r="{ref}"><v>{val}</v></c>')
            else:
                strings.append(str(val))
                cells.append(f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>')
        sheet.append(f'<row r="{ri}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    path = os.path.join(tempfile.mkdtemp(), "tags.xlsx")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("xl/workbook.xml", f'<?xml version="1.0"?><workbook {ns}/>')
        z.writestr("xl/sharedStrings.xml", f'<?xml version="1.0"?><sst {ns}>' + "".join(f"<si><t>{escape(s)}</t></si>" for s in strings) + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml", f'<?xml version="1.0"?><worksheet {ns}><sheetData>' + "".join(sheet) + "</sheetData></worksheet>")
    return path


HEADER = ["Name", "Path", "Data Type", "Logical Address", "Comment", "Hmi Visible"]
ROWS = [HEADER,
        ["Tank Level", "Default tag table", "Real", "%MD20", "Level in %", "TRUE"],
        ["Pump_Run", "Default tag table", "Bool", "%M0.0", "", "TRUE"],
        ["Speed_Act", "Default tag table", "Int", "%MW10", "rpm", "TRUE"],
        ["Odd_Byte", "Default tag table", "Int", "%MW11", "", "TRUE"],
        ["Wrong_Size", "Default tag table", "Real", "%MW30", "", "TRUE"],
        ["Total_Count", "Default tag table", "DInt", "%QD8", "", "TRUE"],
        ["Optimized", "Default tag table", "Real", "", "", "TRUE"],
        ["Flow_DB", "Default tag table", "Real", "%DB1.DBD4", "m3/h", "TRUE"],
        ["Label", "Default tag table", "String", "%MB100", "", "TRUE"],
        ["Raw", "Default tag table", "Byte", "%MB50", "", "TRUE"],
        ["Odd_Type", "Default tag table", "Foo", "%MW40", "", "TRUE"],
        ["Dup Name", "Default tag table", "Int", "%MW60", "", "TRUE"],
        ["Dup_Name", "Default tag table", "Int", "%MW62", "", "TRUE"]]


class Reader(unittest.TestCase):
    def test_columns_keep_their_position_with_gaps_and_an_offset_table(self):
        path = make_xlsx([["Name", "", "Data Type"], ["a", "", "Int"], [], ["b", "", "Real"]], first_row=3, first_col=2)
        rows = cm.read_xlsx_rows(open(path, "rb").read())
        self.assertEqual([r for r in rows if r], [["", "", "Name", "", "Data Type"], ["", "", "a", "", "Int"], ["", "", "b", "", "Real"]])

    def test_a_file_that_is_not_excel_is_reported(self):
        p = os.path.join(tempfile.mkdtemp(), "x.xlsx")
        open(p, "wb").write(b"PK not really a zip")
        r = cm.parse_tag_file(p, "s7")
        self.assertIn("Could not read the Excel file", r["error"])


class Parser(unittest.TestCase):
    def setUp(self):
        self.r = cm.parse_tag_file(make_xlsx(ROWS), "s7")
        self.by = {t["name"]: t for t in self.r["tags"]}

    def test_what_is_read(self):
        self.assertEqual(set(self.by), {"Tank_Level", "Speed_Act", "Total_Count", "Flow_DB", "Dup_Name"})
        t = self.by["Tank_Level"]
        self.assertEqual((t["area"], t["number"], t["datatype"], t["byte_order"], t["words"], t["meaning"]), ("M", 20, "float32", "ABCD", 2, "Level in %"))
        self.assertEqual(t["renamed_from"], "Tank Level")
        self.assertEqual((self.by["Total_Count"]["area"], self.by["Total_Count"]["datatype"]), ("Q", "int32"))
        f = self.by["Flow_DB"]
        self.assertEqual((f["area"], f["db"], f["number"]), ("DB", 1, 4))
        self.assertEqual(self.by["Speed_Act"]["byte_order"], "")

    def test_what_is_not_read_says_why(self):
        why = dict(self.r["skipped"])
        self.assertIn("bit tags", why["Pump_Run"])
        self.assertIn("odd byte", why["Odd_Byte"])
        self.assertIn("16-bit but data type Real needs 32", why["Wrong_Size"])
        self.assertIn("optimized block", why["Optimized"])
        self.assertIn("text", why["Label"])
        self.assertIn("8-bit", why["Raw"])
        self.assertIn("not understood", why["Odd_Type"])
        self.assertIn("twice", why["Dup_Name"] if "Dup_Name" in why else why["Dup Name"])

    def test_regions_are_byte_addressed_and_per_area_and_data_block(self):
        regs = {g.name: (g.area, g.db, g.start, g.end, g.step) for g in cm.import_regions(self.r["tags"])}
        self.assertEqual(regs["DB1"], ("DB", 1, 4, 6, 2))
        self.assertEqual(regs["M"], ("M", None, 10, 60, 2))
        self.assertEqual(regs["Q"], ("Q", None, 8, 10, 2))

    def test_csv_text_and_missing_header(self):
        text = "Name;Data Type;Logical Address;Comment\nA;Int;%MW2;x\n"
        r = cm.parse_tia_tags(cm.rows_from_text(text))
        self.assertEqual([t["name"] for t in r["tags"]], ["A"])
        self.assertIn("No header row", cm.parse_tia_tags([["foo", "bar"]])["error"])

    def test_addresses_in_the_pack_look_like_the_programmers(self):
        regs = {g.name: g for g in cm.import_regions(self.r["tags"])}
        f = self.by["Flow_DB"]
        self.assertEqual(cm.format_address("s7", regs["DB1"], f["number"], f["datatype"]), "DB1.DBD4")
        t = self.by["Tank_Level"]
        self.assertEqual(cm.format_address("s7", regs["M"], t["number"], t["datatype"]), "MD20")
        s = self.by["Speed_Act"]
        self.assertEqual(cm.format_address("s7", regs["M"], s["number"], s["datatype"]), "MW10")


class FakeS7:
    """A Siemens CPU: markers M10 (int16, the HMI shows it x0.1), M20 (float32), outputs Q8 (int32 counter), DB1.DBD4 (float32)."""
    SPEED = [1500, 1620, 1580, 1710]          # rpm x 10 in the PLC: the HMI shows 150.0, 162.0 ...
    LEVEL = [42.5, 55.0, 61.25, 38.0]
    COUNT = [1000, 1007, 1019, 1030]
    FLOW = [12.5, 14.0, 9.75, 11.0]

    def __init__(self):
        self.k = -1

    def advance(self):
        self.k = min(self.k + 1, 3)

    def hmi_text(self, k=None):
        k = max(self.k, 0) if k is None else min(max(k, 0), 3)
        return {"speed": f"{self.SPEED[k] / 10:.1f}", "level": f"{self.LEVEL[k]:.2f}", "count": str(self.COUNT[k]), "flow": f"{self.FLOW[k]:.2f}"}

    @staticmethod
    def _f(v):
        b = struct.pack(">f", v)
        return (b[0] << 8) | b[1], (b[2] << 8) | b[3]

    def read_region(self, region, stop=None):
        k = max(self.k, 0)
        mem = {("M", None): {10: self.SPEED[k], 20: self._f(self.LEVEL[k])[0], 22: self._f(self.LEVEL[k])[1]},
               ("Q", None): {8: (self.COUNT[k] >> 16) & 0xFFFF, 10: self.COUNT[k] & 0xFFFF},
               ("DB", 1): {4: self._f(self.FLOW[k])[0], 6: self._f(self.FLOW[k])[1]}}[(region.area, region.db)]
        return {a: mem.get(a, 0) for a in range(region.start, region.end + 1, region.step)}


@unittest.skipIf(tk is None, "tkinter or display not available")
class SiemensFlow(unittest.TestCase):
    def setUp(self):
        from commissioning_screen import CommissioningScreen
        import commissioning_screen as cs
        cs.messagebox.showinfo = lambda *a, **k: None
        cs.messagebox.showerror = lambda *a, **k: None
        self.s = CommissioningScreen(root, app=None)
        self.s.pack(fill="both", expand=True)
        self.s.sync = True

    def test_import_a_tia_table_verify_it_and_catch_the_missing_scale(self):
        for k, v in {"client": "acme", "machine_id": "MC_PRESS_1", "machine_type": "general", "plc_make": "siemens", "plc_ip": "10.0.0.5", "db_numbers": "1"}.items():
            self.s.answer_vars[k].set(v)
        rows = [HEADER,
                ["Speed Act", "t", "Int", "%MW10", "rpm", "TRUE"],
                ["Tank_Level", "t", "Real", "%MD20", "Level in %", "TRUE"],
                ["Total_Count", "t", "DInt", "%QD8", "", "TRUE"],
                ["Flow_DB", "t", "Real", "%DB1.DBD4", "m3/h", "TRUE"]]
        self.s.practice = FakeS7()
        self.s.practice_var.set(True)
        self.s._questions_done()
        self.s.practice = FakeS7()
        self.s.load_imported(cm.parse_tia_tags(rows))
        self.assertEqual(self.s.mode, "verify")
        self.assertEqual({(g.area, g.db) for g in self.s.regions}, {("M", None), ("Q", None), ("DB", 1)})
        shown = {"Speed_Act": "speed", "Tank_Level": "level", "Total_Count": "count", "Flow_DB": "flow"}
        for _ in range(3):
            nxt = self.s.practice.hmi_text(self.s.practice.k + 1)
            for name, key in shown.items():
                self.s.hmi_vars[name].set(nxt[key])
            self.s.record_reading()
            root.update()
        tags = {t["name"]: t for t in self.s.build_tags()}
        self.assertEqual(tags["Tank_Level"]["confidence"], "confirmed")
        self.assertEqual(tags["Total_Count"]["confidence"], "confirmed")
        self.assertEqual(tags["Flow_DB"]["confidence"], "confirmed")
        self.assertEqual(tags["Speed_Act"]["confidence"], "unverified", "the PLC holds 1500, the HMI shows 150.0: the list has no scale")
        self.assertEqual(self.s.apply_suggested_scales(), ["Speed_Act x0.1"])
        tags = {t["name"]: t for t in self.s.build_tags()}
        self.assertEqual((tags["Speed_Act"]["scale"], tags["Speed_Act"]["confidence"]), (0.1, "confirmed"))
        self.assertEqual(tags["Flow_DB"]["address"], "DB1.DBD4"); self.assertEqual(tags["Tank_Level"]["address"], "MD20")
        self.assertEqual(tags["Tank_Level"]["byte_order"], "ABCD")
        self.assertEqual(tags["Tank_Level"]["meaning"], "Level in %")
        pack = self.s.pack_dict()
        self.assertEqual(pack["machines"][0]["plc"]["protocol"], "s7")
        self.assertNotIn("transport", pack["machines"][0]["plc"], "transport is an SLMP setting")
        self.assertEqual(cm.validate_pack(pack), [])


if __name__ == "__main__":
    unittest.main()

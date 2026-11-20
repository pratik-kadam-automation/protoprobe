"""The .xlsx reader accepts any worksheet file name, not only xl/worksheets/sheet1.xml."""
import io
import os
import sys
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import commissioning as cm  # noqa: E402

SHEET = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
         '<row r="1"><c r="A1" t="inlineStr"><is><t>Name</t></is></c><c r="B1" t="inlineStr"><is><t>Data Type</t></is></c></row>'
         '<row r="2"><c r="A2" t="inlineStr"><is><t>Speed</t></is></c><c r="B2" t="inlineStr"><is><t>Int</t></is></c></row>'
         '</sheetData></worksheet>')


def xlsx(sheet_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(sheet_path, SHEET)
    return buf.getvalue()


class SheetNames(unittest.TestCase):
    def test_usual_name(self):
        self.assertEqual(cm.read_xlsx_rows(xlsx("xl/worksheets/sheet1.xml"))[1], ["Speed", "Int"])

    def test_capitalised_name(self):
        self.assertEqual(cm.read_xlsx_rows(xlsx("xl/worksheets/Sheet1.xml"))[1], ["Speed", "Int"])

    def test_own_name(self):
        self.assertEqual(cm.read_xlsx_rows(xlsx("xl/worksheets/Tags.xml"))[0], ["Name", "Data Type"])

    def test_no_worksheet_names_the_parts(self):
        with self.assertRaises(ValueError) as ctx:
            cm.read_xlsx_rows(xlsx("xl/other/data.xml"))
        self.assertIn("xl/other/data.xml", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

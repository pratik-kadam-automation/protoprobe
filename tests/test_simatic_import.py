"""SIMATIC Manager symbol table import (.sdf and .asc), synthetic samples."""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
import commissioning as cm

FIX = os.path.join(HERE, "fixtures")


class Simatic(unittest.TestCase):
    def test_sdf(self):
        r = cm.parse_tag_file(os.path.join(FIX, "simatic-symbols.sdf"), "s7")
        tags = {t["name"]: t for t in r["tags"]}
        self.assertEqual(set(tags), {"Pump_Speed_SP", "Tank_Level", "Total_Count", "Flow_Meter"})
        self.assertEqual((tags["Pump_Speed_SP"]["area"], tags["Pump_Speed_SP"]["number"], tags["Pump_Speed_SP"]["datatype"]), ("M", 10, "int16"))
        self.assertEqual((tags["Tank_Level"]["datatype"], tags["Tank_Level"]["byte_order"]), ("float32", "ABCD"))
        self.assertEqual((tags["Total_Count"]["datatype"], tags["Total_Count"]["number"]), ("int32", 24))
        self.assertEqual((tags["Flow_Meter"]["area"], tags["Flow_Meter"]["number"]), ("I", 64))
        self.assertEqual(tags["Flow_Meter"]["renamed_from"], "Flow Meter")
        self.assertEqual(tags["Tank_Level"]["meaning"], "level %")
        why = dict(r["skipped"])
        for n in ("Run_Cmd", "Raw_In", "Data_Block_1", "Odd_Word"):
            self.assertIn(n, why)
        self.assertIn("odd byte", why["Odd_Word"])

    def test_asc_columns(self):
        r = cm.parse_tag_file(os.path.join(FIX, "simatic-symbols.asc"), "s7")
        self.assertEqual([t["name"] for t in r["tags"]], ["Pump_Speed_SP", "Tank_Level"])
        self.assertEqual(r["tags"][1]["number"], 20)

    def test_cp1252_comment(self):
        p = os.path.join(tempfile.mkdtemp(), "s.sdf")
        with open(p, "wb") as f:
            f.write('"Temp_Out","MW 2","INT","Temp \xe9\xb0"\n'.encode("cp1252", errors="ignore"))
            f.write(b'"Temp_In","MW 4","INT","caf\xe9"\n')
        r = cm.parse_tag_file(p, "s7")
        self.assertEqual({t["name"] for t in r["tags"]}, {"Temp_Out", "Temp_In"})

    def test_tia_text_still_wins(self):
        p = os.path.join(tempfile.mkdtemp(), "t.csv")
        with open(p, "w") as f:
            f.write("Name,Path,Data Type,Logical Address,Comment\nA,t,Int,%MW10,\n")
        self.assertEqual([t["name"] for t in cm.parse_tag_file(p, "s7")["tags"]], ["A"])

    def test_garbage_gives_clear_error(self):
        r = cm.parse_simatic_symbols("hello\nworld\n")
        self.assertEqual(r["tags"], [])
        self.assertIn("Table > Export", r["error"])


if __name__ == "__main__":
    unittest.main()

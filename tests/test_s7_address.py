import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import s7_address as a  # noqa: E402


class AddressText(unittest.TestCase):
    def test_tia_notation(self):
        self.assertEqual(a.address_text("DB", "word", 1, 0), "DB1.DBW0")
        self.assertEqual(a.address_text("DB", "bit", 4, 2, 5), "DB4.DBX2.5")
        self.assertEqual(a.address_text("I", "bit", 0, 3, 1), "I3.1")
        self.assertEqual(a.address_text("I", "word", 0, 4), "IW4")
        self.assertEqual(a.address_text("Q", "dword", 0, 8), "QD8")
        self.assertEqual(a.address_text("M", "dword", 0, 60), "MD60")
        self.assertEqual(a.address_text("M", "bit", 0, 0, 3), "M0.3")

    def test_bad_values(self):
        for args in (("X", "bit", 0, 0), ("M", "bit", 0, 0, 8), ("M", "word", 0, -1), ("M", "nibble", 0, 0)):
            with self.assertRaises(ValueError):
                a.address_text(*args)


class BuildTag(unittest.TestCase):
    def test_bit_tag(self):
        t = a.build_tag("Lamp", "Q", "bit", 0, 0, 5)
        self.assertEqual((t["area"], t["start_byte"], t["bit_offset"], t["data_type"]), ("Q", 0, 5, "bit"))

    def test_dint_in_markers(self):
        t = a.build_tag("HMI_line_speed", "M", "dword", 0, 60, data_type="int32")
        self.assertEqual((t["area"], t["start_byte"], t["data_type"], t["bit_offset"]), ("M", 60, "int32", 0))

    def test_db_word_with_scale_and_unit(self):
        t = a.build_tag("Speed", "DB", "word", 1, 2, data_type="int16", scale=0.1, unit="rpm")
        self.assertEqual((t["db_number"], t["multiplier"], t["unit"]), (1, 0.1, "rpm"))

    def test_refusals_are_plain(self):
        for bad, text in ((dict(name=""), "needs a name"), (dict(width="byte"), "single bytes"),
                          (dict(width="word", data_type="float32_be"), "does not fit"), (dict(area="DB", db=0), "DB number")):
            kw = dict(name="x", area="M", width="word", db=1, byte=0, bit=0)
            kw.update(bad)
            with self.assertRaises(ValueError) as ctx:
                a.build_tag(**kw)
            self.assertIn(text, str(ctx.exception))

    def test_data_types_and_readability(self):
        self.assertEqual(a.data_types_for("dword")[0], "int32")
        self.assertFalse(a.readable("byte"))
        self.assertEqual(a.data_types_for("byte"), [])


if __name__ == "__main__":
    unittest.main()

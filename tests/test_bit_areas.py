"""S7 I/Q rows are bit-packed digital signals: no float32 reading, bits shown, bit tags added."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
import unittest
import protoprobe as pp


class BitAreaTests(unittest.TestCase):
    def test_word_bits_big_endian(self):
        # byte 4 = 0b00000100 (bit 2), byte 5 = 0b10000000 (bit 7)
        self.assertEqual(pp.s7_word_bits("I", 4, 0x0480), [(4, 2), (5, 7)])

    def test_format_bits_marks_changed(self):
        self.assertEqual(pp.format_bits("I", [(0, 3), (1, 0)], [(1, 0)]), "I0.3, I1.0*")

    def test_is_bit_packed(self):
        self.assertTrue(pp.is_bit_packed_area("s7comm", {"area": "I"}))
        self.assertTrue(pp.is_bit_packed_area("s7comm", {"area": "Q"}))
        self.assertFalse(pp.is_bit_packed_area("s7comm", {"area": "DB"}))
        self.assertFalse(pp.is_bit_packed_area("s7comm", {"area": "M"}))
        self.assertFalse(pp.is_bit_packed_area("modbus_tcp", {"area": "I"}))



class ScanIntegration(unittest.TestCase):
    def test_input_area_rows_have_bits_and_no_float_guess(self):
        import test_quiet_s7_scan as q
        msgs, _ = q.scan("I", 0, 20, snapshots=2)
        results = [d for k, d in msgs if k == "results"][0][2]
        self.assertTrue(results)
        for r in results:
            self.assertTrue(r.get("bit_area"))
            self.assertIsNone(r.get("float32_guess"))
            self.assertNotIn("candidates_32bit", r)
        r4 = [r for r in results if r["name"] == "I4"][0]   # stand-in returns bytes [4, 0] -> I4.2 on
        self.assertEqual(r4["bits_on"], [(4, 2)])


if __name__ == "__main__":
    unittest.main()

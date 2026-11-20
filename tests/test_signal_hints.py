import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import signal_hints as sh  # noqa: E402


class Words(unittest.TestCase):
    def test_counter(self):
        h = sh.classify_word([10, 10, 11, 13])
        self.assertEqual((h.kind, h.oee, h.predictive), ("counter", "production count", "usage count"))

    def test_measurement(self):
        h = sh.classify_word([201, 205, 203, 207])
        self.assertEqual((h.kind, h.predictive), ("measurement", "trend"))

    def test_constant_and_idle(self):
        self.assertEqual(sh.classify_word([7, 7, 7]).kind, "constant")
        self.assertEqual(sh.classify_word([0, 0, 0]).kind, "idle")

    def test_countdown(self):
        self.assertEqual(sh.classify_word([9, 8, 8, 5]).kind, "countdown")

    def test_flags(self):
        self.assertEqual(sh.classify_word([0x0100, 0x0300, 0x0100]).kind, "flags")

    def test_too_few_snapshots(self):
        self.assertEqual(sh.classify_word([5]).kind, "unknown")
        self.assertEqual(sh.classify_word(["a", "b"]).kind, "unknown")


class Bits(unittest.TestCase):
    def test_state_change(self):
        h = sh.classify_bits({(0, 2): [0, 0, 1, 1, 1]})
        self.assertEqual((h.kind, h.oee), ("state", "run state"))

    def test_blink(self):
        self.assertEqual(sh.classify_bits({(0, 5): [0, 1, 0, 1, 0]}).kind, "blink")

    def test_blink_and_state_together(self):
        h = sh.classify_bits({(0, 5): [0, 1, 0, 1, 0], (0, 1): [0, 0, 0, 1, 1]})
        self.assertEqual(h.kind, "state")
        self.assertIn("blink", h.text)

    def test_nothing_changed(self):
        self.assertEqual(sh.classify_bits({}).kind, "idle")


class Filter(unittest.TestCase):
    def test_matches(self):
        counter = sh.classify_word([1, 2, 3])
        self.assertTrue(sh.matches(counter, "oee") and sh.matches(counter, "predictive") and sh.matches(counter, "all"))
        self.assertFalse(sh.matches(sh.classify_word([0, 0]), "oee"))
        self.assertFalse(sh.matches(None, "predictive"))


if __name__ == "__main__":
    unittest.main()


class ForScanRows(unittest.TestCase):
    def test_rows_from_a_real_scan_get_a_hint_and_the_text_names_oee_and_predictive(self):
        sys.path.insert(0, os.path.dirname(__file__))
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "edge-gateway"))
        import protoprobe as pp
        counter = {"values": [10, 11, 13], "tag": {"area": "M"}}
        h = pp.hint_for_result(counter)
        self.assertEqual(h.kind, "counter")
        text = pp.hint_text(h)
        self.assertIn("OEE: production count", text)
        self.assertIn("predictive: usage count", text)
        self.assertEqual(pp.hint_for_result({"bit_area": True, "bits_on": [], "bits_series": {}}).kind, "idle")
        self.assertEqual(pp.hint_for_result({"bit_area": True, "bits_on": [(0, 1)], "bits_series": {}}).kind, "steady_on")
        self.assertEqual(pp.hint_text(None), "")

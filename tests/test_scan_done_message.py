"""A scan that reads nothing must say so (with the reason), not just 'Done' over an empty table."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import protoprobe as pp  # noqa: E402


class ScanDone(unittest.TestCase):
    def setUp(self):
        self.app = pp.App()
        self.app.update()

    def tearDown(self):
        self.app.destroy()

    def pump(self, *messages):
        self.app.start_background_scan(lambda *a: None, ())          # resets the per-scan notes; the thread does nothing
        for m in messages:
            self.app.scan_out_queue.put(m)
        self.app._scan_pump()
        self.app.update()

    def test_nothing_read_says_so_and_keeps_the_reason(self):
        self.pump(("status", "[D (0-100)] failed: no answer from the PLC (timeout). Usual causes: a wrong IP or port"), ("job_done", None))
        self.assertEqual(self.app.scan_status_text, "Done -- nothing could be read")
        self.assertIn("no answer from the PLC", self.app.scan_problem_text)
        self.assertIn("Read a Value", self.app.scan_problem_text)
        self.assertEqual(self.app.screens["scan_results"].problem_label.cget("text"), self.app.scan_problem_text)

    def test_nothing_read_without_a_reported_reason_still_explains_the_next_step(self):
        self.pump(("job_done", None))
        self.assertEqual(self.app.scan_status_text, "Done -- nothing could be read")
        self.assertIn("Connection doctor", self.app.scan_problem_text)

    def test_rows_found_is_plain_done(self):
        row = {"name": "D1", "tag": {"name": "D1"}, "changed": False, "latest": 1}
        self.pump(("results", ("c", "job", [row])), ("job_done", None))
        self.assertEqual(self.app.scan_status_text, "Done")
        self.assertEqual(self.app.scan_problem_text, "")

    def test_rows_found_but_some_reads_failed_says_so(self):
        row = {"name": "D1", "tag": {"name": "D1"}, "changed": False, "latest": 1}
        self.pump(("status", "[D] 40 reads failed; most common: address not allowed"), ("results", ("c", "job", [row])), ("job_done", None))
        self.assertEqual(self.app.scan_status_text, "Done -- some reads failed")
        self.assertIn("address not allowed", self.app.scan_problem_text)

    def test_a_new_scan_clears_the_old_message(self):
        self.pump(("job_done", None))
        self.assertTrue(self.app.scan_problem_text)
        self.app.start_background_scan(lambda *a: None, ())
        self.assertEqual(self.app.scan_problem_text, "")


if __name__ == "__main__":
    unittest.main()

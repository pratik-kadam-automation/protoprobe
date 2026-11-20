import csv
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import candidates as cd  # noqa: E402
import signal_hints as sh  # noqa: E402

RESULTS = [
    {"conn_name": "bench", "tag": {"area": "M", "start_byte": 22, "data_type": "int16"}, "values": [10, 11, 13], "latest": 13},
    {"conn_name": "bench", "tag": {"area": "M", "start_byte": 30, "data_type": "int16"}, "values": [5, 5, 5], "latest": 5},
    {"conn_name": "bench", "tag": {"area": "Q", "start_byte": 0, "data_type": "int16"}, "bit_area": True,
     "bits_series": {(0, 1): [0, 0, 1, 1, 1], (0, 5): [0, 1, 0, 1, 0]}},
]


def rows(**kw):
    return cd.rows_from_results(RESULTS, lambda name: "s7comm", lambda p, t: f"{t['area']}{t['start_byte']}",
                                lambda r: sh.classify_word(r["values"]) if r.get("values") else None,
                                lambda series: sh.classify_bits(series), **kw)


class Rows(unittest.TestCase):
    def test_candidates_only_by_default(self):
        got = rows()
        names = [r["suggested_name"] for r in got]
        self.assertIn("M22", names)                      # a counter: OEE and predictive
        self.assertIn("Q0_1", names)                     # a bit that changed a few times: run state
        self.assertNotIn("M30", names)                   # a constant is a setting, not a candidate for either
        self.assertNotIn("Q0_5", names)                  # a bit that blinks at every snapshot: ignore

    def test_everything_when_asked(self):
        self.assertEqual(len(rows(only_candidates=False)), 4)

    def test_roles_and_uses(self):
        by = {r["suggested_name"]: r for r in rows()}
        self.assertEqual((by["M22"]["role"], by["M22"]["oee_use"], by["M22"]["predictive_use"]), ("counter", "production count", "usage count"))
        self.assertEqual((by["Q0_1"]["role"], by["Q0_1"]["oee_use"]), ("state", "run state"))
        self.assertEqual(by["Q0_1"]["data_type"], "bit")

    def test_confidence_is_never_high(self):
        for r in rows():
            self.assertTrue(r["confidence"].startswith(("low", "medium")))
            self.assertIn("confirm on the machine", r["confidence"])


class Files(unittest.TestCase):
    def test_csv_has_the_columns_and_one_line_per_row(self):
        text = cd.to_csv(rows())
        back = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(list(back[0].keys()), cd.COLUMNS)
        self.assertEqual(len(back), len(rows()))

    def test_json_names_its_kind(self):
        data = json.loads(cd.to_json(rows(), "1.2"))
        self.assertEqual((data["kind"], data["schema"], data["app_version"]), ("protoprobe-candidates", 1, "1.2"))
        self.assertEqual(len(data["candidates"]), len(rows()))


if __name__ == "__main__":
    unittest.main()


class ExportFromTheScreen(unittest.TestCase):
    def test_export_writes_the_shown_rows(self):
        import tempfile
        import tkinter as tk
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "edge-gateway"))
        import protoprobe as pp
        saved = tk._default_root
        tk._default_root = None
        try:
            app = pp.App()
        except tk.TclError as exc:
            tk._default_root = saved
            self.skipTest(f"no display: {exc}")
        try:
            app.connections["bench"] = pp.Connection("bench", "s7comm", {"ip": "192.0.2.1", "rack": 0, "slot": 1})
            for r in RESULTS:
                app.scan_results.append(dict(r, job_label="M", name="x", changed=True, latest=r.get("latest", 0), type_label="M",
                                             bits_on=[], bits_changed=sorted(r.get("bits_series", {})),
                                             raw_value=0, candidates_32bit={}, float32_guess=None, float32_pair_with=None))
            screen = app.screens["scan_results"]
            screen.refresh()
            path = os.path.join(tempfile.mkdtemp(), "c.csv")
            old_dialog, old_info = pp.filedialog.asksaveasfilename, pp.messagebox.showinfo
            pp.filedialog.asksaveasfilename = lambda **k: path
            pp.messagebox.showinfo = lambda *a, **k: None
            try:
                self.assertEqual(screen.export_suggestions(), path)
            finally:
                pp.filedialog.asksaveasfilename, pp.messagebox.showinfo = old_dialog, old_info
            back = list(csv.DictReader(open(path, encoding="utf-8")))
            self.assertIn("M22", [r["suggested_name"] for r in back])
            self.assertIn("Q0_1", [r["suggested_name"] for r in back])
        finally:
            app.destroy()
            tk._default_root = saved

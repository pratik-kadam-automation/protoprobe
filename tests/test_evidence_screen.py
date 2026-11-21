"""The Evidence screen (protoprobe.EvidenceScreen): what it builds from the scan and the questions, what it refuses to save, and what the dashboard
will read. All data is synthetic."""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(__file__)
sys.path[:0] = [os.path.join(HERE, ".."), os.path.join(HERE, "..", "edge-gateway")]
import evidence as ev  # noqa: E402


class Excerpt(unittest.TestCase):
    def test_numbers_only_thinned_and_first_last_kept(self):
        series = list(range(1000))
        x = ev.raw_excerpt(series, 60)
        self.assertEqual(len(x["v"]), 60)
        self.assertEqual((x["v"][0], x["v"][-1]), (0, 999))
        self.assertEqual(x["t"], [])
        self.assertEqual(ev.raw_excerpt([1, "x", None, float("nan"), 2.5])["v"], [1, 2.5])
        self.assertIsNone(ev.raw_excerpt([5]))                       # one point says nothing
        self.assertIsNone(ev.raw_excerpt([1, 2], 0))

    def test_build_adds_the_excerpt_only_when_asked(self):
        row = {"connection": "bench", "address": "M22", "data_type": "int16", "role": "counter", "oee_use": "production count", "predictive_use": "",
               "evidence": "only went up", "snapshots": 4, "first_value": 1, "last_value": 4, "series": [1, 2, 3, 4]}
        p, _ = ev.build({}, [row], raw_points=60)
        self.assertEqual(p["tags"][0]["raw"], {"t": [], "v": [1, 2, 3, 4]})
        self.assertEqual(p["privacy"]["raw_excerpt_points"], 60)
        p, _ = ev.build({}, [row])
        self.assertNotIn("raw", p["tags"][0])

    def test_leaks_finds_what_was_left(self):
        self.assertEqual(ev.leaks('{"a": "fine"}', ["Acme"]), [])
        found = " ".join(ev.leaks('ip 10.1.2.3 mail a@b.example Acme', ["acme"]))
        for part in ("IP address", "e-mail", "acme"):
            self.assertIn(part, found)


class Screen(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        import protoprobe as pp
        self.tk, self.pp = tk, pp
        self._saved_root = tk._default_root
        tk._default_root = None
        try:
            self.app = pp.App()
        except tk.TclError as exc:
            tk._default_root = self._saved_root
            self.skipTest(f"no display: {exc}")
        self.app.connections["Acme-Line3"] = pp.Connection("Acme-Line3", "s7comm", {"ip": "192.0.2.9", "rack": 0, "slot": 1})
        tag = {"name": "M22", "area": "M", "db_number": 0, "start_byte": 22, "data_type": "int16"}
        self.app.scan_results[:] = [{"conn_name": "Acme-Line3", "job_label": "j", "name": "M22", "type_label": "word", "changed": True, "raw_value": 99, "latest": 99,
                                     "tag": tag, "values": list(range(10, 40))}]
        com = self.app.screens["commission"]
        for k, v in {"client": "Acme Mills", "machine_name": "Line 3 press", "machine_id": "MC_ACME_001", "machine_type": "press", "production_type": "discrete",
                     "machine_use": "stamps brackets for Acme Mills", "plc_make": "siemens"}.items():
            if k in com.answer_vars:
                com.answer_vars[k].set(v)
        self.screen = self.app.screens["evidence"]

    def tearDown(self):
        self.app.destroy()
        self.tk._default_root = self._saved_root

    def test_preview_has_the_answers_aliases_and_no_identifier(self):
        self.screen.refresh()
        text = self.screen.text.get("1.0", "end")
        payload = json.loads(text)
        self.assertEqual(payload["kind"], "protoprobe-evidence")
        self.assertEqual(payload["tags"][0]["alias"], "T001")
        self.assertEqual(payload["machine"]["machine_type"], "press")
        for secret in ("Acme", "Line 3", "MC_ACME", "192.0.2.9", "M22", "Line3"):
            self.assertNotIn(secret, text, secret)
        self.assertEqual(payload["tags"][0]["raw"]["v"][0], 10)
        rows = [self.screen.tree.item(i)["values"] for i in self.screen.tree.get_children()]
        self.assertEqual(rows[0][0], "T001")
        self.assertIn("M22", str(rows[0][1]))                         # the address is shown on screen only

    def test_saving_needs_the_confirmation_and_refuses_a_leak(self):
        from tkinter import messagebox
        errors = []
        orig = messagebox.showerror
        messagebox.showerror = lambda *a, **k: errors.append(a)
        try:
            self.screen.refresh()
            path = os.path.join(tempfile.mkdtemp(), "e.json")
            self.assertEqual(str(self.screen.save_btn.cget("state")), "disabled")
            self.assertIsNone(self.screen.save(path))                 # not confirmed
            self.assertFalse(os.path.exists(path))
            self.screen.approved.set(True)
            self.screen._sync_buttons()
            self.assertEqual(str(self.screen.save_btn.cget("state")), "normal")
            self.screen.text.insert("end", "\n")
            # someone edits the text and adds a name: refused
            text = json.loads(self.screen.text.get("1.0", "end"))
            text["note"] = "from Acme Mills at 10.0.0.7"
            self.screen.text.delete("1.0", "end")
            self.screen.text.insert("1.0", json.dumps(text))
            self.assertIsNone(self.screen.save(path))
            self.assertTrue(errors and "should be removed" in errors[0][1])
            self.assertFalse(os.path.exists(path))
            self.screen.refresh()
            self.screen.approved.set(True)
            self.assertEqual(self.screen.save(path), path)
            self.assertEqual(json.load(open(path, encoding="utf-8"))["tags"][0]["alias"], "T001")
        finally:
            messagebox.showerror = orig

    def test_raw_excerpt_can_be_switched_off_and_extra_words_removed(self):
        self.screen.include_raw.set(False)
        self.screen.extra_names.insert(0, "brackets")
        self.screen.refresh()
        payload = json.loads(self.screen.text.get("1.0", "end"))
        self.assertNotIn("raw", payload["tags"][0])
        self.assertNotIn("brackets", json.dumps(payload))

    def test_documents_from_the_import_are_used_and_redacted(self):
        self.app.evidence_docs = {"%MW22": {"name": "Parts_Acme", "comment": "good parts, see 10.0.0.1"}}
        self.screen.refresh()
        tag = json.loads(self.screen.text.get("1.0", "end"))["tags"][0]
        self.assertEqual(tag["documented_name"], "Parts_[name]")
        self.assertIn("[ip]", tag["documented_comment"])

    def test_the_screen_is_in_the_menu(self):
        self.assertIn("evidence", self.app.screens)
        self.assertIn(("evidence", "Evidence"), self.pp.NAV_ITEMS)


if __name__ == "__main__":
    unittest.main()

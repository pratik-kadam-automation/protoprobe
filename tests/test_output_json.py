"""The copy-and-paste output JSON built from ticked tags (output_json.py) and its tick controls on Live values."""
import os
import sys
import types
import unittest

HERE = os.path.dirname(__file__)
sys.path[:0] = [os.path.join(HERE, ".."), os.path.join(HERE, "..", "edge-gateway")]
import output_json as oj  # noqa: E402


def conn(tags, values):
    return types.SimpleNamespace(tags=tags, latest_values=values)


IDS = {"deployment_id": "DP_ACME_001", "gateway_id": "GW_ACME_001_000001", "machine_id": "MC_ACME_001"}


class Build(unittest.TestCase):
    def test_only_ticked_tags_appear_under_their_chosen_name(self):
        c = conn([{"name": "tag1", "output": "machine status"}, {"name": "tag2"}], {"tag1": 1, "tag2": 9})
        payload, warnings = oj.build_output(IDS, {"plc": c}, "values", now=1700000000)
        self.assertEqual(payload, {**IDS, "timestamp": 1700000000, "data": {"machine status": 1}})
        self.assertEqual(warnings, [])
        self.assertEqual(list(payload)[:3], ["deployment_id", "gateway_id", "machine_id"])

    def test_names_only_mode_is_a_template(self):
        c = conn([{"name": "tag1", "output": "machine status"}], {})
        payload, _ = oj.build_output(IDS, {"plc": c}, "names", now=1)
        self.assertEqual(payload["data"], {"machine status": "machine status"})

    def test_warnings_say_what_is_missing(self):
        c = conn([{"name": "a", "output": "x"}, {"name": "b", "output": "x"}], {"a": 1})
        payload, warnings = oj.build_output({}, {"plc": c}, "values", now=1)
        text = " ".join(warnings)
        self.assertIn("more than one tag", text)
        self.assertIn("No value yet", text)
        self.assertIn("deployment_id is empty", text)
        _, none = oj.build_output(IDS, {"plc": conn([{"name": "a"}], {})}, "values", now=1)
        self.assertTrue(any("No tag is ticked" in w for w in none))


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
        self.conn = pp.Connection("plc", "modbus_tcp", {"ip": "192.0.2.1", "port": 502, "unit_id": 1})
        self.conn.tags = [{"name": "tag1", "address": 1, "data_type": "uint16"}, {"name": "tag2", "address": 2, "data_type": "uint16"}]
        self.conn.latest_values = {"tag1": 1, "tag2": 7}
        self.app.connections["plc"] = self.conn
        self.live = self.app.screens["live_values"]
        self.live.refresh()

    def tearDown(self):
        self.app.destroy()
        self.tk._default_root = self._saved_root

    def test_tick_name_and_json_text(self):
        self.live.live_tree.selection_set("plc::tag1")
        self.live.include_selected(name="machine status")
        self.assertEqual(self.conn.tags[0]["output"], "machine status")
        self.assertIn("machine status", self.live.live_tree.item("plc::tag1")["values"][0])
        self.assertTrue(self.live.live_tree.selection())           # selection survives the refresh
        self.live.out_ids["machine_id"].set("MC_ACME_001")
        text = self.live.out_text.get("1.0", "end")
        self.assertIn('"machine status": 1', text)
        self.assertIn('"machine_id": "MC_ACME_001"', text)
        self.assertNotIn("tag2", text)
        self.live.out_mode.set("names"); self.live.render_output()
        self.assertIn('"machine status": "machine status"', self.live.out_text.get("1.0", "end"))
        self.live.remove_selected()
        self.assertNotIn("output", self.conn.tags[0])


if __name__ == "__main__":
    unittest.main()

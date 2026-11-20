import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import evidence as ev  # noqa: E402

ROWS = [{"connection": "bench", "address": "M22", "data_type": "int16", "role": "counter", "oee_use": "production count",
         "predictive_use": "usage count", "evidence": "only went up: a counter; see 10.0.0.5", "snapshots": 30, "first_value": 10, "last_value": 99}]
ANSWERS = {"client": "Acme Mills", "gateway_id": "GW_ACME_001_000001", "machine_name": "Line 3 press", "plc_ip": "192.0.2.77", "notes": "call Raj at raj@acme.example",
           "machine_type": "press", "production_type": "discrete", "machine_use": "stamps brackets for Acme Mills at Plant 7", "plc_make": "siemens",
           "plc_model": "S7-1200", "hmi": "yes", "hmi_make": "", "ideal_cycle_s": "14"}


class Redact(unittest.TestCase):
    def test_identifiers_are_replaced(self):
        out = ev.redact("PLC at 192.0.2.5, mail raj@acme.example, see http://plant.local/x, mac 00:1A:2B:3C:4D:5E, Acme Mills", names=["acme mills"])
        for secret in ("192.0.2.5", "raj@acme.example", "plant.local", "00:1A:2B", "Acme"):
            self.assertNotIn(secret, out)
        self.assertIn("[ip]", out)
        self.assertIn("[name]", out)

    def test_longer_names_are_replaced_first(self):
        self.assertEqual(ev.redact("Acme Mills Ltd", names=["Acme", "Acme Mills"]), "[name] Ltd")


class Build(unittest.TestCase):
    def setUp(self):
        self.payload, self.aliases = ev.build(ANSWERS, ROWS, docs={"M22": {"name": "Part_count", "comment": "parts made at Plant 7 by Acme"}},
                                              hide_names=["Acme Mills", "Plant 7", "Acme"])
        self.text = ev.preview(self.payload)

    def test_no_identifier_survives_anywhere_in_the_payload(self):
        for secret in ("Acme", "GW_ACME", "Line 3", "192.0.2.77", "raj@", "10.0.0.5", "Plant 7", "bench", "M22"):
            self.assertNotIn(secret, self.text, secret)

    def test_tags_are_aliased_and_the_map_stays_outside(self):
        self.assertEqual(self.payload["tags"][0]["alias"], "T001")
        self.assertEqual(self.aliases["T001"], {"connection": "bench", "address": "M22"})
        self.assertNotIn("address", json.dumps(self.payload["tags"][0]))

    def test_machine_facts_a_model_needs_are_kept(self):
        m = self.payload["machine"]
        self.assertEqual((m["machine_type"], m["production_type"], m["plc_model"], m["ideal_cycle_s"]), ("press", "discrete", "S7-1200", "14"))
        self.assertNotIn("client", m)

    def test_documentation_text_is_kept_but_redacted(self):
        tag = self.payload["tags"][0]
        self.assertEqual(tag["documented_name"], "Part_count")
        self.assertIn("[name]", tag["documented_comment"])

    def test_it_says_what_is_missing_and_what_it_left_out(self):
        self.assertIn("machine_make", self.payload["missing_answers"])
        self.assertIn("hmi_make", self.payload["missing_answers"])
        self.assertFalse(self.payload["privacy"]["raw_recordings_included"])


class Missing(unittest.TestCase):
    def test_questions_come_with_a_reason(self):
        got = ev.missing_for_analysis({"machine_type": "press"})
        self.assertNotIn("machine_type", [m["key"] for m in got])
        self.assertTrue(all(m["question"] and m["why"] for m in got))

    def test_nothing_missing_when_all_answered(self):
        self.assertEqual(ev.missing_for_analysis({k: "x" for k, _q, _w in ev.ANALYSIS_FIELDS}), [])


if __name__ == "__main__":
    unittest.main()

"""The evidence file is a contract with the dashboard (industrial-backend, aiReview.js validateEvidence). The same synthetic sample lives in both repos:
tests/fixtures/evidence-sample.json here and test/fixtures/protoprobe-evidence-sample.json there. Change the builder, regenerate it (python tests/test_evidence_contract.py --write),
copy it to the backend, and run both test suites."""
import json
import os
import sys
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
import evidence as ev  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "evidence-sample.json")
ANSWERS = {"client": "acme", "machine_name": "Line 3 press", "plc_ip": "192.0.2.10", "machine_type": "press", "production_type": "discrete",
           "machine_use": "stamps brackets", "machine_make": "Example Machines", "machine_model": "EP-200", "plc_make": "siemens", "plc_model": "S7-1200",
           "hmi": "yes", "ideal_cycle_s": "14"}
ROWS = [
    {"connection": "bench", "address": "MD60", "data_type": "int32", "role": "counter", "oee_use": "production count", "predictive_use": "usage count",
     "evidence": "only went up: a counter", "snapshots": 8, "first_value": 100, "last_value": 107, "series": [100, 101, 102, 103, 104, 105, 106, 107]},
    {"connection": "bench", "address": "Q0.5", "data_type": "bit", "role": "state", "oee_use": "run state", "predictive_use": "",
     "evidence": "changed a few times: a run state", "snapshots": 6, "first_value": 0, "last_value": 1, "series": [0, 0, 1, 1, 0, 1]},
    {"connection": "bench", "address": "MW10", "data_type": "int16", "role": "measurement", "oee_use": "", "predictive_use": "analog value, trend",
     "evidence": "moves up and down: an analog value", "snapshots": 5, "first_value": 70, "last_value": 72, "series": [70, 74, 69, 73, 72]}]


def sample():
    payload, _ = ev.build(ANSWERS, ROWS, docs={"MD60": {"name": "Part_count", "comment": "good parts"}}, hide_names=["acme"], app_version="0.3.2", raw_points=60)
    return payload


class Contract(unittest.TestCase):
    def test_the_fixture_is_what_the_builder_makes(self):
        with open(FIXTURE, encoding="utf-8") as f:
            self.assertEqual(json.load(f), sample(), "regenerate with: python tests/test_evidence_contract.py --write (and copy to the backend)")

    def test_nothing_identifying_is_in_it(self):
        text = json.dumps(sample())
        for secret in ("acme", "Line 3", "192.0.2.10", "MD60", "Q0.5", "bench"):
            self.assertNotIn(secret, text, secret)


if __name__ == "__main__":
    if "--write" in sys.argv:
        os.makedirs(os.path.dirname(FIXTURE), exist_ok=True)
        with open(FIXTURE, "w", encoding="utf-8", newline="\n") as f:
            json.dump(sample(), f, indent=2, ensure_ascii=False)
            f.write("\n")
        print("written", FIXTURE)
    else:
        unittest.main()

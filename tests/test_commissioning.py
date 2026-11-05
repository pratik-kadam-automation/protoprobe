"""Engine tests: a simulated PLC with hidden tags and decoys, no hardware, no Tk."""
import json
import os
import random
import struct
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import commissioning as cm

T0 = 1793955600          # 2026-11-06T09:00:00Z


def words_of_float(value, order):
    """The two registers a PLC would hold for a float32 in the given byte order."""
    b = struct.pack(">f", value)
    idx = {"ABCD": (0, 1, 2, 3), "CDAB": (2, 3, 0, 1), "BADC": (1, 0, 3, 2), "DCBA": (3, 2, 1, 0)}[order]
    # inverse of commissioning._order: the stored bytes are such that reading them in `order` gives b
    stored = [0, 0, 0, 0]
    for out_pos, src in enumerate(idx):
        stored[src] = b[out_pos]
    return (stored[0] << 8) | stored[1], (stored[2] << 8) | stored[3]


class FakePLC:
    """An FX5U-like machine: noise everywhere, plus hidden tags at known addresses."""
    def __init__(self, seed=7):
        rnd = random.Random(seed)
        self.base = {a: rnd.randrange(0, 65536) for a in range(0, 2000)}
        for a in range(0, 2000, 7):          # lots of small plausible numbers, to tempt coincidences
            self.base[a] = rnd.randrange(0, 60)

    def memory(self, speed, mould, run, billets, temp):
        w = dict(self.base)
        w[100], w[101] = words_of_float(speed, "CDAB")          # float32, word-swapped
        raw = int(round(mould * 10))
        w[102] = raw & 0xFFFF                                    # int16 x0.1 (can be negative)
        w[50] = (w.get(50, 0) & ~(1 << 3)) | (run << 3)          # run = bit 3 of D50
        w[200] = billets                                         # uint16 counter
        w[300] = temp                                            # int16, scale 1
        return {"D": w}


SIG = {s["key"]: s for m in cm.MACHINES.values() for s in m["signals"]}
SIG["temp"] = {"drift": 0.001}


def moment(plc, at, phase, hmi_text, **state):
    hmi = {k: cm.hmi_reading(SIG.get(k, {}), v) for k, v in hmi_text.items()}
    return cm.Moment(T0 + at, hmi, plc.memory(**state), phase)


REGIONS = [cm.Region("D", "D", 0, 1999)]
ORDERS = cm.MAKES["mitsubishi"][2]


class Parse(unittest.TestCase):
    def test_hmi_value_and_tolerance(self):
        self.assertEqual(cm.parse_hmi_value("1.25")[0], 1.25)
        v, tol = cm.parse_hmi_value("1.25")
        self.assertAlmostEqual(tol, 0.005 + 0.005 * 1.25)
        self.assertEqual(cm.parse_hmi_value("41")[0], 41)
        self.assertEqual(cm.parse_hmi_value("1,8")[0], 1.8)               # decimal comma
        self.assertEqual(cm.parse_hmi_value("ON"), (1.0, 0.0))
        self.assertEqual(cm.parse_hmi_value("off"), (0.0, 0.0))
        for bad in ("", "abc", "nan", "inf"):
            with self.assertRaises(ValueError):
                cm.parse_hmi_value(bad)

    def test_float_interpretations(self):
        w0, w1 = words_of_float(1.25, "CDAB")
        got = {(d, o): v for d, o, _, v in cm.interpretations(w0, w1)}
        self.assertAlmostEqual(got[("float32", "CDAB")], 1.25)
        self.assertNotAlmostEqual(got[("float32", "ABCD")], 1.25)


class Find(unittest.TestCase):
    def setUp(self):
        self.plc = FakePLC()
        self.m1 = moment(self.plc, 0, "find", {"speed": "1.25", "mould": "3.2", "run": "ON", "billets": "41", "temp": "1520"},
                         speed=1.25, mould=3.2, run=1, billets=41, temp=1520)
        self.m2 = moment(self.plc, 600, "find", {"speed": "1.8", "mould": "-1.5", "run": "OFF", "billets": "42", "temp": "1590"},
                         speed=1.8, mould=-1.5, run=0, billets=42, temp=1590)

    def top(self, key, moments=None):
        return cm.find_numeric(moments or [self.m1, self.m2], key, REGIONS, ORDERS, kind=SIG.get(key, {}).get("kind", "number"))

    def test_each_hidden_tag_is_found_first_with_the_right_reading(self):
        c = self.top("speed")[0]
        self.assertEqual((c.addr, c.datatype, c.byte_order, c.scale), (100, "float32", "CDAB", 1))
        self.assertTrue(c.strong)
        c = self.top("mould")[0]
        self.assertEqual((c.addr, c.datatype, c.scale), (102, "int16", 0.1))     # negative value and scale both found
        c = self.top("temp")[0]
        self.assertEqual((c.addr, c.datatype, c.scale), (300, "int16", 1))
        c = self.top("billets")[0]
        self.assertEqual((c.addr, c.datatype, c.scale), (200, "uint16", 1))

    def test_two_readings_remove_the_coincidences_one_reading_leaves(self):
        one = self.top("billets", [self.m1])
        two = self.top("billets")
        self.assertGreater(len([c for c in one if c.strong]), 1, "a single value matches several addresses")
        self.assertEqual([c.addr for c in two if c.strong][:1], [200])
        self.assertLess(len([c for c in two if c.strong]), len([c for c in one if c.strong]) + 1)

    def test_a_missing_scale_is_detected(self):
        plc = FakePLC(3)
        a = moment(plc, 0, "find", {"billets": "41"}, speed=1, mould=0, run=1, billets=4100, temp=1)
        b = moment(plc, 600, "find", {"billets": "42"}, speed=1, mould=0, run=1, billets=4200, temp=1)
        c = cm.find_numeric([a, b], "billets", REGIONS, ORDERS, kind="counter")[0]
        self.assertEqual((c.addr, c.scale), (200, 0.01))

    def test_on_off_bit(self):
        found = cm.find_bits([self.m1, self.m2], "run", REGIONS)
        self.assertIn((50, 3), [(a, b) for _, a, b in found])
        # the HMI only ever showed ON: far too many bits qualify, so nothing is offered
        self.assertEqual(cm.find_bits([self.m1], "run", REGIONS), [])
        same = moment(self.plc, 100, "find", {"run": "ON"}, speed=1, mould=0, run=1, billets=1, temp=1)
        self.assertEqual(cm.find_bits([self.m1, same], "run", REGIONS), [])

    def test_decode_candidate_reads_a_later_memory(self):
        c = self.top("speed")[0]
        later = self.plc.memory(speed=0.9, mould=0, run=1, billets=50, temp=1500)
        self.assertAlmostEqual(cm.decode_candidate(later, c), 0.9, places=5)
        self.assertIsNone(cm.decode_candidate({"D": {}}, c))

    def test_nothing_matches_a_value_that_is_not_in_memory(self):
        a = moment(self.plc, 0, "find", {"x": "77777.5"}, speed=1, mould=0, run=1, billets=1, temp=1)
        b = moment(self.plc, 600, "find", {"x": "88888.5"}, speed=1, mould=0, run=1, billets=1, temp=1)
        self.assertEqual([c for c in cm.find_numeric([a, b], "x", REGIONS, ORDERS) if c.strong], [])

    def test_readings_too_close_together_are_flagged_as_unable_to_tell_candidates_apart(self):
        close = moment(self.plc, 600, "find", {"billets": "41"}, speed=1, mould=0, run=1, billets=41, temp=1)
        cands = cm.find_numeric([self.m1, close], "billets", REGIONS, ORDERS, kind="counter")
        self.assertFalse(cm.distinguishable([self.m1, close], "billets"))
        self.assertTrue(cands and all(not c.discriminating for c in cands))
        self.assertTrue(cm.distinguishable([self.m1, self.m2], "billets"))
        self.assertTrue(all(c.discriminating for c in self.top("billets")))

    def test_a_decoy_that_equals_the_value_in_only_one_reading_is_not_strong(self):
        # D75 and friends sit at a constant: 41 matches the first reading only, so they must lose to D200
        strong = [c for c in self.top("billets") if c.strong]
        self.assertEqual([(c.addr, c.datatype) for c in strong][:1], [(200, "uint16")])
        self.assertNotIn(75, [c.addr for c in strong])


class Gate(unittest.TestCase):
    def tag(self, evid, **kw):
        sig = cm.MACHINES["ccm"]["signals"][1]
        return cm.make_tag(sig, "D100", "float32", "CDAB", 1, evid, **kw)

    def ev(self, h, r, minutes, phase):
        return cm.evidence(h, r, T0 + minutes * 60, phase, "ela")

    def test_find_readings_alone_never_confirm(self):
        t = self.tag([self.ev(1.25, 1.25, 0, "find"), self.ev(1.8, 1.8, 10, "find")])
        self.assertEqual(t["confidence"], "probable")

    def test_two_fresh_matching_readings_confirm(self):
        t = self.tag([self.ev(1.25, 1.25, 0, "find"), self.ev(1.8, 1.8, 10, "find"), self.ev(1.5, 1.5, 20, "verify"), self.ev(0.9, 0.9, 30, "verify")])
        self.assertEqual(t["confidence"], "confirmed")

    def test_same_hmi_value_twice_does_not_confirm_a_measurement(self):
        t = self.tag([self.ev(1.5, 1.5, 20, "verify"), self.ev(1.5, 1.5, 30, "verify")])
        self.assertEqual(t["confidence"], "probable")

    def test_readings_seconds_apart_do_not_confirm(self):
        t = self.tag([cm.evidence(1.5, 1.5, T0, "verify", "e"), cm.evidence(0.9, 0.9, T0 + 10, "verify", "e")])
        self.assertEqual(t["confidence"], "probable")

    def test_a_fresh_reading_that_disagrees_blocks_confirmation(self):
        t = self.tag([self.ev(1.5, 1.5, 20, "verify"), self.ev(0.9, 9.0, 30, "verify")])
        self.assertEqual(t["confidence"], "probable")

    def test_no_evidence_is_unverified(self):
        self.assertEqual(self.tag([])["confidence"], "unverified")

    def test_a_setpoint_may_stay_the_same(self):
        sp = dict(cm.MACHINES["stenter"]["signals"][4])            # width setpoint
        t = cm.make_tag(sp, "D500", "uint16", "", 1, [self.ev(2400, 2400, 0, "verify"), self.ev(2400, 2400, 10, "verify")])
        self.assertEqual(t["confidence"], "confirmed")


class Questionnaire(unittest.TestCase):
    def test_defaults_follow_the_make(self):
        d = cm.derive({"plc_make": "mitsubishi"})
        self.assertEqual((d["protocol"], d["port"], d["byte_orders"][0]), ("mc", 5006, "CDAB"))
        d = cm.derive({"plc_make": "siemens"})
        self.assertEqual((d["protocol"], d["port"], d["byte_orders"][0]), ("s7", 102, "ABCD"))
        d = cm.derive({"plc_make": "modbus", "plc_port": "1502"})
        self.assertEqual((d["protocol"], d["port"]), ("modbus_tcp", 1502))

    def test_a_known_byte_order_goes_first(self):
        self.assertEqual(cm.derive({"plc_make": "mitsubishi", "byte_order_known": "DCBA"})["byte_orders"][0], "DCBA")

    def test_required_answers_and_conditional_questions(self):
        d = cm.derive({"plc_make": "mitsubishi", "machine_type": "ccm"})
        self.assertIn("client", d["missing"]); self.assertIn("machine_id", d["missing"]); self.assertIn("plc_ip", d["missing"])
        self.assertEqual(cm.derive({"client": "x", "machine_id": "M1", "plc_make": "modbus_rtu"})["missing"], [])   # serial: no IP asked
        q = {q.key: q for q in cm.QUESTIONS}
        self.assertTrue(q["counters_reset"].visible({"machine_type": "ccm"}))
        self.assertFalse(q["counters_reset"].visible({"machine_type": "cbr"}))
        self.assertTrue(q["rack"].visible({"plc_make": "siemens"})); self.assertFalse(q["rack"].visible({"plc_make": "mitsubishi"}))

    def test_scan_plan(self):
        self.assertEqual([(r.name, r.step) for r in cm.scan_plan({"plc_make": "siemens", "db_numbers": "1, 2,10"})], [("DB1", 2), ("DB2", 2), ("DB10", 2)])
        mb = cm.scan_plan({"plc_make": "modbus"})
        self.assertEqual([(r.name, r.enabled) for r in mb], [("holding", True), ("input", True), ("coil", False)])
        self.assertEqual(cm.scan_plan({"plc_make": "opcua"}), [])


class Pack(unittest.TestCase):
    def test_pack_from_a_session_is_valid_and_summarised(self):
        plc = FakePLC()
        answers = {"client": "mrent", "machine_id": "MC_CCM_001", "machine_name": "CCM strand 1", "machine_type": "ccm", "plc_make": "mitsubishi",
                   "plc_ip": "192.168.1.10", "prefix": "S1_", "counters_reset": "Billet_Count", "gateway_id": "GW_MREN_001_008830"}
        sig = cm.MACHINES["ccm"]["signals"][1]
        ev = [cm.evidence(1.25, 1.25, T0, "find", "ela"), cm.evidence(1.5, 1.5, T0 + 1200, "verify", "ela"), cm.evidence(0.9, 0.9, T0 + 1800, "verify", "ela")]
        pack = cm.build_pack(answers, [cm.make_tag(sig, "D100", "float32", "CDAB", 1, ev)], "ela")
        self.assertEqual(cm.validate_pack(pack), [])
        self.assertEqual(pack["machines"][0]["type"], "Continuous casting machine (CCM)")
        self.assertEqual(pack["machines"][0]["plc"]["prefix"], "S1_")
        self.assertEqual(pack["machines"][0]["batch"], {"resetTags": ["Billet_Count"]})
        self.assertEqual(cm.summary(pack), {"confirmed": 1, "probable": 0, "unverified": 0})
        json.dumps(pack)

    def test_validate_pack_catches_the_basics(self):
        self.assertTrue(cm.validate_pack({"schema": "x"}))
        good = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "golden-pack.json")))
        self.assertEqual(cm.validate_pack(good), [])
        for mut, text in [(lambda p: p["machines"][0]["tags"][0].update(datatype="int128"), "datatype"),
                          (lambda p: p["machines"][0]["tags"][0].update(name="bad name"), "name"),
                          (lambda p: p["machines"][0]["tags"][1].update(name="Casting_Speed"), "twice"),
                          (lambda p: p["machines"][0]["plc"].update(protocol="x"), "protocol"),
                          (lambda p: p["machines"][0]["tags"][0].update(scale=0), "scale")]:
            p = json.loads(json.dumps(good)); mut(p)
            self.assertTrue(any(text in e for e in cm.validate_pack(p)), text)

    def test_golden_pack_confidences_match_the_rule(self):
        """The same file is checked by the dashboard's tests: both sides must agree on every tag."""
        good = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "golden-pack.json")))
        for t in good["machines"][0]["tags"]:
            self.assertEqual(cm.judge(t), t["confidence"], t["name"])


if __name__ == "__main__":
    unittest.main()

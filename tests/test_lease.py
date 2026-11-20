import copy
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import lease as ls  # noqa: E402

FX = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "lease-sample.json")))
KEYS = FX["public_keys"]
KIT, ONPREM = FX["leases"]["field_kit"], FX["leases"]["on_prem"]


def at(text):
    return ls._ts(text)


class Verify(unittest.TestCase):
    def test_the_backend_signed_samples_verify_here(self):
        self.assertEqual(ls.verify(KIT, KEYS)["kind"], "field-kit")
        self.assertEqual(ls.verify(ONPREM, KEYS)["valid_until"], "2027-05-20T00:00:00.000Z")

    def test_canonical_text_is_the_one_the_backend_signed(self):
        # if this drifts from licence.js `canonical`, every signature fails: the verify test above is the real proof, this one names the rule
        self.assertEqual(ls.canonical({"b": 1, "a": {"d": [3, {"y": 2, "x": 1}], "c": "é"}}), '{"a":{"c":"é","d":[3,{"x":1,"y":2}]},"b":1}')

    def test_any_change_to_the_lease_breaks_the_signature(self):
        longer = copy.deepcopy(KIT)
        longer["lease"]["expires_at"] = "2030-01-01T00:00:00.000Z"
        with self.assertRaisesRegex(ls.LeaseError, "not valid"):
            ls.verify(longer, KEYS)

    def test_unknown_key_garbage_and_missing_parts(self):
        with self.assertRaisesRegex(ls.LeaseError, "unknown key"):
            ls.verify(KIT, {"other": KEYS["k1"]})
        with self.assertRaisesRegex(ls.LeaseError, "not a lease"):
            ls.verify({"lease": {}}, KEYS)
        with self.assertRaises(ls.LeaseError):
            ls.verify({"lease": KIT["lease"], "sig": "!!!notbase64"}, KEYS)


class Status(unittest.TestCase):
    def test_field_kit_runs_72_hours_warns_the_last_day_then_expires_then_is_due_for_removal(self):
        lease = KIT["lease"]
        self.assertEqual(ls.status(lease, at("2026-11-21T10:00:00Z"))["state"], "active")
        self.assertEqual(ls.status(lease, at("2026-11-22T12:00:00Z"))["state"], "warning")
        expired = ls.status(lease, at("2026-11-23T10:00:01Z"))
        self.assertEqual((expired["state"], expired["mode"]), ("expired", "disable"))
        self.assertIn("get a new kit", expired["message"])
        self.assertEqual(ls.status(lease, at("2026-11-26T10:00:01Z"))["state"], "removal_due")          # 3 grace days later

    def test_on_prem_warns_in_the_last_week_and_expires_into_buffer_only_and_never_into_removal(self):
        lease = ONPREM["lease"]                                                                          # 15 days from 20 Nov
        self.assertEqual(ls.status(lease, at("2026-11-27T10:00:00Z"))["state"], "active")                # more than a week left
        self.assertEqual(ls.status(lease, at("2026-11-30T10:00:00Z"))["state"], "warning")               # the last week
        e = ls.status(lease, at("2026-12-05T10:00:01Z"))
        self.assertEqual((e["state"], e["mode"]), ("expired", "buffer_only"))
        self.assertEqual(ls.status(lease, at("2027-03-01T00:00:00Z"))["state"], "expired")                # months later: still only expired
        self.assertIn("still collected", e["message"])

    def test_what_each_state_allows(self):
        warn = ls.status(KIT["lease"], at("2026-11-22T12:00:00Z"))
        self.assertTrue(all(ls.allowed(warn, a) for a in ("scan", "read", "script", "export")))
        kit_expired = ls.status(KIT["lease"], at("2026-11-24T00:00:00Z"))
        self.assertTrue(ls.allowed(kit_expired, "export") and ls.allowed(kit_expired, "renew"))
        self.assertFalse(any(ls.allowed(kit_expired, a) for a in ("scan", "read", "connect", "script", "buffer")))
        op_expired = ls.status(ONPREM["lease"], at("2026-12-06T00:00:00Z"))
        self.assertTrue(ls.allowed(op_expired, "buffer"))                                                # data keeps being held
        self.assertFalse(any(ls.allowed(op_expired, a) for a in ("analyse", "configure", "scan")))
        self.assertTrue(ls.allowed(op_expired, "export"))
        gone = ls.status(KIT["lease"], at("2026-12-30T00:00:00Z"))
        self.assertEqual(gone["state"], "removal_due")
        self.assertTrue(ls.allowed(gone, "export") and not ls.allowed(gone, "scan"))


class ClockTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, ls.STATE_FILE)

    def test_it_follows_the_wall_clock_going_forward(self):
        w = [1000.0]
        c = ls.Clock(self.path, wall=lambda: w[0], mono=lambda: w[0])
        self.assertEqual(c.now(), 1000.0)
        w[0] = 5000.0
        self.assertEqual(c.now(), 5000.0)

    def test_setting_the_clock_back_does_not_give_the_time_back(self):
        wall, mono = [10000.0], [0.0]
        c = ls.Clock(self.path, wall=lambda: wall[0], mono=lambda: mono[0])
        c.now()
        wall[0], mono[0] = 100.0, 30.0                 # someone sets the clock back to 100; 30 seconds really passed
        self.assertEqual(c.now(), 10030.0)             # time kept running from where it was, by the real seconds
        wall[0], mono[0] = 200.0, 90.0
        self.assertEqual(c.now(), 10090.0)

    def test_the_latest_time_survives_a_restart_and_a_clock_reset(self):
        c = ls.Clock(self.path, wall=lambda: 50000.0, mono=lambda: 0.0)
        c.now()
        c.flush()
        again = ls.Clock(self.path, wall=lambda: 100.0, mono=lambda: 0.0)       # restarted, and the PC clock reads 1970-ish
        self.assertGreaterEqual(again.now(), 50000.0)

    def test_an_unreadable_state_file_is_not_a_crash(self):
        open(self.path, "w").write("{ nope")
        c = ls.Clock(self.path, wall=lambda: 7.0, mono=lambda: 0.0)
        self.assertEqual(c.now(), 7.0)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.now = [at("2026-11-21T10:00:00Z")]
        self.clock = ls.Clock(None, wall=lambda: self.now[0], mono=lambda: self.now[0])

    def kit(self, container=KIT, keys=KEYS):
        json.dump({"trusted_keys": keys}, open(os.path.join(self.dir, "kit.json"), "w"))
        json.dump(container, open(os.path.join(self.dir, "lease.json"), "w"))
        return ls.Manager(self.dir, self.clock)

    def test_no_kit_file_means_an_unmanaged_copy_and_nothing_is_enforced(self):
        m = ls.Manager(self.dir, self.clock)
        self.assertIsNone(m.current())
        self.assertEqual(m.check("scan"), (True, ""))

    def test_a_valid_lease_allows_work_then_expiry_blocks_it_but_not_the_export(self):
        m = self.kit()
        self.assertTrue(m.check("scan")[0])
        self.now[0] = at("2026-11-24T00:00:00Z")
        ok, message = m.check("scan")
        self.assertFalse(ok)
        self.assertIn("expired", message)
        self.assertTrue(m.check("export")[0])

    def test_a_changed_lease_file_blocks_everything_but_the_export(self):
        bad = copy.deepcopy(KIT)
        bad["lease"]["expires_at"] = "2031-01-01T00:00:00.000Z"
        m = self.kit(bad)
        self.assertEqual(m.current()["state"], "invalid")
        self.assertFalse(m.check("scan")[0])
        self.assertTrue(m.check("export")[0])

    def test_a_missing_lease_file_is_invalid_not_a_crash(self):
        json.dump({"trusted_keys": KEYS}, open(os.path.join(self.dir, "kit.json"), "w"))
        self.assertEqual(ls.Manager(self.dir, self.clock).current()["state"], "invalid")

    def test_on_prem_after_expiry_still_buffers(self):
        m = self.kit(ONPREM)
        self.now[0] = at("2027-01-01T00:00:00Z")
        self.assertTrue(m.check("buffer")[0])
        self.assertFalse(m.check("configure")[0])


if __name__ == "__main__":
    unittest.main()

import json
import logging
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import diagnostics  # noqa: E402
import prefs  # noqa: E402


class Prefs(unittest.TestCase):
    def test_roundtrip_and_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "prefs.json")
            self.assertEqual(prefs.load(path), {"tour_done": False, "palette": ""})
            self.assertTrue(prefs.save(path, {"tour_done": True}))
            self.assertEqual(prefs.load(path), {"tour_done": True, "palette": ""})

    def test_damaged_or_newer_file_means_defaults_not_a_crash(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "prefs.json")
            with open(path, "w") as f:
                f.write("{not json")
            self.assertEqual(prefs.load(path), {"tour_done": False, "palette": ""})
            with open(path, "w") as f:
                json.dump({"schema": 99, "tour_done": True}, f)
            self.assertEqual(prefs.load(path), {"tour_done": False, "palette": ""})

    def test_unwritable_location_does_not_raise(self):
        # portable: a FILE used as the parent folder cannot be created on Linux or Windows (a fixed path like /proc/x is writable on Windows)
        with tempfile.TemporaryDirectory() as d:
            blocker = os.path.join(d, "a_file")
            with open(blocker, "w") as f:
                f.write("x")
            self.assertFalse(prefs.save(os.path.join(blocker, "sub", "prefs.json"), {"tour_done": True}))


def conn(**kw):
    base = dict(name="c1", protocol="mc_protocol_3e", plc_type="Q_TCP", enabled=True, tags=[1, 2], status="idle",
                interface={"ip": "10.20.30.40", "port": 1388, "transport": "udp", "password": "hunter2"})
    base.update(kw)
    return SimpleNamespace(**base)


class Diagnostics(unittest.TestCase):
    def test_hides_ip_and_mac_and_never_prints_secrets(self):
        text = diagnostics.build("ProtoProbe", "9.9.9", [conn()], ["x 10.20.30.40 aa:bb:cc:dd:ee:ff"], [("10.20.30.5", "255.255.255.0")], False, True)
        self.assertNotIn("10.20.30", text)
        self.assertNotIn("aa:bb", text)
        self.assertNotIn("hunter2", text)
        self.assertIn("x.x.x.x", text)
        self.assertIn("version: 9.9.9", text)
        self.assertIn("administrator: no", text)

    def test_can_keep_addresses(self):
        text = diagnostics.build("ProtoProbe", "1", [conn()], [], [], None, False)
        self.assertIn("10.20.30.40", text)
        self.assertNotIn("hunter2", text)
        self.assertIn("(none)", text)
        self.assertIn("(could not be read)", text)

    def test_ring_handler_keeps_only_the_latest(self):
        ring = diagnostics.RingHandler(size=3)
        log = logging.getLogger("test_ring")
        log.addHandler(ring)
        log.setLevel(logging.INFO)
        for i in range(5):
            log.info("line %d", i)
        log.removeHandler(ring)
        self.assertEqual(len(ring.lines), 3)
        self.assertIn("line 4", ring.lines[-1])


if __name__ == "__main__":
    unittest.main()

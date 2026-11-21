import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import arpscan as ar  # noqa: E402

WINDOWS = """
Interface: 192.0.2.10 --- 0x5
  Internet Address      Physical Address      Type
  192.0.2.1             00-11-22-33-44-55     dynamic
  192.0.2.51            a0-bb-cc-dd-ee-ff     dynamic
  192.0.2.60            00-00-00-00-00-00     invalid
  192.0.2.255           ff-ff-ff-ff-ff-ff     static
  224.0.0.22            01-00-5e-00-00-16     static
  239.255.255.250       01-00-5e-7f-ff-fa     static
"""
LINUX = """? (192.0.2.1) at 00:11:22:33:44:55 [ether] on eth0
? (192.0.2.77) at <incomplete> on eth0
? (192.0.2.78) at a:b:c:d:e:f [ether] on eth0
"""
IPNEIGH = "192.0.2.5 dev eth0 lladdr 00:11:22:33:44:66 REACHABLE\n192.0.2.6 dev eth0  FAILED\n"
OUI = "Registry,Assignment,Organization Name,Organization Address\nMA-L,A0BBCC,Siemens AG,Somewhere\nMA-L,001122,Example Co,Elsewhere\n"


class Parse(unittest.TestCase):
    def test_windows_table_drops_broadcast_multicast_and_invalid(self):
        got = ar.parse_arp_table(WINDOWS)
        self.assertEqual([e["ip"] for e in got], ["192.0.2.1", "192.0.2.51"])
        self.assertEqual(got[1]["mac"], "a0:bb:cc:dd:ee:ff")

    def test_linux_and_ip_neigh(self):
        self.assertEqual([e["ip"] for e in ar.parse_arp_table(LINUX)], ["192.0.2.1", "192.0.2.78"])
        self.assertEqual(ar.parse_arp_table(LINUX)[1]["mac"], "0a:0b:0c:0d:0e:0f")                 # short octets are padded
        self.assertEqual([e["ip"] for e in ar.parse_arp_table(IPNEIGH)], ["192.0.2.5"])

    def test_reading_uses_the_runner_and_never_raises(self):
        class Out:
            stdout = WINDOWS
        calls = []
        got = ar.read_arp_table(platform="win32", runner=lambda cmd, **kw: calls.append(cmd) or Out())
        self.assertEqual(calls[0], ["arp", "-a"])
        self.assertEqual(len(got), 2)
        self.assertEqual(ar.read_arp_table(runner=lambda *a, **k: (_ for _ in ()).throw(OSError("no arp"))), [])


class Vendors(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.path = os.path.join(d, "oui.csv")
        open(self.path, "w").write(OUI)
        self.oui = ar.load_oui(self.path)

    def test_oui_file_is_read_and_looked_up(self):
        self.assertEqual(ar.vendor_of("a0:bb:cc:dd:ee:ff", self.oui), "Siemens AG")
        self.assertEqual(ar.vendor_of("de:ad:be:ef:00:01", self.oui), "")

    def test_a_locally_administered_mac_has_no_vendor(self):
        self.assertEqual(ar.vendor_of("a2:bb:cc:dd:ee:ff", self.oui), "")        # the 0x02 bit of the first byte: virtual or randomised

    def test_no_file_is_not_an_error(self):
        self.assertEqual(ar.load_oui(os.path.join(tempfile.mkdtemp(), "nope.csv")), {})

    def test_hints_come_from_words_in_the_organisation_name(self):
        self.assertIn("102", ar.hint_for_vendor("Siemens AG"))
        self.assertIn("44818", ar.hint_for_vendor("Rockwell Automation"))
        self.assertEqual(ar.hint_for_vendor("Example Co"), "")


class Merge(unittest.TestCase):
    def test_arp_finds_devices_that_ignore_ping(self):
        oui = {"A0BBCC": "Siemens AG"}
        arp = ar.parse_arp_table(WINDOWS)
        rows = ar.merge({"192.0.2.1": True, "192.0.2.2": False}, arp, oui, "192.0.2.")
        by = {r["ip"]: r for r in rows}
        self.assertEqual(sorted(by), ["192.0.2.1", "192.0.2.51"])                  # .2 neither answered nor is in the table: not listed
        self.assertEqual(by["192.0.2.1"]["status"], "Answers ping")
        self.assertEqual(by["192.0.2.51"]["status"], "ARP only: does not answer ping")
        self.assertEqual(by["192.0.2.51"]["vendor"], "Siemens AG")
        self.assertIn("102", by["192.0.2.51"]["hint"])

    def test_without_a_vendor_list_it_says_so(self):
        rows = ar.merge({}, ar.parse_arp_table(WINDOWS), {}, "192.0.2.")
        self.assertEqual(rows[0]["vendor"], "(no vendor list)")

    def test_rows_are_in_numeric_order_and_stay_in_the_swept_range(self):
        arp = [{"ip": "192.0.2.100", "mac": "00:11:22:00:00:01"}, {"ip": "192.0.2.9", "mac": "00:11:22:00:00:02"}, {"ip": "10.9.9.9", "mac": "00:11:22:00:00:03"}]
        rows = ar.merge({}, arp, {}, "192.0.2.")
        self.assertEqual([r["ip"] for r in rows], ["192.0.2.9", "192.0.2.100"])


if __name__ == "__main__":
    unittest.main()


class OnTheScreen(unittest.TestCase):
    def test_the_network_tools_table_shows_arp_only_devices_with_vendor_and_hint(self):
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
            screen = app.screens["network"]
            screen._oui_table = {"A0BBCC": "Siemens AG"}
            screen._show_arp({"arp": ar.parse_arp_table(WINDOWS), "pinged": {"192.0.2.1": True}, "prefix": "192.0.2."})
            rows = [screen.ip_tree.item(i)["values"] for i in screen.ip_tree.get_children()]
            self.assertEqual([r[0] for r in rows], ["192.0.2.1", "192.0.2.51"])
            self.assertEqual(rows[1][1], "ARP only: does not answer ping")
            self.assertEqual(rows[1][3], "Siemens AG")
            self.assertIn("102", rows[1][4])
            self.assertEqual(screen._arp_extra, 1)
        finally:
            app.destroy()
            tk._default_root = saved

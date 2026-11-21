import unittest
from types import SimpleNamespace

import netcheck
from protoprobe import explain_exception

WIN = """
Windows IP Configuration

Ethernet adapter Ethernet:

   Connection-specific DNS Suffix  . :
   IPv4 Address. . . . . . . . . . . : 192.168.1.50
   Subnet Mask . . . . . . . . . . . : 255.255.255.0
   Default Gateway . . . . . . . . . : 192.168.1.1
   DNS Servers . . . . . . . . . . . : 8.8.8.8

Ethernet adapter Bad:

   IPv4 Address. . . . . . . . . . . : 10.0.0.7
   Subnet Mask . . . . . . . . . . . : 0.0.0.0
"""
LIN = "2: eth0    inet 192.168.1.50/24 brd 192.168.1.255 scope global eth0\n1: lo    inet 127.0.0.1/8 scope host lo\n"


class Parse(unittest.TestCase):
    def test_windows(self):
        self.assertEqual(netcheck.parse_ipconfig(WIN), [("192.168.1.50", "255.255.255.0"), ("10.0.0.7", "0.0.0.0")])

    def test_linux(self):
        self.assertEqual(netcheck.parse_ip_addr(LIN), [("192.168.1.50", "255.255.255.0")])

    def test_local_adapters_uses_runner(self):
        run = lambda cmd, **kw: SimpleNamespace(stdout=LIN)
        self.assertEqual(netcheck.local_adapters(runner=run, platform="linux"), [("192.168.1.50", "255.255.255.0")])
        self.assertEqual(netcheck.local_adapters(runner=lambda *a, **k: 1 / 0, platform="linux"), [])


class Hint(unittest.TestCase):
    def test_zero_mask(self):
        h = netcheck.network_hint("192.168.1.10", [("10.0.0.7", "0.0.0.0")])
        self.assertIn("0.0.0.0", h)
        self.assertIn("255.255.255.0", h)

    def test_other_network(self):
        h = netcheck.network_hint("172.16.5.10", [("192.168.1.50", "255.255.255.0")])
        self.assertIn("NOT in the same network", h)
        self.assertIn("172.16.5.xxx", h)

    def test_same_network(self):
        self.assertIn("not the problem", netcheck.network_hint("192.168.1.10", [("192.168.1.50", "255.255.255.0")]))

    def test_link_local_only(self):
        self.assertIn("no usable address", netcheck.network_hint("192.168.1.10", [("169.254.3.4", "255.255.0.0")]))

    def test_nothing_known(self):
        self.assertEqual(netcheck.network_hint("", [("1.2.3.4", "255.0.0.0")]), "")
        self.assertEqual(netcheck.network_hint("192.168.1.10", []), "")


class Explain(unittest.TestCase):
    def test_timeout_gets_hint(self):
        msg = explain_exception(TimeoutError(), "mc_protocol_3e", ip="172.16.5.10", adapters=[("192.168.1.50", "255.255.255.0")])
        self.assertIn("timeout", msg)
        self.assertIn("NOT in the same network", msg)

    def test_no_ip_no_hint(self):
        msg = explain_exception(TimeoutError(), "modbus_tcp", adapters=[("192.168.1.50", "255.255.255.0")])
        self.assertNotIn("same network", msg)


if __name__ == "__main__":
    unittest.main()

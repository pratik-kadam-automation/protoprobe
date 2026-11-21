import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import doctor  # noqa: E402

NET = [("192.168.3.93", "255.255.255.0")]


def res(t, p, status, text="", hexs=""):
    return {"transport": t, "port": p, "status": status, "end_code": None, "text": text, "reply_hex": hexs}


def go(results, alive=True, transport="tcp", port=1388, adapters=NET):
    return doctor.run("192.168.3.39", port, transport, adapters=adapters, ping_fn=lambda ip: alive, probe_fn=lambda ip, ports, timeout: results)


class Verdicts(unittest.TestCase):
    def test_udp_answers_tcp_connection(self):
        v = " ".join(go([res("udp", 1388, "ok", hexs="d000"), res("tcp", 1388, "timeout")])["verdict"])
        self.assertIn("UDP port 1388", v)
        self.assertIn("transport udp", v)

    def test_matching_settings_say_so(self):
        v = " ".join(go([res("tcp", 1388, "ok")])["verdict"])
        self.assertIn("what the connection is set to", v)

    def test_refused_by_plc_end_code(self):
        v = " ".join(go([res("tcp", 5000, "answered", "the PLC answered with end code 0xC059")], port=5000)["verdict"])
        self.assertIn("PLC setting", v)

    def test_nothing_answers_but_ping_ok(self):
        v = " ".join(go([res("udp", 1388, "timeout"), res("tcp", 1388, "refused")])["verdict"])
        self.assertIn("answers a ping", v)
        self.assertIn("powered off and on", v)
        self.assertIn("listens", v)

    def test_wrong_network_reported_first(self):
        out = go([res("udp", 1388, "timeout")], alive=False, adapters=[("10.0.0.7", "255.255.255.0")])
        self.assertIn("NOT in the same network", out["verdict"][0])

    def test_zero_mask(self):
        out = go([res("udp", 1388, "timeout")], alive=False, adapters=[("10.0.0.7", "0.0.0.0")])
        self.assertIn("not valid", out["verdict"][0])

    def test_report_text(self):
        text = doctor.report(go([res("udp", 1388, "ok", hexs="d00000")]), "192.168.3.39")
        self.assertIn("UDP 1388: ANSWERED", text)
        self.assertIn("500000ffff03", text)


class Helpers(unittest.TestCase):
    def test_ports_include_connection_port_once(self):
        ports = doctor.ports_to_test(5000)
        self.assertEqual(ports.count(5000), 1)
        self.assertIn(1388, doctor.ports_to_test(1388))

    def test_ping(self):
        ok = lambda cmd, **k: SimpleNamespace(stdout="Reply from 1.2.3.4: bytes=32 time<1ms TTL=64")
        bad = lambda cmd, **k: SimpleNamespace(stdout="Reply from 1.2.3.1: Destination host unreachable.")
        self.assertTrue(doctor.ping("1.2.3.4", runner=ok, platform="win32"))
        self.assertFalse(doctor.ping("1.2.3.4", runner=bad, platform="win32"))
        self.assertIsNone(doctor.ping("1.2.3.4", runner=lambda *a, **k: 1 / 0))


if __name__ == "__main__":
    unittest.main()

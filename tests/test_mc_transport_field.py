"""The Add Connection form offers a transport (tcp / udp) for Mitsubishi MC, and a connection set to udp really reads over UDP."""
import os
import sys
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import protoprobe as pp  # noqa: E402
from test_slmp import SimUdp  # noqa: E402


class TransportField(unittest.TestCase):
    def test_form_has_transport_default_tcp(self):
        self.assertIn(("transport", "tcp"), pp.INTERFACE_FIELDS["mc_protocol_3e"])

    def test_udp_connection_reads(self):
        plc = SimUdp({100: 42}); plc.start()
        try:
            conn = pp.Connection("c", "mc_protocol_3e", {"ip": "127.0.0.1", "port": plc.port, "transport": "udp", "timeout_s": 0.4})
            tag = {"name": "t", "device_code": "D", "device_number": 100, "data_type": "int16"}
            value, err = pp.read_tag_once(conn, tag)
            self.assertIsNone(err)
            self.assertEqual(value, 42)
        finally:
            plc.close()

    def test_tcp_to_a_udp_only_plc_times_out_in_words(self):
        plc = SimUdp({100: 42}); plc.start()
        try:
            conn = pp.Connection("c", "mc_protocol_3e", {"ip": "127.0.0.1", "port": plc.port, "timeout_s": 0.4})
            value, err = pp.read_tag_once(conn, {"name": "t", "device_code": "D", "device_number": 100, "data_type": "int16"})
            self.assertIsNone(value)
            self.assertTrue(err)
        finally:
            plc.close()


if __name__ == "__main__":
    unittest.main()

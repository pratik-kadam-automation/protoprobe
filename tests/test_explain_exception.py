"""A failed connection must say something in plain words. A TimeoutError has no text, so the scan used to print "failed:" and nothing else."""
import asyncio
import os
import sys
import unittest

HERE = os.path.dirname(__file__)
sys.path[:0] = [os.path.join(HERE, ".."), os.path.join(HERE, "..", "edge-gateway")]
import protoprobe as pp  # noqa: E402


class Explain(unittest.TestCase):
    def test_a_timeout_is_never_empty_and_names_the_usual_causes(self):
        for exc in (asyncio.TimeoutError(), TimeoutError()):
            self.assertEqual(str(exc), "")                         # the reason for this change
            text = pp.explain_exception(exc)
            self.assertIn("no answer from the PLC", text)
            self.assertIn("wrong IP or port", text)

    def test_mitsubishi_gets_the_q_series_port_help(self):
        text = pp.explain_exception(asyncio.TimeoutError(), "mc_protocol_3e")
        self.assertIn("Open settings", text)
        self.assertIn("1388 hex = 5000 decimal", text)
        self.assertIn("Connection doctor", text)
        self.assertNotIn("Open settings", pp.explain_exception(asyncio.TimeoutError(), "s7comm"))

    def test_refused_unreachable_and_reset(self):
        self.assertIn("nothing listens on that port", pp.explain_exception(ConnectionRefusedError()))
        e = OSError(10065, "unreachable"); e.winerror = 10065
        self.assertIn("cannot be reached", pp.explain_exception(e))
        self.assertIn("closed the connection", pp.explain_exception(ConnectionResetError("boom")))
        w = OSError(); w.winerror = 10060
        self.assertIn("no answer", pp.explain_exception(w))

    def test_other_errors_keep_their_own_text_or_at_least_their_name(self):
        self.assertEqual(pp.explain_exception(ValueError("bad address")), "bad address")
        self.assertEqual(pp.explain_exception(KeyError()), "KeyError")

    def test_read_a_value_shows_the_explained_reason(self):
        class Conn:
            protocol = "mc_protocol_3e"
            interface = {"ip": "127.0.0.1", "port": 1}
            name = "c"

        class Dead:
            def __init__(self, cfg): pass
            async def connect(self): raise asyncio.TimeoutError()
            async def poll(self): return {}
            async def disconnect(self): pass
        value, why = pp.read_tag_once(Conn(), {"name": "t", "device_code": "D", "device_number": 0, "data_type": "int16", "mqtt_key": "t"}, {"mc_protocol_3e": Dead})
        self.assertIsNone(value)
        self.assertIn("no answer from the PLC", why)
        self.assertIn("Open settings", why)


if __name__ == "__main__":
    unittest.main()

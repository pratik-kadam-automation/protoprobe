"""The green / amber / red status of a connection, from what the poller reports."""
import logging
import os
import queue
import sys
import unittest

HERE = os.path.dirname(__file__)
sys.path[:0] = [os.path.join(HERE, ".."), os.path.join(HERE, "..", "edge-gateway")]
import protoprobe as pp  # noqa: E402


class Health(unittest.TestCase):
    def test_not_polling_is_grey(self):
        self.assertEqual(pp.connection_health("idle", None, 3)[0], "grey")
        self.assertEqual(pp.connection_health("stopped", None, 3)[0], "grey")

    def test_connecting_and_first_reading_are_amber(self):
        self.assertEqual(pp.connection_health("connecting", None, 3)[0], "amber")
        self.assertEqual(pp.connection_health("connected", None, 3)[0], "amber")

    def test_all_tags_good_is_green(self):
        colour, text = pp.connection_health("connected", {"polls": 5, "errors": 0, "cycle_ms": 19.4, "bad": {}}, 4)
        self.assertEqual(colour, "green")
        self.assertIn("4 tag(s) OK", text)
        self.assertIn("19 ms", text)

    def test_some_failing_is_amber_and_all_failing_is_red(self):
        some = pp.connection_health("connected", {"polls": 5, "errors": 5, "cycle_ms": 10, "bad": {"a": "object does not exist"}}, 4)
        self.assertEqual(some[0], "amber")
        self.assertIn("3 of 4", some[1])
        every = pp.connection_health("connected", {"polls": 5, "errors": 20, "cycle_ms": 10, "bad": {"a": "boom", "b": "boom"}}, 2)
        self.assertEqual(every[0], "red")
        self.assertIn("boom", every[1])

    def test_error_status_is_red_with_the_reason(self):
        colour, text = pp.connection_health("error: timed out", None, 2)
        self.assertEqual(colour, "red")
        self.assertIn("timed out", text)


class FakeDriver:
    """Stand-in driver: tag 'bad' is never answered and is logged the way the real drivers do."""
    def __init__(self, cfg):
        self.cfg = cfg
        self.log = logging.getLogger("edge_collector.s7")

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def poll(self):
        out = {}
        for t in self.cfg["tags"]:
            if t["name"] == "bad":
                self.log.warning("[%s] tag %s decode error: %s", "g", t["name"], "Read operation failed: Object does not exist (0x0a)")
            else:
                out[t["mqtt_key"]] = 7
        return out


class PollerReportsHealth(unittest.TestCase):
    def test_poller_names_the_failing_tag(self):
        conn = pp.Connection("c", "s7comm", {"ip": "192.0.2.1", "rack": 0, "slot": 1})
        conn.tags = [{"name": "good", "area": "M", "start_byte": 0, "data_type": "int16"},
                     {"name": "bad", "area": "DB", "db_number": 1, "start_byte": 0, "data_type": "int16"}]
        q = queue.Queue()
        old = pp.DRIVER_FACTORY["s7comm"]
        pp.DRIVER_FACTORY["s7comm"] = FakeDriver
        try:
            thread = pp.PollerThread(conn, 0.05, q)
            thread.start()
            reports = []
            for _ in range(60):
                try:
                    kind, data = q.get(timeout=0.2)
                except queue.Empty:
                    continue
                if kind == "health":
                    reports.append(data[1])
                    if len(reports) >= 2:
                        break
            thread.stop()
            thread.join(timeout=3)
        finally:
            pp.DRIVER_FACTORY["s7comm"] = old
        self.assertGreaterEqual(len(reports), 2)
        last = reports[-1]
        self.assertEqual(set(last["bad"]), {"bad"})
        self.assertIn("Object does not exist", last["bad"]["bad"])
        self.assertGreaterEqual(last["polls"], 2)
        self.assertEqual(pp.connection_health("connected", last, 2)[0], "amber")


if __name__ == "__main__":
    unittest.main()


class RealDriverLoggerNames(unittest.TestCase):
    def test_every_real_driver_logs_under_a_name_the_collectors_listen_on(self):
        import re
        for name in ("s7", "mc", "modbus"):
            src = open(os.path.join(HERE, "..", "edge-gateway", "drivers", f"{name}_driver.py"), encoding="utf-8").read()
            logger_name = re.search(r'getLogger\("([^"]+)"\)', src).group(1)
            self.assertTrue(any(logger_name == n or logger_name.startswith(n + ".") for n in pp.DRIVER_LOGGERS), logger_name)

    def test_a_real_driver_warning_reaches_the_collector(self):
        import logging
        collector = pp.PollLogCollector()
        for n in pp.DRIVER_LOGGERS:
            logging.getLogger(n).addHandler(collector)
        try:
            from drivers import s7_driver
            s7_driver.logger.warning("[%s] tag %s decode error: %s", "g", "Speed", "Object does not exist (0x0a)")
        finally:
            for n in pp.DRIVER_LOGGERS:
                logging.getLogger(n).removeHandler(collector)
        self.assertEqual(collector.errors, {"Speed": "Object does not exist (0x0a)"})

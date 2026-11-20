"""A whole-PLC scan of a Siemens PLC. The first real run asked for input bytes up to 2000 on a PLC whose input memory is smaller: every
missing address failed, each failure was printed to the console (one line per address), and the scan kept asking. Now the readable end is found
first, the scan stays inside it, and the failures are counted instead of printed. Uses a stand-in driver: no PLC."""
import contextlib
import io
import logging
import os
import queue
import sys
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "edge-gateway"))
import protoprobe                                   # noqa: E402  (imports tkinter; needs no display)
from plc_reader import build_range_tags             # noqa: E402

MEMORY = 1024                                       # bytes of input memory the stand-in PLC has


class FakeS7:
    """Looks like the S7 driver as the scan uses it: connect/poll/disconnect, the blocking read, config['tags'], and the same warning on a failed read."""
    reads = []

    def __init__(self, cfg):
        self.config = cfg
        self.log = logging.getLogger("edge_collector.s7")

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    def _read_area_blocking(self, area, db, start, size):
        FakeS7.reads.append(start)
        if area == "DB" or start + size > MEMORY:
            raise RuntimeError("Read operation failed: Invalid address (0x05)")
        return bytes([start % 256, 0])

    async def poll(self):
        out = {}
        for tag in self.config["tags"]:
            try:
                raw = self._read_area_blocking(tag["area"], tag["db_number"], tag["start_byte"], 2)
                out[tag["mqtt_key"]] = (raw[0] << 8) | raw[1]
            except Exception as e:
                self.log.warning("[%s] tag %s decode error: %s", "conn", tag["name"], e)
        return out


class Conn:
    name = "conn"
    interface = {"ip": "192.0.2.10", "rack": 0, "slot": 1}


def scan(area, start, end, snapshots=2):
    FakeS7.reads = []
    q = queue.Queue()
    err = io.StringIO()
    old = protoprobe.DRIVER_FACTORY["s7comm"]
    protoprobe.DRIVER_FACTORY["s7comm"] = FakeS7
    try:
        with contextlib.redirect_stderr(err):
            protoprobe.run_range_scan(Conn(), "s7comm", build_range_tags("s7comm", {"area": area, "db_number": 1, "start": start, "end": end}),
                                      snapshots, 0, f"{area} ({start}-{end})", q)
    finally:
        protoprobe.DRIVER_FACTORY["s7comm"] = old
    msgs = []
    while not q.empty():
        msgs.append(q.get_nowait())
    return msgs, err.getvalue()


class QuietS7Scan(unittest.TestCase):
    def test_halving_finds_the_last_readable_byte(self):
        for limit in (2, 6, 100, 1022, 1024, 1999):
            last = protoprobe.last_readable_offset(lambda off: off + 2 <= limit, 0, 2000)
            self.assertEqual(last, (limit - 2) // 2 * 2, limit)
        self.assertIsNone(protoprobe.last_readable_offset(lambda off: False, 0, 2000))
        self.assertEqual(protoprobe.last_readable_offset(lambda off: True, 0, 2000), 2000)

    def test_the_scan_stays_inside_the_plcs_memory_and_prints_nothing(self):
        msgs, stderr = scan("I", 0, 2000)
        self.assertEqual(stderr, "", "no per-address warnings on the console")
        results = [m for m in msgs if m[0] == "results"][0][1][2]
        starts = [r["tag"]["start_byte"] for r in results]
        self.assertEqual(max(starts), MEMORY - 2)
        self.assertEqual(len(starts), MEMORY // 2)
        text = " ".join(m[1] for m in msgs if m[0] == "status")
        self.assertIn(f"the PLC has {MEMORY} bytes here", text)
        self.assertNotIn("reads failed", text)                         # nothing readable was left to fail
        beyond = [r for r in FakeS7.reads if r >= MEMORY]
        self.assertLess(len(beyond), 20, "only the few probe reads go past the end, not one per address")

    def test_a_range_inside_the_memory_is_left_alone(self):
        msgs, stderr = scan("I", 0, 200)
        self.assertEqual(stderr, "")
        self.assertEqual(len([r for r in [m for m in msgs if m[0] == "results"][0][1][2]]), 101)
        self.assertNotIn("bytes here", " ".join(m[1] for m in msgs if m[0] == "status"))

    def test_an_unreadable_area_is_reported_once_not_per_address(self):
        msgs, stderr = scan("DB", 0, 400)
        self.assertEqual(stderr, "")
        text = " ".join(m[1] for m in msgs if m[0] == "status")
        self.assertIn("nothing readable at byte 0", text)
        self.assertLess(len(FakeS7.reads), 5)                          # one or two probes, not 200 reads x snapshots

    def test_the_collector_counts_and_prints_nothing(self):
        c = protoprobe.ScanLogCollector()
        lg = logging.getLogger("edge_collector.s7")
        lg.addHandler(c)
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                for n in range(5):
                    lg.warning("[%s] tag %s decode error: %s", "conn", f"I{n}", "Invalid address (0x05)")
        finally:
            lg.removeHandler(c)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(c.counts, {"Invalid address (0x05)": 5})
        self.assertEqual(c.top(), "Invalid address (0x05)")


if __name__ == "__main__":
    unittest.main()

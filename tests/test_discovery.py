import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import discovery as dv  # noqa: E402
import signal_hints as sh  # noqa: E402


class FakePlc:
    """Memory that a script changes; counts the bytes it was asked for."""
    def __init__(self, size=256):
        self.mem = {"M": bytearray(size)}
        self.asked = 0
        self.fail = False

    def read(self, area, start, size):
        if self.fail:
            raise OSError("down")
        self.asked += size
        return bytes(self.mem[area][start:start + size])


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def engine(plc, clock, **kw):
    store = dv.Store()
    args = dict(areas={"M": (0, 255)}, reeval_s=60, active_period_s=1, sweep_period_s=10)
    args.update(kw)
    return dv.Engine(plc.read, store, clock=clock, **args), store


class Blocks(unittest.TestCase):
    def test_neighbours_share_a_request(self):
        self.assertEqual(dv.blocks_covering([4, 5, 6, 40]), [(4, 3), (40, 1)])
        self.assertEqual(dv.blocks_covering([4, 5, 6, 20]), [(4, 17)])         # gap of 14 <= 16: one request
        self.assertEqual(dv.blocks_covering([0, 10]), [(0, 11)])             # gap of 9 <= 16: one request
        self.assertEqual(dv.blocks_covering([0, 100]), [(0, 1), (100, 1)])

    def test_a_request_is_never_longer_than_the_block(self):
        got = dv.blocks_covering(range(0, 500, 4), block=200)
        self.assertTrue(all(size <= 200 for _s, size in got))

    def test_the_end_of_memory_is_respected(self):
        self.assertEqual(dv.blocks_covering([250, 255], limit=256), [(250, 6)])


class Sweeps(unittest.TestCase):
    def test_a_full_sweep_of_256_bytes_is_two_requests(self):
        plc, clock = FakePlc(), Clock()
        e, _ = engine(plc, clock)
        e.sweep_all()
        self.assertEqual(e.reads, 2)

    def test_only_changes_are_stored(self):
        plc, clock = FakePlc(), Clock()
        e, store = engine(plc, clock)
        e.sweep_all()
        self.assertEqual(e.stored, 0)                                         # the first look is the baseline
        plc.mem["M"][22] = 5
        clock.t += 1
        e.sweep_all()
        e.sweep_all()
        self.assertEqual(store.changes_of("M", 22), [(1001.0, 0, 5)])
        self.assertEqual(e.stored, 1)

    def test_a_failing_read_is_counted_not_raised(self):
        plc, clock = FakePlc(), Clock()
        plc.fail = True
        e, _ = engine(plc, clock)
        e.sweep_all()
        self.assertGreaterEqual(e.read_errors, 1)


class Tiers(unittest.TestCase):
    def run_session(self, plc, clock, seconds, script, **kw):
        e, store = engine(plc, clock, **kw)
        for _ in range(seconds):
            script(clock.t - 1000)
            e.tick()
            clock.t += 1
        return e, store

    def test_the_moving_byte_becomes_active_the_quiet_one_does_not(self):
        plc, clock = FakePlc(), Clock()

        def script(t):
            if int(t) % 7 == 0:
                plc.mem["M"][22] += 1                                         # a counter

        e, store = self.run_session(plc, clock, 130, script)
        self.assertIn(22, e.active["M"])
        self.assertNotIn(100, e.active["M"])
        before = plc.asked
        e.sweep_active()
        self.assertLess(plc.asked - before, 40)                               # an active-only step reads far less than a full sweep

    def test_a_flashing_byte_is_noisy_and_its_storage_is_limited(self):
        plc, clock = FakePlc(), Clock()

        def script(t):
            plc.mem["M"][0] ^= 0xFF                                           # changes at every look

        e, store = self.run_session(plc, clock, 130, script, noisy_store_s=10, sweep_period_s=1)
        self.assertIn(0, e.noisy["M"])
        self.assertGreater(e.dropped, 0)
        late = [c for c in store.changes_of("M", 0) if c[0] > 1070]
        self.assertLess(len(late), 10)                                        # about one change per 10 s once it is noisy, not one per second

    def test_status_reports_progress(self):
        plc, clock = FakePlc(), Clock()
        e, _ = self.run_session(plc, clock, 5, lambda t: None)
        s = e.status()
        self.assertGreater(s["reads"], 0)
        self.assertEqual(s["read_errors"], 0)
        self.assertGreater(s["next_reevaluation_s"], 0)


class Series(unittest.TestCase):
    def test_a_16_bit_counter_is_rebuilt_from_byte_changes(self):
        plc, clock = FakePlc(), Clock()
        e, store = engine(plc, clock, sweep_period_s=1)
        e.tick()
        for _ in range(6):
            clock.t += 1
            value = (plc.mem["M"][20] << 8 | plc.mem["M"][21]) + 130           # crosses the byte boundary: 130, 260, 390 ...
            plc.mem["M"][20], plc.mem["M"][21] = value >> 8, value & 0xFF
            e.tick()
        series = dv.word_series(store, "M", 20, 2)
        self.assertEqual([v for _t, v in series][:3], [130, 260, 390])
        self.assertEqual(sh.classify_word([v for _t, v in series]).kind, "counter")

    def test_nothing_changed_gives_an_empty_series(self):
        self.assertEqual(dv.word_series(dv.Store(), "M", 0), [])


if __name__ == "__main__":
    unittest.main()

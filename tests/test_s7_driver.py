"""The S7 driver against the python-snap7 version the project pins (3.3.1): the area codes must come from the installed
library (snap7.type.Area), and a poll with a stand-in PLC client must decode tags. The first real run on an S7-1214C
failed on `from snap7.types import Areas`, a module that no longer exists: every tag logged 'decode error'."""
import asyncio
import os
import struct
import sys
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "edge-gateway"))
from drivers.s7_driver import S7Driver


class FakeClient:
    """Stands in for snap7's Client: remembers what was asked, answers from a dict of DB bytes."""
    def __init__(self, db):
        self.db, self.calls = db, []

    def read_area(self, area, db_number, start, size):
        self.calls.append((int(area), db_number, start, size))
        return bytearray(self.db[db_number][start:start + size])


def driver_with(tags, db):
    d = S7Driver("conn", {"interface": {"ip": "127.0.0.1", "rack": 0, "slot": 1}, "tags": tags})
    d.client, d.connected = FakeClient(db), True
    return d


class S7DriverTests(unittest.TestCase):
    def test_area_codes_come_from_the_installed_library(self):
        d = S7Driver("conn", {"interface": {}, "tags": []})
        self.assertEqual(int(d._area_code("DB")), 0x84)
        self.assertEqual(int(d._area_code("I")), 0x81)
        self.assertEqual(int(d._area_code("Q")), 0x82)
        self.assertEqual(int(d._area_code("M")), 0x83)
        with self.assertRaises(ValueError):
            d._area_code("X")

    def test_poll_decodes_tags_from_a_data_block(self):
        db9 = bytes(struct.pack(">hHif", -5, 700, 123456, 1.5))        # int16, uint16, int32, float32 at 0, 2, 4, 8
        tags = [
            {"name": "a", "mqtt_key": "a", "area": "DB", "db_number": 9, "start_byte": 0, "data_type": "int16"},
            {"name": "b", "mqtt_key": "b", "area": "DB", "db_number": 9, "start_byte": 2, "data_type": "uint16"},
            {"name": "c", "mqtt_key": "c", "area": "DB", "db_number": 9, "start_byte": 4, "data_type": "int32"},
            {"name": "d", "mqtt_key": "d", "area": "DB", "db_number": 9, "start_byte": 8, "data_type": "float32_be", "multiplier": 2.0},
        ]
        d = driver_with(tags, {9: db9})
        out = asyncio.run(d.poll())
        self.assertEqual(out, {"a": -5, "b": 700, "c": 123456, "d": 3.0})
        self.assertEqual(d.client.calls[0], (0x84, 9, 0, 2))           # DB area, DB9, byte 0, 2 bytes

    def test_a_bit_tag(self):
        d = driver_with([{"name": "run", "mqtt_key": "run", "area": "DB", "db_number": 9, "start_byte": 0,
                          "data_type": "bit", "bit_offset": 3}], {9: bytes([0b00001000])})
        self.assertEqual(asyncio.run(d.poll()), {"run": 1})


if __name__ == "__main__":
    unittest.main()

"""
mc_driver.py

Mitsubishi MC Protocol (SLMP), 3E frame, binary mode, over TCP --
for the FX5U (and Q/L/iQ-R series generally).

There's no well-maintained lightweight pure-Python SLMP library that
plays nicely with a 256MB MIPS box, so this implements the 3E frame
format directly. It's ~120 lines and has zero external dependencies
beyond asyncio -- deliberately the cheapest driver in this set,
resource-wise.

Frame reference: batch read of word devices, binary communication,
QnA-compatible 3E frame, command 0401 / subcommand 0000.
"""

import asyncio
import logging
from .base_driver import BaseDriver

logger = logging.getLogger("edge_collector.mc")


class MCProtocolError(Exception):
    """A well-formed response came back with a nonzero end code -- the
    PLC understood the request and rejected it (bad address, out of
    range, etc). This means the connection itself is fine; only this
    one address is invalid. Deliberately NOT a ConnectionError/OSError
    subclass, so poll() treats it as a per-tag skip, not a fatal
    connection failure that aborts every other tag in the same batch."""
    pass


# Device code -> ASCII/binary code used in the 3E frame device field
DEVICE_CODES = {
    "D": 0xA8,   # Data register
    "M": 0x90,   # Internal relay (bit device -- word-read path not used here)
    "R": 0xAF,   # File register
    "W": 0xB4,   # Link register
}


class MCProtocolDriver(BaseDriver):
    def __init__(self, group_id, group_config):
        super().__init__(group_id, group_config)
        self.reader = None
        self.writer = None

    async def connect(self):
        iface = self.config["interface"]
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(iface["ip"], iface["port"]),
            timeout=iface.get("timeout_s", 1.0),
        )
        self.connected = True
        logger.info("Connected: %s", self.group_id)

    async def disconnect(self):
        if self.writer:
            self.writer.close()
        self.connected = False

    async def poll(self) -> dict:
        if not self.connected:
            await self.connect()

        results = {}
        for tag in self.config["tags"]:
            try:
                words_needed = 2 if "32" in tag["data_type"] else 1
                raw_words = await self._read_words(
                    tag["device_code"], tag["device_number"], words_needed
                )
                raw = self.decode_value(raw_words, tag["data_type"])
                scaled = self.apply_scaling(
                    raw, tag.get("multiplier", 1.0), tag.get("offset", 0.0)
                )
                results[tag["mqtt_key"]] = scaled
            except (asyncio.TimeoutError, ConnectionError, OSError):
                self.connected = False
                raise
            except Exception as e:
                logger.warning(
                    "[%s] tag %s decode error: %s", self.group_id, tag["name"], e
                )
        return results

    async def _read_words(self, device_code: str, device_number: int, count: int):
        frame = self._build_read_frame(device_code, device_number, count)
        self.writer.write(frame)
        await self.writer.drain()

        header = await asyncio.wait_for(self.reader.readexactly(9), timeout=1.0)
        data_len = int.from_bytes(header[7:9], "little")
        body = await asyncio.wait_for(
            self.reader.readexactly(data_len), timeout=1.0
        )

        end_code = int.from_bytes(body[0:2], "little")
        if end_code != 0:
            raise MCProtocolError(f"MC protocol end code error: 0x{end_code:04X}")

        payload = body[2:]
        words = []
        for i in range(0, len(payload), 2):
            words.append(int.from_bytes(payload[i:i + 2], "little"))
        return words

    @staticmethod
    def _build_read_frame(device_code: str, device_number: int, count: int) -> bytes:
        if device_code not in DEVICE_CODES:
            raise ValueError(f"Unsupported MC device code: {device_code}")

        subheader = b"\x50\x00"          # 3E frame subheader
        network = b"\x00"
        pc = b"\xFF"
        module_io = b"\xFF\x03"
        module_station = b"\x00"

        command = b"\x01\x04"            # batch read
        subcommand = b"\x00\x00"         # word units, binary

        device_addr = device_number.to_bytes(3, "little")
        device_code_byte = bytes([DEVICE_CODES[device_code]])
        num_points = count.to_bytes(2, "little")

        request_data = (
            command + subcommand + device_addr + device_code_byte + num_points
        )
        request_len = (len(request_data) + 2).to_bytes(2, "little")  # +2 for timer
        timer = b"\x10\x00"  # 4s CPU monitoring timer

        return (
            subheader
            + network
            + pc
            + module_io
            + module_station
            + request_len
            + timer
            + request_data
        )

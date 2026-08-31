"""
s7_driver.py

Siemens S7comm via python-snap7.

Supports four memory areas:
  - DB (data blocks) -- word/float values at a byte offset, as before
  - I  (physical inputs)  -- typically single bits, e.g. I0.0
  - Q  (physical outputs) -- typically single bits, e.g. Q0.1
  - M  (internal markers/flags) -- bits or words, program-internal
       (unlike I/Q, M has no physical wiring to reference -- it's as
       opaque as DB data without the source program, so scanning it
       is worthwhile the same way scanning a DB is)

*** VERIFY THIS AGAINST YOUR INSTALLED python-snap7 VERSION ***
The area codes below (snap7.types.Areas.PE/PA/MK/DB) are the
conventional python-snap7 API, but this hasn't been tested against
real I/O hardware -- the exact attribute names have shifted across
python-snap7 forks/versions historically. Before trusting a full I/Q/M
scan, verify with one known bit first: e.g. read a physical input
you can toggle by hand (a start button, a limit switch) and confirm
the driver's value flips when you do. If the import of Areas fails
outright, or reads raise unexpected errors, that's the API mismatch
to chase down -- not a wiring or PLC problem.

*** DEPLOYMENT RISK (unchanged from before) ***
python-snap7 is a ctypes wrapper around libsnap7.so, a compiled C
library. There is no official prebuilt MIPS24KEc binary. Before
relying on this driver on the AG-221/702:
  1. Cross-compile snap7 using the OpenWrt SDK toolchain for this
     target (mips_24kc).
  2. Confirm the DB you're reading has "Optimized block access" OFF
     and "PUT/GET access" enabled in TIA Portal -- otherwise every
     read here will fail regardless of driver correctness.

If compiling snap7 for this hardware turns out not to be worth the
effort, config.json's Option B (Modbus TCP via the S7's MB_SERVER
block) reuses modbus_driver.py instead and needs no native library
at all. That fallback exists specifically so this risk doesn't block
the whole gateway.
"""

import asyncio
import logging
from .base_driver import BaseDriver

logger = logging.getLogger("edge_collector.s7")

try:
    import snap7
except ImportError:  # pragma: no cover
    snap7 = None


class S7Driver(BaseDriver):
    def __init__(self, group_id, group_config):
        super().__init__(group_id, group_config)
        self.client = None

    async def connect(self):
        if snap7 is None:
            raise RuntimeError(
                "python-snap7 not installed / libsnap7.so not built for MIPS. "
                "See module docstring."
            )
        iface = self.config["interface"]
        loop = asyncio.get_event_loop()
        self.client = snap7.client.Client()
        # snap7 calls are blocking C calls -- run off the event loop thread
        await loop.run_in_executor(
            None, self.client.connect, iface["ip"], iface["rack"], iface["slot"]
        )
        self.connected = self.client.get_connected()
        if not self.connected:
            raise ConnectionError(f"[{self.group_id}] S7 connect failed")
        logger.info("Connected: %s", self.group_id)

    async def disconnect(self):
        if self.client:
            self.client.disconnect()
        self.connected = False

    def _area_code(self, area: str):
        """Maps our 'DB'/'I'/'Q'/'M' strings to snap7's Areas enum.
        See the VERIFY warning in the module docstring."""
        from snap7.types import Areas
        mapping = {"DB": Areas.DB, "I": Areas.PE, "Q": Areas.PA, "M": Areas.MK}
        if area not in mapping:
            raise ValueError(f"Unsupported S7 area: {area!r} (expected DB/I/Q/M)")
        return mapping[area]

    def _read_area_blocking(self, area: str, db_number: int, start: int, size: int):
        """Runs on the executor thread -- snap7 calls are blocking C calls."""
        area_code = self._area_code(area)
        db_arg = db_number if area == "DB" else 0
        return self.client.read_area(area_code, db_arg, start, size)

    async def poll(self) -> dict:
        if not self.connected:
            await self.connect()

        results = {}
        loop = asyncio.get_event_loop()
        for tag in self.config["tags"]:
            try:
                area = tag.get("area", "DB")
                data_type = tag["data_type"]

                if data_type == "bit":
                    raw_bytes = await loop.run_in_executor(
                        None, self._read_area_blocking, area, tag.get("db_number", 0),
                        tag["start_byte"], 1,
                    )
                    bit_offset = tag.get("bit_offset", 0)
                    value = 1 if (raw_bytes[0] >> bit_offset) & 1 else 0
                    scaled = self.apply_scaling(
                        value, tag.get("multiplier", 1.0), tag.get("offset", 0.0)
                    )
                else:
                    size = 4 if "32" in data_type or "float" in data_type else 2
                    raw_bytes = await loop.run_in_executor(
                        None, self._read_area_blocking, area, tag.get("db_number", 0),
                        tag["start_byte"], size,
                    )
                    words = self._bytes_to_words(raw_bytes)
                    raw = self.decode_value(words, data_type)
                    scaled = self.apply_scaling(
                        raw, tag.get("multiplier", 1.0), tag.get("offset", 0.0)
                    )

                results[tag["mqtt_key"]] = scaled
            except (ConnectionError, OSError):
                self.connected = False
                raise
            except Exception as e:
                logger.warning(
                    "[%s] tag %s decode error: %s", self.group_id, tag["name"], e
                )
        return results

    @staticmethod
    def _bytes_to_words(raw_bytes):
        words = []
        for i in range(0, len(raw_bytes), 2):
            words.append((raw_bytes[i] << 8) | raw_bytes[i + 1])
        return words

"""
base_driver.py

Every protocol driver implements this same tiny interface so
edge_collector.py never needs protocol-specific branching in its
scheduling loop. This is what makes the engine hardware- and
protocol-agnostic: swapping the M300 for the AG-221/702, or later
for in-house hardware, only ever touches interface parameters in
config.json -- never this contract.
"""

from abc import ABC, abstractmethod
import struct


class BaseDriver(ABC):
    """
    Contract:
      - connect()/disconnect() manage the underlying transport.
      - poll() reads every tag for this polling group in one pass
        and returns {tag_name: raw_numeric_value, ...}. Missing/failed
        reads should be OMITTED from the dict (not set to 0 or None) --
        the engine treats an absent key as "no update this cycle",
        which keeps report-by-exception logic honest.
      - poll() must never raise for a single bad tag; log and skip it.
        It MAY raise for a transport-level failure (socket closed,
        timeout on the whole exchange) -- the engine catches that,
        marks the group unhealthy, and retries on the next cycle.
    """

    def __init__(self, group_id: str, group_config: dict):
        self.group_id = group_id
        self.config = group_config
        self.connected = False

    @abstractmethod
    async def connect(self):
        ...

    @abstractmethod
    async def disconnect(self):
        ...

    @abstractmethod
    async def poll(self) -> dict:
        ...

    @staticmethod
    def decode_value(raw_registers, data_type: str):
        """
        Shared register->value decoding used by the register-based
        protocols (Modbus, S7, MC protocol all store data as raw
        16-bit words or byte blocks). raw_registers is a list of
        16-bit ints in the order read off the wire.
        """
        if data_type == "int16":
            v = raw_registers[0]
            return v - 0x10000 if v >= 0x8000 else v

        if data_type == "uint16":
            return raw_registers[0]

        if data_type in ("int32", "uint32", "float32_be", "float32_le"):
            hi, lo = raw_registers[0], raw_registers[1]
            if data_type.endswith("_le"):
                raw = struct.pack(">HH", lo, hi)
            else:
                raw = struct.pack(">HH", hi, lo)

            if data_type == "int32":
                return struct.unpack(">i", raw)[0]
            if data_type == "uint32":
                return struct.unpack(">I", raw)[0]
            return struct.unpack(">f", raw)[0]

        raise ValueError(f"Unsupported data_type: {data_type}")

    @staticmethod
    def apply_scaling(value, multiplier: float, offset: float):
        return (value * multiplier) + offset

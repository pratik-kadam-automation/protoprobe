"""
modbus_driver.py

Covers both Modbus RTU (RS485 -- Delta DVP/AS, Mitsubishi FX3U/FX3G)
and Modbus TCP (Ethernet -- Delta AS series, S7 via MB_SERVER block,
FX5U via Modbus TCP option). Same driver class handles both; only
the transport differs.

Uses pymodbus (pure Python, no compiled extension) -- picked
deliberately because it's the one protocol library in this whole
stack guaranteed to run on MIPS with zero cross-compilation risk.
"""

import asyncio
import logging
from .base_driver import BaseDriver

logger = logging.getLogger("edge_collector.modbus")

try:
    from pymodbus.client import AsyncModbusTcpClient, AsyncModbusSerialClient
except ImportError:  # pragma: no cover
    AsyncModbusTcpClient = None
    AsyncModbusSerialClient = None


class ModbusDriver(BaseDriver):
    def __init__(self, group_id, group_config, transport: str):
        """transport: 'tcp' or 'rtu'"""
        super().__init__(group_id, group_config)
        self.transport = transport
        self.client = None

    async def connect(self):
        if AsyncModbusTcpClient is None:
            raise RuntimeError(
                "pymodbus not installed -- opkg/pip install pymodbus"
            )

        iface = self.config["interface"]
        if self.transport == "tcp":
            self.client = AsyncModbusTcpClient(
                host=iface["ip"],
                port=iface.get("port", 502),
                timeout=iface.get("timeout_s", 1.0),
            )
        else:
            self.client = AsyncModbusSerialClient(
                port=iface["port"],
                baudrate=iface.get("baudrate", 9600),
                parity=iface.get("parity", "N"),
                stopbits=iface.get("stopbits", 1),
                bytesize=iface.get("bytesize", 8),
                timeout=iface.get("timeout_s", 1.0),
            )

        await self.client.connect()
        self.connected = self.client.connected
        if not self.connected:
            raise ConnectionError(f"[{self.group_id}] Modbus connect failed")
        logger.info("Connected: %s (%s)", self.group_id, self.transport)

    async def disconnect(self):
        if self.client:
            self.client.close()
        self.connected = False

    async def poll(self) -> dict:
        if not self.connected:
            await self.connect()

        results = {}
        for device in self.config["devices"]:
            unit_id = device["unit_id"]
            for tag in device["tags"]:
                try:
                    words_needed = 2 if "32" in tag["data_type"] else 1
                    rr = await self._read(
                        tag["register_type"], tag["address"], words_needed, unit_id
                    )
                    if rr is None or rr.isError():
                        logger.warning(
                            "[%s] read failed for tag %s", self.group_id, tag["name"]
                        )
                        continue

                    raw = self.decode_value(rr.registers, tag["data_type"])
                    scaled = self.apply_scaling(
                        raw, tag.get("multiplier", 1.0), tag.get("offset", 0.0)
                    )
                    results[tag["mqtt_key"]] = scaled
                except (asyncio.TimeoutError, ConnectionError) as e:
                    # transport-level -- bubble up so the engine can
                    # mark the whole group unhealthy and reconnect
                    self.connected = False
                    raise
                except Exception as e:
                    # single bad tag -- log and continue, per contract
                    logger.warning(
                        "[%s] tag %s decode error: %s", self.group_id, tag["name"], e
                    )
        return results

    async def _read(self, register_type, address, count, unit_id):
        if register_type == "holding":
            return await self.client.read_holding_registers(
                address, count, slave=unit_id
            )
        if register_type == "input":
            return await self.client.read_input_registers(
                address, count, slave=unit_id
            )
        if register_type == "coil":
            return await self.client.read_coils(address, count, slave=unit_id)
        raise ValueError(f"Unsupported register_type: {register_type}")

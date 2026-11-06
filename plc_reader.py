"""
plc_reader.py -- reading a block of PLC memory with ProtoProbe's own drivers. No Tk, so the wizard screen, the
command line tool (plcread.py) and the tests all share it.

    DriverReader(protocol, interface).read_region(region) -> {address: 16-bit word}

protocol: "mc" (Mitsubishi SLMP), "s7" (Siemens) or "modbus_tcp". The driver libraries are imported only when needed,
so reading a Mitsubishi PLC needs no extra install.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "edge-gateway"))

PROTOCOL_KEY = {"mc": "mc_protocol_3e", "s7": "s7comm", "modbus_tcp": "modbus_tcp", "modbus_rtu": "modbus_rtu"}
BATCH = 200


def lazy_driver_factory(pkey, cfg):
    """A driver for a protocol key, importing its library only now."""
    if pkey == "mc_protocol_3e":
        from drivers.mc_driver import MCProtocolDriver
        return MCProtocolDriver("conn", cfg)
    if pkey == "s7comm":
        from drivers.s7_driver import S7Driver
        return S7Driver("conn", cfg)
    if pkey in ("modbus_tcp", "modbus_rtu"):
        from drivers.modbus_driver import ModbusDriver
        return ModbusDriver("conn", cfg, transport="tcp" if pkey == "modbus_tcp" else "rtu")
    raise ValueError(f"No driver for {pkey}")


def build_range_tags(protocol, params):
    """params varies by protocol -- see each branch. Returns a list of
    synthetic int16 tag dicts covering the requested range."""
    if protocol == "mc_protocol_3e":
        code = params["device_code"]
        return [
            {"name": f"{code}{n}", "device_code": code, "device_number": n,
             "data_type": "int16", "multiplier": 1.0, "offset": 0.0, "mqtt_key": f"{code}{n}"}
            for n in range(params["start"], params["end"] + 1)
        ]
    if protocol == "s7comm":
        area = params.get("area", "DB")
        db = params.get("db_number", 1)
        prefix = f"DB{db}." if area == "DB" else area
        return [
            {"name": f"{prefix}{n}", "area": area, "db_number": db, "start_byte": n,
             "data_type": "int16", "multiplier": 1.0, "offset": 0.0, "mqtt_key": f"{prefix}{n}"}
            for n in range(params["start"], params["end"] + 1, 2)
        ]
    # modbus_tcp / modbus_rtu
    rtype = params["register_type"]
    return [
        {"name": f"{rtype}[{a}]", "address": a, "register_type": rtype,
         "data_type": "int16", "multiplier": 1.0, "offset": 0.0, "mqtt_key": f"{rtype}[{a}]"}
        for a in range(params["start"], params["end"] + 1)
    ]


class DriverReader:
    """Reads a region of a real PLC with ProtoProbe's own drivers (the same path the Scan feature uses)."""
    def __init__(self, protocol, interface, driver_factory=None, build_range_tags=None):
        self.protocol = protocol
        self.pkey = PROTOCOL_KEY[protocol]
        self.interface = interface
        self.driver_factory = driver_factory or lazy_driver_factory
        self.build_range_tags = build_range_tags or globals()["build_range_tags"]

    def _params(self, region):
        if self.protocol == "mc":
            return {"device_code": region.area, "start": region.start, "end": region.end}
        if self.protocol == "s7":
            return {"area": region.area, "db_number": region.db or 1, "start": region.start, "end": region.end}
        return {"register_type": region.area, "start": region.start, "end": region.end}

    @staticmethod
    def _addr_of(protocol, tag):
        return tag["device_number"] if protocol == "mc" else tag["start_byte"] if protocol == "s7" else tag["address"]

    def read_region(self, region, stop=None):
        tags = self.build_range_tags(self.pkey, self._params(region))
        is_modbus = self.protocol.startswith("modbus")
        cfg = ({"interface": self.interface, "devices": [{"unit_id": self.interface.get("unit_id", 1), "tags": []}]} if is_modbus
               else {"interface": self.interface, "tags": []})
        fac = self.driver_factory
        driver = fac[self.pkey](cfg) if isinstance(fac, dict) else fac(self.pkey, cfg)

        async def run():
            await driver.connect()
            words = {}
            try:
                for i in range(0, len(tags), BATCH):
                    if stop is not None and stop.is_set():
                        break
                    batch = tags[i:i + BATCH]
                    if is_modbus:
                        driver.config["devices"][0]["tags"] = batch
                    else:
                        driver.config["tags"] = batch
                    values = await driver.poll()
                    for t in batch:
                        if t["name"] in values:
                            words[self._addr_of(self.protocol, t)] = int(values[t["name"]]) & 0xFFFF
            finally:
                await driver.disconnect()
            return words

        return asyncio.run(run())

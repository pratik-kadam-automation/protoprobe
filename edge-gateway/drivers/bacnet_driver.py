"""
bacnet_driver.py

BACnet/IP read client via bacpypes3.

This is the heaviest dependency in the driver set for a 256MB
device -- bacpypes3 brings in its own asyncio application stack and
device-discovery machinery. It's imported lazily (only if a bacnet
polling group is actually enabled in config.json) so a gateway that
never touches BACnet never pays the RAM cost of importing it.

*** VERIFY THIS AGAINST YOUR ACTUAL DEVICE ***
Unlike the FX5U driver (proven against real hardware, error by
error) or S7comm (proven for DB reads), this driver has never been
run against a real BACnet device -- there wasn't one available while
building it. The Address()/ObjectIdentifier() usage below follows a
working reference implementation (a real bacpypes3 troubleshooting
script for a Mitsubishi AE200 controller, see
github.com/JoelBender/BACpypes3/issues/99), which is meaningfully
more confidence than a blind guess, but it's still unverified here.
Before trusting a wide read, confirm one known point first -- read a
value you can also see on the device's own display or an existing
BMS screen, and check it matches.

If BACnet load turns out to be a real problem on this hardware, the
cheaper alternative is a minimal hand-rolled Who-Is/I-Am + ReadProperty
implementation over raw UDP (mirroring what mc_driver.py does for MC
protocol) -- flagging that as a fallback path, not building it
preemptively since bacpypes3 covers correctness better for a first pass.
"""

import asyncio
import logging
from .base_driver import BaseDriver

logger = logging.getLogger("edge_collector.bacnet")


class BACnetDriver(BaseDriver):
    def __init__(self, group_id, group_config):
        super().__init__(group_id, group_config)
        self.app = None
        self._bacpypes3 = None
        self._dest = None
        self._device_oid = None

    async def connect(self):
        try:
            import bacpypes3.app
            import bacpypes3.local.device
            from bacpypes3.pdu import Address
            from bacpypes3.primitivedata import ObjectIdentifier
        except ImportError:
            raise RuntimeError(
                "bacpypes3 not installed -- pip install bacpypes3 "
                "(consider whether this device has RAM headroom for it)"
            )

        self._bacpypes3 = bacpypes3
        iface = self.config["interface"]

        device_obj = bacpypes3.local.device.LocalDeviceObject(
            objectName="edge-collector",
            objectIdentifier=iface["device_instance"],
            maxApduLengthAccepted=1024,
            segmentationSupported="segmentedBoth",
        )
        self.app = bacpypes3.app.Application.from_object_name_and_address(
            device_obj, f"{iface['local_ip']}/24"
        )

        device_ip = iface.get("device_ip")
        if not device_ip:
            raise RuntimeError(
                f"[{self.group_id}] BACnet interface config is missing 'device_ip' "
                f"(the target device's own IP address -- distinct from 'local_ip', "
                f"which is this client's own bind address)"
            )
        port = iface.get("device_port", 47808)
        self._dest = Address(f"{device_ip}:{port}")

        remote_instance = iface.get("remote_device_instance")
        if remote_instance is not None:
            self._device_oid = ObjectIdentifier(("device", int(remote_instance)))

        self.connected = True
        logger.info("Connected: %s", self.group_id)

    async def disconnect(self):
        if self.app:
            self.app.close()
        self.connected = False

    async def poll(self) -> dict:
        if not self.connected:
            await self.connect()

        from bacpypes3.primitivedata import ObjectIdentifier

        results = {}
        for tag in self.config["tags"]:
            try:
                oid = ObjectIdentifier((tag["object_type"], tag["object_instance"]))
                value = await asyncio.wait_for(
                    self.app.read_property(self._dest, oid, "presentValue"),
                    timeout=3.0,
                )
                scaled = self.apply_scaling(
                    float(value), tag.get("multiplier", 1.0), tag.get("offset", 0.0)
                )
                results[tag["mqtt_key"]] = scaled
            except asyncio.TimeoutError:
                self.connected = False
                raise ConnectionError(f"[{self.group_id}] BACnet read timeout")
            except Exception as e:
                logger.warning(
                    "[%s] tag %s read error: %s", self.group_id, tag["name"], e
                )
        return results

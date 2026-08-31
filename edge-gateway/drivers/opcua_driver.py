"""
opcua_driver.py

Generic OPC UA polling client via asyncua. This is the same driver
you'd point at the Sumitomo injection molding machine's EUROMAP 77
server -- config.json just needs the right node IDs.

Note: asyncua keeps a persistent session (subscriptions would be
cheaper than polling, but polling matches this project's uniform
"poll every N seconds" model across all protocols, so we stick with
that for consistency and simplicity on constrained hardware).
"""

import asyncio
import logging
from .base_driver import BaseDriver

logger = logging.getLogger("edge_collector.opcua")

try:
    from asyncua import Client
except ImportError:  # pragma: no cover
    Client = None


class OPCUADriver(BaseDriver):
    def __init__(self, group_id, group_config):
        super().__init__(group_id, group_config)
        self.client = None
        self._nodes = {}  # mqtt_key -> Node object, cached after first resolve

    async def connect(self):
        if Client is None:
            raise RuntimeError("asyncua not installed -- pip install asyncua")

        iface = self.config["interface"]
        self.client = Client(url=iface["endpoint"])
        await self.client.connect()
        self.connected = True
        logger.info("Connected: %s", self.group_id)

        for tag in self.config["tags"]:
            node = self.client.get_node(tag["node_id"])
            self._nodes[tag["mqtt_key"]] = (node, tag)

    async def disconnect(self):
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.connected = False

    async def poll(self) -> dict:
        if not self.connected:
            await self.connect()

        results = {}
        for mqtt_key, (node, tag) in self._nodes.items():
            try:
                raw = await asyncio.wait_for(node.read_value(), timeout=2.0)
                scaled = self.apply_scaling(
                    raw, tag.get("multiplier", 1.0), tag.get("offset", 0.0)
                )
                results[mqtt_key] = scaled
            except asyncio.TimeoutError:
                self.connected = False
                raise ConnectionError(f"[{self.group_id}] OPC UA read timeout")
            except Exception as e:
                logger.warning(
                    "[%s] tag %s read error: %s", self.group_id, tag["name"], e
                )
        return results

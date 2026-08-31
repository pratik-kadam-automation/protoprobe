#!/usr/bin/env python3
"""
edge_collector.py

Main engine: for each enabled polling_group in config.json, runs an
independent asyncio task that polls its driver on its own interval,
applies report-by-exception (deadband) + heartbeat logic per tag,
and writes resulting payloads to the local buffer. A separate
publisher task drains the buffer to MQTT continuously, independent
of whether any given polling group is currently healthy -- a stuck
PLC on one protocol never blocks telemetry from the others, and a
dead network never blocks polling.

Run under procd (see etc/init.d/edge-collector) so a crash triggers
an automatic restart rather than silently going dark.
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

from buffer_manager import BufferManager
from drivers.modbus_driver import ModbusDriver
from drivers.s7_driver import S7Driver
from drivers.mc_driver import MCProtocolDriver
from drivers.opcua_driver import OPCUADriver
from drivers.bacnet_driver import BACnetDriver

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("edge_collector")

CONFIG_PATH = os.environ.get("EDGE_CONFIG", "/etc/edge-collector/config.json")

DRIVER_FACTORY = {
    "modbus_tcp": lambda gid, cfg: ModbusDriver(gid, cfg, transport="tcp"),
    "modbus_rtu": lambda gid, cfg: ModbusDriver(gid, cfg, transport="rtu"),
    "s7comm": lambda gid, cfg: S7Driver(gid, cfg),
    "mc_protocol_3e": lambda gid, cfg: MCProtocolDriver(gid, cfg),
    "opcua": lambda gid, cfg: OPCUADriver(gid, cfg),
    "bacnet_ip": lambda gid, cfg: BACnetDriver(gid, cfg),
}


def load_config(path: str) -> dict:
    with open(path, "r") as f:
        return json.load(f)


class TagState:
    """Tracks last-sent value + timestamp per tag for RBE + heartbeat."""

    def __init__(self):
        self.last_value = {}
        self.last_sent_ts = {}

    def should_send(self, key, value, deadband, heartbeat_s, now):
        last_val = self.last_value.get(key)
        last_ts = self.last_sent_ts.get(key, 0)

        changed = last_val is None or abs(value - last_val) >= deadband
        heartbeat_due = (now - last_ts) >= heartbeat_s

        if changed or heartbeat_due:
            self.last_value[key] = value
            self.last_sent_ts[key] = now
            return True
        return False


class PollingGroupRunner:
    def __init__(self, group_cfg: dict, buffer: BufferManager, device_cfg: dict,
                 transmission_cfg: dict, publish_topic_template: str):
        self.group_id = group_cfg["group_id"]
        self.protocol = group_cfg["protocol"]
        self.cfg = group_cfg
        self.buffer = buffer
        self.device_cfg = device_cfg
        self.interval_s = group_cfg.get("poll_interval_s", 5)
        self.heartbeat_s = transmission_cfg.get("heartbeat_interval_s", 60)
        self.publish_topic = publish_topic_template.format(
            deviceId=device_cfg["deviceId"]
        )
        self.state = TagState()

        factory = DRIVER_FACTORY.get(self.protocol)
        if factory is None:
            raise ValueError(f"No driver registered for protocol: {self.protocol}")
        self.driver = factory(self.group_id, group_cfg)

    def _tag_deadband_lookup(self) -> dict:
        """Build mqtt_key -> deadband map, tolerant of the driver's tag layout
        (flat 'tags' list vs nested 'devices[].tags')."""
        lookup = {}
        if "tags" in self.cfg:
            for t in self.cfg["tags"]:
                lookup[t["mqtt_key"]] = t.get("deadband", 0)
        if "devices" in self.cfg:
            for d in self.cfg["devices"]:
                for t in d["tags"]:
                    lookup[t["mqtt_key"]] = t.get("deadband", 0)
        return lookup

    async def run(self):
        deadbands = self._tag_deadband_lookup()
        backoff_s = 2

        while True:
            cycle_start = time.monotonic()
            try:
                raw_metrics = await self.driver.poll()
                backoff_s = 2  # reset backoff after a healthy cycle

                now = time.time()
                metrics_to_send = {}
                for key, value in raw_metrics.items():
                    db = deadbands.get(key, 0)
                    if self.state.should_send(key, value, db, self.heartbeat_s, now):
                        metrics_to_send[key] = value

                if metrics_to_send:
                    payload = {
                        "deviceId": self.device_cfg["deviceId"],
                        "timestamp": int(now),
                        "group": self.group_id,
                        "metrics": metrics_to_send,
                    }
                    self.buffer.enqueue(self.publish_topic, payload)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("[%s] poll cycle failed: %s", self.group_id, e)
                try:
                    await self.driver.disconnect()
                except Exception:
                    pass
                await asyncio.sleep(backoff_s)
                backoff_s = min(backoff_s * 2, 60)
                continue

            elapsed = time.monotonic() - cycle_start
            await asyncio.sleep(max(0, self.interval_s - elapsed))


class MQTTPublisher:
    """
    Drains BufferManager to the cloud broker continuously. Runs
    independently of the polling groups -- store-and-forward means
    polling never waits on network state.
    """

    def __init__(self, buffer: BufferManager, cloud_cfg: dict, provider: str,
                 batch_size: int, rate_limit_per_s: int):
        self.buffer = buffer
        self.provider_cfg = cloud_cfg[provider]
        self.batch_size = batch_size
        self.min_interval = 1.0 / max(rate_limit_per_s, 1)
        self.client = None
        self._connected = asyncio.Event()

    def _build_client(self):
        import paho.mqtt.client as mqtt

        cfg = self.provider_cfg
        client_id = cfg.get("client_id", cfg.get("username", "edge-collector"))
        client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)

        client.tls_set(
            ca_certs=cfg["ca_cert"],
            certfile=cfg.get("client_cert"),
            keyfile=cfg.get("private_key"),
        )

        if "username" in cfg:
            password = os.environ.get(cfg.get("password_env", ""), "")
            client.username_pw_set(cfg["username"], password)

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        return client

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logger.info("MQTT connected")
            self._connected.set()
        else:
            logger.error("MQTT connect failed, rc=%s", rc)

    def _on_disconnect(self, client, userdata, rc):
        logger.warning("MQTT disconnected, rc=%s", rc)
        self._connected.clear()

    async def run(self):
        self.client = self._build_client()
        cfg = self.provider_cfg
        backoff_schedule = [2, 5, 10, 30, 60]
        backoff_idx = 0

        while True:
            try:
                self.client.connect(cfg["host"], cfg.get("port", 8883), keepalive=30)
                self.client.loop_start()
                await asyncio.wait_for(self._connected.wait(), timeout=15)
                backoff_idx = 0
                await self._drain_loop()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("MQTT connection error: %s", e)
            finally:
                try:
                    self.client.loop_stop()
                    self.client.disconnect()
                except Exception:
                    pass
                self._connected.clear()

            wait_s = backoff_schedule[min(backoff_idx, len(backoff_schedule) - 1)]
            backoff_idx += 1
            await asyncio.sleep(wait_s)

    async def _drain_loop(self):
        while self._connected.is_set():
            rows = self.buffer.peek_batch(self.batch_size)
            if not rows:
                await asyncio.sleep(1.0)
                continue

            sent_ids = []
            for row_id, ts, topic, payload_json in rows:
                if not self._connected.is_set():
                    break
                result = self.client.publish(topic, payload_json, qos=1)
                result.wait_for_publish(timeout=5)
                if result.is_published():
                    sent_ids.append(row_id)
                else:
                    logger.warning("Publish not confirmed for row %s", row_id)
                    break
                await asyncio.sleep(self.min_interval)

            if sent_ids:
                self.buffer.delete_ids(sent_ids)


async def main():
    config = load_config(CONFIG_PATH)

    device_cfg = config["device"]
    cloud_cfg = config["cloud"]
    transmission_cfg = config["transmission"]
    buffer_cfg = config["buffer"]

    buffer = BufferManager(buffer_cfg["path"], buffer_cfg.get("max_rows", 200_000))

    provider = cloud_cfg["provider"]
    publish_topic_template = cloud_cfg[provider]["publish_topic"]

    tasks = []

    publisher = MQTTPublisher(
        buffer,
        cloud_cfg,
        provider,
        batch_size=buffer_cfg.get("backfill_batch_size", 20),
        rate_limit_per_s=buffer_cfg.get("backfill_rate_limit_per_s", 10),
    )
    tasks.append(asyncio.create_task(publisher.run(), name="mqtt_publisher"))

    for group_cfg in config["polling_groups"]:
        if not group_cfg.get("enabled", False):
            logger.info("Skipping disabled group: %s", group_cfg["group_id"])
            continue
        runner = PollingGroupRunner(
            group_cfg, buffer, device_cfg, transmission_cfg, publish_topic_template
        )
        tasks.append(
            asyncio.create_task(runner.run(), name=f"poll_{group_cfg['group_id']}")
        )

    if len(tasks) == 1:
        logger.warning("No polling groups enabled -- publisher running idle")

    stop_event = asyncio.Event()

    def _handle_signal():
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal)

    await stop_event.wait()

    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    buffer.close()


if __name__ == "__main__":
    asyncio.run(main())

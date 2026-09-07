from __future__ import annotations

import asyncio
import json
import logging
import time

from bleak import BleakClient, BleakScanner

from .config import Config
from .publishing import publish_sensor, publish_victron, utc_now
from .temperature import PARSERS, parse_inkbird_gatt
from .victron import decode_victron

logger = logging.getLogger(__name__)
INKBIRD_CHARACTERISTIC = "0000fff2-0000-1000-8000-00805f9b34fb"


class BluetoothCleanupError(RuntimeError):
    """Ownership is uncertain: exit the process before acquiring Bluetooth again."""


async def read_inkbird_gatt(device, values, config: Config, client_factory=BleakClient):
    # Passing the discovered BLEDevice prevents BleakClient from implicitly
    # creating a discovery scanner to resolve a plain MAC address.
    client = client_factory(device, timeout=config.gatt_timeout)
    try:
        await asyncio.wait_for(client.connect(), config.gatt_timeout)
        data = await asyncio.wait_for(client.read_gatt_char(INKBIRD_CHARACTERISTIC), config.gatt_read_timeout)
        return parse_inkbird_gatt(bytes(data), values)
    finally:
        # Also disconnect if connect partially succeeded, timed out or was cancelled.
        try:
            await asyncio.wait_for(client.disconnect(), config.cleanup_timeout)
        except Exception:
            raise BluetoothCleanupError("GATT cleanup failed; process restart required") from None


class Gateway:
    """One owner loop serializes scanner lifecycle and every GATT operation.

    Callbacks only decode/store observations. They never start tasks, connect
    clients, or manipulate discovery. Only this loop can create a scanner.
    """

    def __init__(self, config: Config, publisher, scanner_factory=BleakScanner,
                 client_factory=BleakClient, monotonic=time.monotonic):
        self.config = config
        self.publisher = publisher
        self.scanner_factory = scanner_factory
        self.client_factory = client_factory
        self.monotonic = monotonic
        self.scanner = None
        self.scanner_state = "stopped"
        self.sensors = {sensor.address: sensor for sensor in config.sensors}
        self.victron = {device.address: device for device in config.victron_devices}
        self.temperature_readings = {}
        self.victron_readings = {}
        self.victron_published = {}
        self.victron_raw = {}
        self.victron_names = dict(config.victron_names)
        self.missed = {sensor.address: 0 for sensor in config.sensors}
        self.seen = set()
        self.last_advertisement = None
        self.last_activity = monotonic()
        self.gatt_operations = 0
        self.scanner_restarts = 0
        self.errors_logged = {}

    def warning(self, address, kind, exc):
        # Never interpolate exception text: third-party errors may echo inputs.
        key = (address, kind)
        now = self.monotonic()
        if now - self.errors_logged.get(key, float("-inf")) >= 60:
            logger.warning("%s for %s (%s)", kind, address, type(exc).__name__)
            self.errors_logged[key] = now

    def on_advertisement(self, device, advertisement):
        self.last_activity = self.monotonic()
        self.last_advertisement = utc_now()
        address = device.address.upper()
        sensor = self.sensors.get(address)
        victron = self.victron.get(address)
        if sensor is None and victron is None:
            return
        if address not in self.seen:
            self.seen.add(address)
            logger.info("%s device seen: %s", "Temperature" if sensor else "Victron", address)
        if sensor is not None:
            try:
                values = PARSERS[sensor.protocol](advertisement)
                if values is not None:
                    values = {"timestamp": utc_now(), **values}
                    self.temperature_readings[address] = (sensor, values, advertisement.rssi, device)
            except Exception as exc:
                self.warning(address, "Temperature decoding failed", exc)
        if victron is not None:
            raw = advertisement.manufacturer_data.get(0x02E1)
            if not raw or raw == self.victron_raw.get(address):
                return
            try:
                payload = decode_victron(raw, victron.key)
                if payload is None:
                    return
                name = self.victron_names.get(address) or device.name or advertisement.local_name
                if name:
                    self.victron_names[address] = name
                self.victron_readings[address] = {
                    "timestamp": utc_now(), "name": name or address,
                    "address": device.address, "rssi": advertisement.rssi, "payload": payload,
                }
                self.victron_raw[address] = raw
            except Exception as exc:
                self.warning(address, "Victron decoding failed", exc)

    async def start_scanner(self):
        if self.scanner is not None:
            raise RuntimeError("Scanner already owned")
        self.scanner_state = "starting"
        self.scanner = self.scanner_factory(
            detection_callback=self.on_advertisement,
            bluez={"adapter": self.config.adapter, "filters": {"DuplicateData": True}},
        )
        try:
            await asyncio.wait_for(self.scanner.start(), self.config.scanner_timeout)
        except TimeoutError:
            # BlueZ may have accepted StartDiscovery before its reply timed out.
            # Bleak has no stop handle yet in that case; scanner.stop() is a no-op.
            # Exit so DBus releases our lease instead of starting another scanner.
            self.scanner_state = "cleanup_failed"
            raise BluetoothCleanupError("Scanner start timed out with uncertain ownership") from None
        self.last_activity = self.monotonic()
        self.scanner_state = "running"
        logger.info("BLE scanner started: %s", self.config.adapter)

    async def stop_scanner(self):
        if self.scanner is None:
            return
        self.scanner_state = "stopping"
        try:
            await asyncio.wait_for(self.scanner.stop(), self.config.cleanup_timeout)
        except Exception:
            # Bleak clears its stop callback before awaiting StopDiscovery.
            # Retrying stop on that object could appear to succeed without
            # releasing discovery. Do not create another scanner in this process.
            self.scanner_state = "cleanup_failed"
            raise BluetoothCleanupError("Scanner cleanup failed; process restart required") from None
        self.scanner = None
        self.scanner_state = "stopped"

    def publish_victron_ready(self):
        now = self.monotonic()
        for address, reading in list(self.victron_readings.items()):
            if now - self.victron_published.get(address, float("-inf")) >= self.config.victron_interval:
                publish_victron(self.publisher, self.config.victron_topic, reading)
                self.victron_published[address] = now
                del self.victron_readings[address]

    @staticmethod
    def needs_gatt(sensor, values):
        return sensor.protocol == "inkbird" and (
            sensor.gatt == "always" or
            (sensor.gatt == "auto" and values["model"] == "Inkbird IBS-TH/IBS-TH2")
        )

    async def temperature_cycle(self):
        readings, self.temperature_readings = self.temperature_readings, {}
        found = set()
        paused = False
        for address, (sensor, values, rssi, device) in readings.items():
            if self.needs_gatt(sensor, values):
                if not paused:
                    await self.stop_scanner()
                    self.scanner_state = "paused_for_gatt"
                    paused = True
                values = await self.read_sensor_gatt(sensor, device, values)
                if values is None:
                    continue
            publish_sensor(self.publisher, self.config.temperature_topic, sensor, values, rssi)
            found.add(address)
        for address, sensor in self.sensors.items():
            self.missed[address] = 0 if address in found else self.missed[address] + 1
            if self.missed[address] >= self.config.missed_cycles:
                self.publisher.publish(f"{self.config.temperature_topic}/{sensor.identifier}/availability", "offline")
        summary = {
            "timestamp": utc_now(), "found": len(found), "expected": len(self.sensors),
            "missing": [s.name for a, s in self.sensors.items() if a not in found],
            "offline": [s.name for a, s in self.sensors.items() if self.missed[a] >= self.config.missed_cycles],
            "missed_cycles": {s.name: self.missed[a] for a, s in self.sensors.items()},
        }
        self.publisher.publish(f"{self.config.temperature_topic}/scan", json.dumps(summary, ensure_ascii=False))
        # The next owner-loop iteration resumes scanning, even if a device failed.

    async def read_sensor_gatt(self, sensor, device, values):
        for attempt in range(self.config.gatt_attempts):
            self.gatt_operations = 1
            self.publish_health()
            try:
                current = await read_inkbird_gatt(device, values, self.config, self.client_factory)
                if current is not None:
                    current["timestamp"] = utc_now()
                    logger.info("GATT read completed: %s", sensor.identifier)
                    return current
            except BluetoothCleanupError:
                self.scanner_state = "cleanup_failed"
                raise
            except Exception as exc:
                self.warning(sensor.address, "GATT read failed", exc)
            finally:
                self.gatt_operations = 0
            if attempt + 1 < self.config.gatt_attempts:
                await asyncio.sleep(min(2 ** attempt, self.config.retry_max))
        return None

    def publish_health(self):
        self.publisher.publish(self.config.health_topic, json.dumps({
            "timestamp": utc_now(), "scanner": self.scanner_state,
            "adapter": self.config.adapter, "devices_seen": len(self.seen),
            "last_advertisement": self.last_advertisement,
            "gatt_operations": self.gatt_operations, "scanner_restarts": self.scanner_restarts,
        }))

    async def run(self):
        next_temperature = self.monotonic() + self.config.initial_scan
        next_health = retry_at = 0.0
        retry_delay = min(1.0, self.config.retry_max)
        try:
            while True:
                now = self.monotonic()
                if self.scanner is None and now >= retry_at:
                    try:
                        await self.start_scanner()
                        retry_delay = min(1.0, self.config.retry_max)
                    except BluetoothCleanupError:
                        raise
                    except Exception as exc:
                        self.warning(self.config.adapter, "BLE scanner start failed", exc)
                        await self.stop_scanner()
                        self.scanner_state = "retrying"
                        self.scanner_restarts += 1
                        retry_at = self.monotonic() + retry_delay
                        retry_delay = min(retry_delay * 2, self.config.retry_max)
                elif self.scanner is not None and now - self.last_activity >= self.config.scanner_idle:
                    logger.warning("BLE scanner watchdog expired; restarting discovery")
                    await self.stop_scanner()
                    self.scanner_state = "retrying"
                    self.scanner_restarts += 1
                    retry_at = self.monotonic() + retry_delay
                    retry_delay = min(retry_delay * 2, self.config.retry_max)
                self.publish_victron_ready()
                if self.monotonic() >= next_temperature:
                    await self.temperature_cycle()
                    next_temperature = self.monotonic() + self.config.temperature_interval
                if self.monotonic() >= next_health:
                    self.publish_health()
                    next_health = self.monotonic() + self.config.health_interval
                self.publisher.flush()
                await asyncio.sleep(self.config.tick)
        finally:
            # No background scanner or per-advertisement tasks to abandon.
            if self.scanner_state != "cleanup_failed":
                await self.stop_scanner()
            self.publish_health()

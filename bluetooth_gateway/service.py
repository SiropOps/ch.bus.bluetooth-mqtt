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
from .runtime import BluetoothCleanupError, bounded

logger = logging.getLogger(__name__)
INKBIRD_CHARACTERISTIC = "0000fff2-0000-1000-8000-00805f9b34fb"


async def read_inkbird_gatt(device, values, config: Config, client_factory=BleakClient):
    # Passing the discovered BLEDevice prevents BleakClient from implicitly
    # creating a discovery scanner to resolve a plain MAC address.
    client = client_factory(device, timeout=config.gatt_timeout)
    try:
        await bounded(client.connect(), config.gatt_timeout, restart_delay=config.process_restart_delay)
        data = await bounded(client.read_gatt_char(INKBIRD_CHARACTERISTIC), config.gatt_read_timeout,
                             restart_delay=config.process_restart_delay)
        return parse_inkbird_gatt(bytes(data), values)
    finally:
        # Also disconnect if connect partially succeeded, timed out or was cancelled.
        try:
            await bounded(client.disconnect(), config.cleanup_timeout, restart_delay=config.process_restart_delay)
        except Exception:
            raise BluetoothCleanupError("GATT cleanup failed; process restart required") from None


class Gateway:
    """One owner loop serializes scanner lifecycle and every GATT operation.

    Callbacks only decode/store observations. They never start tasks, connect
    clients, or manipulate discovery. Only this loop can create a scanner.
    """

    def __init__(self, config: Config, publisher, scanner_factory=BleakScanner,
                 client_factory=BleakClient, monotonic=time.monotonic, sleep=asyncio.sleep):
        self.config = config
        self.publisher = publisher
        self.scanner_factory = scanner_factory
        self.client_factory = client_factory
        self.monotonic = monotonic
        self.sleep = sleep
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
        self.last_advertisement_at = None
        self.last_activity = monotonic()
        self.gatt_operations = 0
        self.scanner_restarts = 0
        self.errors_logged = {}
        self.last_gatt_success = None
        self.last_gatt_at = None
        self.consecutive_failures = 0
        self.gatt_failures = 0
        self.silence_recoveries = 0
        self.silence_interval = config.scanner_idle
        self.recovery_pending = False
        self.recovery_level = 0
        self.retry_delay = min(1.0, config.retry_max)

    def warning(self, address, kind, exc):
        # Never interpolate exception text: third-party errors may echo inputs.
        key = (address, kind)
        now = self.monotonic()
        if now - self.errors_logged.get(key, float("-inf")) >= 60:
            logger.warning("%s for %s (%s)", kind, address, type(exc).__name__)
            self.errors_logged[key] = now

    def on_advertisement(self, device, advertisement):
        if self.recovery_pending:
            logger.info("BLE recovery confirmed by advertisement; recovery_level=0")
        self.recovery_pending = False
        self.recovery_level = 0
        self.consecutive_failures = 0
        self.silence_recoveries = 0
        self.silence_interval = self.config.scanner_idle
        self.retry_delay = min(1.0, self.config.retry_max)
        self.last_activity = self.monotonic()
        self.last_advertisement_at = self.last_activity
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
            await bounded(self.scanner.start(), self.config.scanner_timeout,
                          restart_delay=self.config.process_restart_delay)
        except TimeoutError:
            # BlueZ may have accepted StartDiscovery before its reply timed out.
            # Bleak has no stop handle yet in that case; scanner.stop() is a no-op.
            # Exit so DBus releases our lease instead of starting another scanner.
            self.scanner_state = "cleanup_failed"
            self.recovery_level = 2
            raise BluetoothCleanupError("Scanner start timed out with uncertain ownership") from None
        self.last_activity = self.monotonic()
        self.scanner_state = "running"
        logger.info("BLE scanner started: %s", self.config.adapter)

    async def stop_scanner(self):
        if self.scanner is None:
            return
        self.scanner_state = "stopping"
        try:
            await bounded(self.scanner.stop(), self.config.cleanup_timeout,
                          restart_delay=self.config.process_restart_delay)
        except Exception:
            # Bleak clears its stop callback before awaiting StopDiscovery.
            # Retrying stop on that object could appear to succeed without
            # releasing discovery. Do not create another scanner in this process.
            self.scanner_state = "cleanup_failed"
            self.recovery_level = 2
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
                    self.last_gatt_success = utc_now()
                    self.last_gatt_at = self.monotonic()
                    self.gatt_failures = 0
                    current["timestamp"] = utc_now()
                    logger.info("GATT read completed: %s", sensor.identifier)
                    return current
            except BluetoothCleanupError:
                self.scanner_state = "cleanup_failed"
                self.recovery_level = 2
                raise
            except Exception as exc:
                self.gatt_failures += 1
                self.warning(sensor.address, "GATT read failed", exc)
            finally:
                self.gatt_operations = 0
            if attempt + 1 < self.config.gatt_attempts:
                await self.sleep(min(2 ** attempt, self.config.retry_max))
        return None

    def publish_health(self):
        now = self.monotonic()
        health = {
            "timestamp": utc_now(), "scanner": self.scanner_state,
            "adapter": self.config.adapter, "devices_seen": len(self.seen),
            "last_advertisement": self.last_advertisement,
            "gatt_operations": self.gatt_operations, "scanner_restarts": self.scanner_restarts,
            "advertisement_age_seconds": None if self.last_advertisement_at is None else now - self.last_advertisement_at,
            "last_gatt_success": self.last_gatt_success,
            "gatt_age_seconds": None if self.last_gatt_at is None else now - self.last_gatt_at,
            "consecutive_failures": self.consecutive_failures, "gatt_failures": self.gatt_failures,
            "silence_recoveries": self.silence_recoveries,
            "radio_state": "silent_or_unverified" if self.recovery_pending else "observed" if self.last_advertisement else "unverified",
            "recovery_level": self.recovery_level,
            "mqtt": self.publisher.health() if hasattr(self.publisher, "health") else {},
        }
        self.publisher.publish(self.config.health_topic, json.dumps(health))
        logger.info("BLE health %s", json.dumps(health))

    async def run(self):
        next_temperature = self.monotonic() + self.config.initial_scan
        next_health = retry_at = 0.0
        try:
            while True:
                now = self.monotonic()
                if self.scanner is None and now >= retry_at:
                    try:
                        await self.start_scanner()
                    except BluetoothCleanupError:
                        raise
                    except Exception as exc:
                        self.warning(self.config.adapter, "BLE scanner start failed", exc)
                        await self.stop_scanner()
                        self.scanner_state = "retrying"
                        self.scanner_restarts += 1
                        self.consecutive_failures += 1
                        self.recovery_level = 1
                        self.recovery_pending = True
                        if self.consecutive_failures >= self.config.failure_limit:
                            self.recovery_level = 2
                            raise BluetoothCleanupError("Repeated scanner failures; process restart required")
                        retry_at = self.monotonic() + self.retry_delay
                        self.retry_delay = min(self.retry_delay * 2, self.config.retry_max)
                elif self.scanner is not None and now - self.last_activity >= self.silence_interval:
                    logger.warning("BLE radio silent; recovery_level=1 restarting discovery (not proof of failure)")
                    await self.stop_scanner()
                    self.silence_recoveries += 1
                    self.silence_interval = min(self.silence_interval * 2,
                                               max(self.config.scanner_idle, self.config.silence_retry_max))
                    self.recovery_pending = True
                    self.recovery_level = 1
                    self.scanner_state = "retrying"
                    self.scanner_restarts += 1
                    retry_at = self.monotonic() + self.retry_delay
                    self.retry_delay = min(self.retry_delay * 2, self.config.retry_max)
                self.publish_victron_ready()
                if self.monotonic() >= next_temperature:
                    await self.temperature_cycle()
                    next_temperature = self.monotonic() + self.config.temperature_interval
                if self.monotonic() >= next_health:
                    self.publish_health()
                    next_health = self.monotonic() + self.config.health_interval
                self.publisher.flush()
                await self.sleep(self.config.tick)
        finally:
            # No background scanner or per-advertisement tasks to abandon.
            if self.scanner_state != "cleanup_failed":
                await self.stop_scanner()
            self.publish_health()

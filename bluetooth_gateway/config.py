from __future__ import annotations

import json
import math
import os
import re
import unicodedata
from dataclasses import dataclass, field


def topic_safe(value: str, *, ascii_names: bool = True) -> str:
    if ascii_names:
        value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9_-]+", "_", value.lower().strip()).strip("_") or "device"


def address(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", value.strip()):
        raise ValueError("Invalid Bluetooth MAC address")
    return value.strip().upper()


@dataclass(frozen=True)
class Sensor:
    name: str
    address: str
    protocol: str
    # auto reads 9-byte Inkbirds over GATT; 18-byte models use advertisements.
    gatt: str = "auto"

    @property
    def identifier(self) -> str:
        return topic_safe(self.name)


# Compatibility defaults from temperature-mqtt, replaceable as one JSON list.
DEFAULT_SENSORS = (
    Sensor("Ça pique", "E3:EE:E4:14:FA:B0", "ruuvi"),
    Sensor("Avalanche Toit", "9D:88:00:00:02:2C", "sensorblue"),
    Sensor("Fruit Storage", "49:22:11:08:18:64", "inkbird", "always"),
    Sensor("Tête used", "49:22:09:05:14:A1", "inkbird", "always"),
)


def parse_sensors(value: str | None) -> tuple[Sensor, ...]:
    if value is None:
        return DEFAULT_SENSORS
    try:
        rows = json.loads(value)
        if not isinstance(rows, list):
            raise ValueError
        sensors = tuple(Sensor(**row) for row in rows)
        for sensor in sensors:
            if (not isinstance(sensor.name, str) or not sensor.name.strip()
                    or sensor.protocol not in {"ruuvi", "sensorblue", "inkbird"}
                    or sensor.gatt not in {"auto", "always", "never"}
                    or sensor.identifier in {"status", "scan", "dht22"}):
                raise ValueError
        sensors = tuple(Sensor(s.name, address(s.address), s.protocol, s.gatt) for s in sensors)
        if len({s.address for s in sensors}) != len(sensors) or len({s.identifier for s in sensors}) != len(sensors):
            raise ValueError
        return sensors
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Invalid TEMPERATURE_SENSORS JSON; use unique names/MACs and supported protocols") from None


@dataclass(frozen=True)
class VictronDevice:
    address: str
    key: str = field(repr=False)


def parse_victron_devices(value: str) -> tuple[VictronDevice, ...]:
    result = []
    for index, entry in enumerate(filter(str.strip, value.split(",")), 1):
        mac, separator, key = entry.strip().partition("@")
        try:
            mac = address(mac)
            key = key.strip()
            if not separator or not re.fullmatch(r"[0-9a-fA-F]{32}", key):
                raise ValueError
            if any(device.address == mac for device in result):
                raise ValueError
        except ValueError:
            raise ValueError(f"Invalid VICTRON_DEVICES entry {index}; expected unique MAC@32_HEX_DIGITS") from None
        result.append(VictronDevice(mac, key))
    return tuple(result)


def positive(source: dict, name: str, default: float) -> float:
    try:
        value = float(source.get(name, default))
        if not math.isfinite(value) or value <= 0:
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise ValueError(f"{name} must be a finite positive number") from None


def topic(source: dict, name: str, default: str) -> str:
    value = source.get(name, default).strip().rstrip("/")
    if not value or any(c in value for c in ("#", "+", "\x00")):
        raise ValueError(f"Invalid {name}")
    return value


@dataclass(frozen=True)
class Config:
    sensors: tuple[Sensor, ...] = DEFAULT_SENSORS
    victron_devices: tuple[VictronDevice, ...] = field(default=(), repr=False)
    victron_names: dict[str, str] = field(default_factory=dict)
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_username: str = field(default="", repr=False)
    mqtt_password: str = field(default="", repr=False)
    temperature_topic: str = "van/temperature"
    victron_topic: str = "van/victron"
    status_topic: str = "van/bluetooth/status"
    health_topic: str = "van/bluetooth/health"
    adapter: str = "hci0"
    temperature_interval: float = 300
    victron_interval: float = 30
    initial_scan: float = 45
    missed_cycles: int = 3
    gatt_timeout: float = 20
    gatt_read_timeout: float = 10
    cleanup_timeout: float = 10
    gatt_attempts: int = 2
    scanner_timeout: float = 20
    scanner_idle: float = 120
    retry_max: float = 60
    health_interval: float = 30
    tick: float = 1

    @classmethod
    def from_env(cls, source: dict | None = None) -> Config:
        source = os.environ if source is None else source
        devices = parse_victron_devices(source.get("VICTRON_DEVICES", ""))
        try:
            names = json.loads(source.get("VICTRON_NAMES", "{}"))
            if not isinstance(names, dict):
                raise ValueError
            names = {address(mac): name for mac, name in names.items()}
            if any(not isinstance(name, str) or not name.strip() for name in names.values()):
                raise ValueError
            if not names.keys() <= {device.address for device in devices}:
                raise ValueError
            if len({topic_safe(name, ascii_names=False) for name in names.values()}) != len(names):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Invalid VICTRON_NAMES; use a JSON mapping of configured MACs to unique names") from None
        options = dict(
            sensors=parse_sensors(source.get("TEMPERATURE_SENSORS")),
            victron_devices=devices, victron_names=names,
            mqtt_host=source.get("MQTT_HOST", "127.0.0.1"),
            mqtt_username=source.get("MQTT_USERNAME", ""),
            mqtt_password=source.get("MQTT_PASSWORD", ""),
            adapter=source.get("BLUETOOTH_ADAPTER", "hci0"),
        )
        for name, env_name, default in (
            ("temperature_interval", "TEMPERATURE_READ_INTERVAL_SECONDS", 300),
            ("victron_interval", "VICTRON_READ_INTERVAL_SECONDS", 30),
            ("initial_scan", "SCAN_TIMEOUT_SECONDS", 45),
            ("missed_cycles", "MISSED_CYCLES_BEFORE_OFFLINE", 3),
            ("gatt_timeout", "INKBIRD_GATT_TIMEOUT_SECONDS", 20),
            ("gatt_read_timeout", "GATT_READ_TIMEOUT_SECONDS", 10),
            ("cleanup_timeout", "BLE_CLEANUP_TIMEOUT_SECONDS", 10),
            ("gatt_attempts", "GATT_ATTEMPTS", 2),
            ("scanner_timeout", "SCANNER_OPERATION_TIMEOUT_SECONDS", 20),
            ("scanner_idle", "SCANNER_IDLE_TIMEOUT_SECONDS", 120),
            ("retry_max", "BLUETOOTH_RETRY_MAX_SECONDS", 60),
            ("health_interval", "HEALTH_INTERVAL_SECONDS", 30),
            ("mqtt_port", "MQTT_PORT", 1883),
        ):
            value = positive(source, env_name, default)
            if name in {"missed_cycles", "gatt_attempts", "mqtt_port"}:
                if not value.is_integer():
                    raise ValueError(f"{env_name} must be an integer")
                value = int(value)
            options[name] = value
        if options["mqtt_port"] > 65535 or options["gatt_attempts"] > 5:
            raise ValueError("MQTT_PORT must be <= 65535 and GATT_ATTEMPTS <= 5")
        if not re.fullmatch(r"hci\d+", options["adapter"]):
            raise ValueError("BLUETOOTH_ADAPTER must be an hci adapter name")
        for name, env_name, default in (
            ("temperature_topic", "MQTT_TEMPERATURE_BASE_TOPIC", "van/temperature"),
            ("victron_topic", "MQTT_VICTRON_BASE_TOPIC", "van/victron"),
            ("status_topic", "MQTT_STATUS_TOPIC", "van/bluetooth/status"),
            ("health_topic", "MQTT_HEALTH_TOPIC", "van/bluetooth/health"),
        ):
            options[name] = topic(source, env_name, default)
        return cls(**options)

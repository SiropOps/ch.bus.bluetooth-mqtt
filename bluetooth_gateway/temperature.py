"""Temperature advertisement/GATT decoders migrated without MQTT or hardware I/O."""
from __future__ import annotations
import struct
from typing import Any
from bleak.backends.scanner import AdvertisementData

THERMOBEACON_MANUFACTURER_IDS = {0x10, 0x11, 0x14, 0x15, 0x18, 0x1B, 0x30}


def valid_environment(temperature: float, humidity: float | None = None) -> bool:
    return -80 <= temperature <= 150 and (humidity is None or 0 <= humidity <= 100)


def parse_ruuvi(advertisement: AdvertisementData) -> dict[str, Any] | None:
    data = advertisement.manufacturer_data.get(0x0499)
    if not data:
        return None

    if data[0] == 5 and len(data) >= 24:
        temperature_raw = int.from_bytes(data[1:3], "big", signed=True)
        humidity_raw = int.from_bytes(data[3:5], "big")
        pressure_raw = int.from_bytes(data[5:7], "big")
        power_raw = int.from_bytes(data[13:15], "big")
        values = {
            "model": "RuuviTag",
            "data_format": 5,
            "temperature": round(temperature_raw * 0.005, 3),
            "humidity": round(humidity_raw * 0.0025, 4),
            "pressure": pressure_raw + 50000,
            "acceleration_x": int.from_bytes(data[7:9], "big", signed=True),
            "acceleration_y": int.from_bytes(data[9:11], "big", signed=True),
            "acceleration_z": int.from_bytes(data[11:13], "big", signed=True),
            "battery_voltage": ((power_raw >> 5) + 1600) / 1000,
            "tx_power": (power_raw & 0x1F) * 2 - 40,
            "movement_counter": data[15],
            "measurement_sequence": int.from_bytes(data[16:18], "big"),
        }
    elif data[0] == 3 and len(data) >= 14:
        temperature = data[2] + data[3] / 100
        if data[2] & 0x80:
            temperature = -(data[2] & 0x7F) - data[3] / 100
        values = {
            "model": "RuuviTag",
            "data_format": 3,
            "temperature": round(temperature, 2),
            "humidity": data[1] * 0.5,
            "pressure": int.from_bytes(data[4:6], "big") + 50000,
            "acceleration_x": int.from_bytes(data[6:8], "big", signed=True),
            "acceleration_y": int.from_bytes(data[8:10], "big", signed=True),
            "acceleration_z": int.from_bytes(data[10:12], "big", signed=True),
            "battery_voltage": int.from_bytes(data[12:14], "big") / 1000,
        }
    else:
        return None

    if not valid_environment(values["temperature"], values["humidity"]):
        return None
    return values


def parse_sensorblue(advertisement: AdvertisementData) -> dict[str, Any] | None:
    for manufacturer_id, payload in advertisement.manufacturer_data.items():
        if manufacturer_id not in THERMOBEACON_MANUFACTURER_IDS:
            continue
        data = manufacturer_id.to_bytes(2, "little") + payload
        if len(data) != 20:
            continue

        voltage_mv, temperature_raw, humidity_raw = struct.unpack("<HhH", data[10:16])
        temperature = temperature_raw / 16
        humidity = humidity_raw / 16
        if not valid_environment(temperature, humidity):
            continue

        if voltage_mv >= 3000:
            battery = 100
        elif voltage_mv >= 2600:
            battery = 60 + (voltage_mv - 2600) * 0.1
        elif voltage_mv >= 2500:
            battery = 40 + (voltage_mv - 2500) * 0.2
        elif voltage_mv >= 2450:
            battery = 20 + (voltage_mv - 2450) * 0.4
        else:
            battery = 0

        return {
            "model": "SensorBlue/ThermoBeacon",
            "temperature": round(temperature, 2),
            "humidity": round(humidity, 2),
            "battery": round(battery),
            "battery_voltage": round(voltage_mv / 1000, 3),
            "button_pressed": bool(data[3] & 0x80),
        }
    return None


def parse_inkbird(advertisement: AdvertisementData) -> dict[str, Any] | None:
    if not advertisement.manufacturer_data:
        return None

    # BlueZ may accumulate entries because 9-byte Inkbirds put the temperature
    # in the manufacturer ID. This advertisement value is only provisional:
    # The gateway refreshes these models directly over GATT before publishing.
    manufacturer_id, payload = next(
        reversed(advertisement.manufacturer_data.items())
    )
    data = manufacturer_id.to_bytes(2, "little") + payload
    if len(data) == 9:
        temperature_raw, humidity_raw = struct.unpack("<hH", data[0:4])
        temperature = temperature_raw / 100
        humidity = humidity_raw / 100
        battery = data[7]
        model = "Inkbird IBS-TH/IBS-TH2"
    elif len(data) == 18:
        temperature_raw, humidity_raw = struct.unpack("<hH", data[6:10])
        temperature = temperature_raw / 10
        humidity = humidity_raw / 10
        battery = data[10]
        model = "Inkbird IBS-TH (18-byte)"
    else:
        return None

    if not valid_environment(temperature, humidity) or not 0 <= battery <= 100:
        return None
    values: dict[str, Any] = {
        "model": model,
        "temperature": round(temperature, 2),
        "battery": battery,
    }
    if humidity_raw != 0:
        values["humidity"] = round(humidity, 2)
    return values


def parse_inkbird_gatt(
    data: bytes, advertisement_values: dict[str, Any]
) -> dict[str, Any] | None:
    """Decode the current IBS-TH/IBS-TH2 value read from GATT FFF2."""
    if len(data) < 4:
        return None

    temperature_raw, humidity_raw = struct.unpack("<hH", data[:4])
    temperature = temperature_raw / 100
    humidity = humidity_raw / 100
    if not valid_environment(temperature, humidity):
        return None

    values = advertisement_values.copy()
    values["model"] = "Inkbird IBS-TH/IBS-TH2"
    values["temperature"] = round(temperature, 2)
    if humidity_raw:
        values["humidity"] = round(humidity, 2)
    else:
        values.pop("humidity", None)
    return values

PARSERS = {"ruuvi": parse_ruuvi, "sensorblue": parse_sensorblue, "inkbird": parse_inkbird}

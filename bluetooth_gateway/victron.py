"""Decode externally acquired advertisements; never instantiate victron_ble.Scanner."""
import inspect
from enum import Enum

from victron_ble.devices import detect_device_type


def decode_victron(raw: bytes, key: str) -> dict | None:
    if len(raw) < 9 or raw[0] != 0x10:
        return None
    parser = detect_device_type(raw)
    if parser is None:
        return None
    parsed = parser(key).parse(raw)
    # Match victron_ble 0.9.3 DeviceDataEncoder, including omission of None.
    result = {}
    for name, method in inspect.getmembers(parsed, predicate=inspect.ismethod):
        if name.startswith("get_"):
            value = method()
            if isinstance(value, Enum):
                value = value.name.lower()
            if value is not None:
                result[name[4:]] = value
    return result

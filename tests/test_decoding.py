import json
import struct
from dataclasses import replace
from types import SimpleNamespace

import pytest

from bluetooth_gateway.config import Config, Sensor, VictronDevice, parse_sensors, parse_victron_devices, topic_safe
from bluetooth_gateway.publishing import publish_sensor
from bluetooth_gateway.service import Gateway
from bluetooth_gateway.temperature import PARSERS, parse_inkbird, parse_inkbird_gatt
from bluetooth_gateway.victron import decode_victron
from tests.conftest import advertisement, frames

TEST_KEY = "00" * 16


@pytest.mark.parametrize("row", frames("victron.json"), ids=lambda row: row["name"])
def test_real_victron_decoder_to_json_model_and_scalar_topics(row, publications):
    config = Config(sensors=(), victron_devices=(VictronDevice(row["address"], TEST_KEY),))
    gateway = Gateway(config, publications)
    gateway.on_advertisement(*advertisement(row))
    gateway.publish_victron_ready()
    topic = f"van/victron/{topic_safe(row['name'], ascii_names=False)}"
    payload = json.loads(publications.value(topic))
    assert payload["name"] == row["name"]
    assert payload["address"] == row["address"]
    assert payload["rssi"] == -42
    for key, value in row["expected"].items():
        assert payload[key] == value
        assert json.loads(publications.value(f"{topic}/{key}")) == value
    assert json.loads(publications.value(f"van/victron/{topic_safe(payload['model_name'], ascii_names=False)}")) == payload


def test_multiple_victron_devices_are_independent_and_unknown_devices_ignored(publications):
    rows = frames("victron.json")
    config = Config(sensors=(), victron_devices=tuple(VictronDevice(row["address"], TEST_KEY) for row in rows))
    gateway = Gateway(config, publications)
    for row in rows:
        gateway.on_advertisement(*advertisement(row))
    unknown = dict(rows[0], address="11:22:33:44:55:66")
    gateway.on_advertisement(*advertisement(unknown))
    gateway.publish_victron_ready()
    assert len(gateway.victron_raw) == 3
    assert len(gateway.seen) == 3
    for row in rows:
        assert publications.value(f"van/victron/{topic_safe(row['name'], ascii_names=False)}")


@pytest.mark.parametrize("row", frames("temperature.json"), ids=lambda row: row["name"])
def test_temperature_frames_to_compatible_payloads(row, publications):
    _, adv = advertisement(row)
    values = PARSERS[row["protocol"]](adv)
    if "gatt" in row:
        values = parse_inkbird_gatt(bytes.fromhex(row["gatt"]), values)
    sensor = Sensor(row["name"], row["address"], row["protocol"])
    publish_sensor(publications, "van/temperature", sensor, values, -42)
    topic = f"van/temperature/{sensor.identifier}"
    payload = json.loads(publications.value(topic))
    assert payload["protocol"] == row["protocol"]
    assert payload["name"] == row["name"]
    assert payload["address"] == row["address"]
    assert "timestamp" in payload
    for key, value in row["expected"].items():
        assert payload[key] == value
        assert json.loads(publications.value(f"{topic}/{key}")) == value
    assert publications.value(f"{topic}/availability") == "online"


def test_inkbird_gatt_corrects_bluez_accumulated_temperature():
    adv = SimpleNamespace(manufacturer_data={770: bytes.fromhex("00000000001800"), 790: bytes.fromhex("00000000001800")})
    provisional = parse_inkbird(adv)
    assert provisional["temperature"] == 7.9
    current = parse_inkbird_gatt(struct.pack("<hH", 770, 0), provisional)
    assert current["temperature"] == 7.7
    assert current["battery"] == 24
    assert "humidity" not in current
    assert provisional["temperature"] == 7.9


@pytest.mark.parametrize("data", [b"", b"abc", struct.pack("<hH",2000,10001)])
def test_invalid_gatt_readings_rejected(data):
    assert parse_inkbird_gatt(data, {}) is None


def test_ruuvi_format3_negative_temperature():
    adv = SimpleNamespace(manufacturer_data={0x499: bytes([3,100,0x85,50]) + struct.pack(">HhhhH",51325,1,2,3,3000)})
    values = PARSERS["ruuvi"](adv)
    assert values["temperature"] == -5.5
    assert values["humidity"] == 50


@pytest.mark.parametrize("raw", [b"", b"x", b"\x10\x02", bytes.fromhex("100200007f01000000")])
def test_truncated_or_unsupported_victron(raw):
    assert decode_victron(raw, TEST_KEY) is None


def test_wrong_key_rejected():
    with pytest.raises(Exception):
        decode_victron(bytes.fromhex(frames("victron.json")[0]["data"]), "11" * 16)


def test_custom_names_preserve_topics_without_advertised_name(publications):
    row = frames("victron.json")[0]
    config = Config(sensors=(), victron_devices=(VictronDevice(row["address"], TEST_KEY),),
                    victron_names={row["address"]: row["name"]})
    gateway = Gateway(config, publications)
    device, adv = advertisement(row)
    device.name = adv.local_name = None
    gateway.on_advertisement(device, adv)
    gateway.publish_victron_ready()
    assert publications.value("van/victron/smartsolar_pyleas")


def test_config_is_validated_without_exposing_secrets():
    secret = "must-not-appear-in-errors"
    for raw in [f"@{secret}", f"AA:BB:CC:DD:EE:FF@{secret}"]:
        with pytest.raises(ValueError) as error:
            parse_victron_devices(raw)
        assert secret not in str(error.value)
    config = Config.from_env({"VICTRON_DEVICES": f"AA:BB:CC:DD:EE:FF@{TEST_KEY}", "MQTT_PASSWORD":secret})
    assert secret not in repr(config)
    assert TEST_KEY not in repr(config.victron_devices)
    for setting in ("GATT_ATTEMPTS", "SCAN_TIMEOUT_SECONDS", "MQTT_PORT"):
        with pytest.raises(ValueError):
            Config.from_env({setting:"0"})
    with pytest.raises(ValueError):
        Config.from_env({"SCAN_TIMEOUT_SECONDS":"nan"})
    assert Config.from_env({"TEMPERATURE_SENSORS":"[]"}).sensors == ()


def test_sensor_configuration_retains_identity_and_rejects_collisions():
    sensors = parse_sensors('[{"name":"Ça pique","address":"e3:ee:e4:14:fa:b0","protocol":"ruuvi"}]')
    assert sensors[0].identifier == "ca_pique"
    assert sensors[0].address == "E3:EE:E4:14:FA:B0"
    with pytest.raises(ValueError):
        parse_sensors('[{"name":"DHT22","address":"E3:EE:E4:14:FA:B0","protocol":"ruuvi"}]')


def test_multiple_devices_use_their_own_distinct_keys(publications):
    from Crypto.Cipher import AES
    from Crypto.Util import Counter
    rows = frames("victron.json")
    devices = []
    for index, row in enumerate(rows, 1):
        raw = bytes.fromhex(row["data"])
        iv = int.from_bytes(raw[5:7], "little")
        def cipher(key):
            return AES.new(key, AES.MODE_CTR, counter=Counter.new(128, initial_value=iv, little_endian=True))
        plaintext = cipher(bytes(16)).decrypt(raw[8:])
        synthetic_key = bytes([index]) * 16
        row["data"] = (raw[:7] + synthetic_key[:1] + cipher(synthetic_key).encrypt(plaintext)).hex()
        devices.append(VictronDevice(row["address"], synthetic_key.hex()))
    gateway = Gateway(Config(sensors=(), victron_devices=tuple(devices)), publications)
    for row in rows:
        gateway.on_advertisement(*advertisement(row))
    gateway.publish_victron_ready()
    for row in rows:
        payload = json.loads(publications.value(f"van/victron/{topic_safe(row['name'], ascii_names=False)}"))
        for key, value in row["expected"].items():
            assert payload[key] == value

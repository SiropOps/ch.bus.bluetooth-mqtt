import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bluetooth_gateway.config import Config, Sensor, VictronDevice
from bluetooth_gateway.service import BluetoothCleanupError, Gateway, read_inkbird_gatt
from tests.conftest import advertisement, frames


class Radio:
    def __init__(self, start_failures=0, stop_failure=False, gatt_failure=None):
        self.start_failures = start_failures
        self.stop_failure = stop_failure
        self.gatt_failure = gatt_failure
        self.scanners = []
        self.clients = []
        self.active = 0
        self.connections = 0
        self.events = []

    def scanner(self, **kwargs):
        radio = self
        class Scanner:
            running = False
            async def start(self):
                assert radio.active == 0 and radio.connections == 0
                self.running = True
                radio.active += 1
                radio.events.append("start")
                if radio.start_failures:
                    radio.start_failures -= 1
                    raise RuntimeError("org.bluez.Error.InProgress")
            async def stop(self):
                radio.events.append("stop")
                if radio.stop_failure:
                    raise RuntimeError("DBus unavailable")
                if self.running:
                    radio.active -= 1
                    self.running = False
        scanner = Scanner()
        scanner.callback = kwargs["detection_callback"]
        self.scanners.append(scanner)
        return scanner

    def client(self, device, timeout):
        # An address string would make Bleak create a hidden discovery scanner.
        assert not isinstance(device, str)
        radio = self
        class Client:
            connected = False
            disconnected = False
            async def connect(self):
                assert radio.active == 0 and radio.connections == 0
                self.connected = True
                radio.connections += 1
                radio.events.append("connect")
                if radio.gatt_failure == "connect_timeout":
                    await asyncio.Future()
                if radio.gatt_failure == "connect_error":
                    raise RuntimeError("connection failed")
            async def read_gatt_char(self, uuid):
                assert uuid == "0000fff2-0000-1000-8000-00805f9b34fb"
                radio.events.append("read")
                if radio.gatt_failure == "read_timeout":
                    await asyncio.Future()
                if radio.gatt_failure == "read_error":
                    raise RuntimeError("DBus error")
                return bytes.fromhex("02030000")
            async def disconnect(self):
                radio.events.append("disconnect")
                if radio.gatt_failure == "disconnect_error":
                    raise RuntimeError("DBus error")
                if self.connected:
                    radio.connections -= 1
                    self.connected = False
                self.disconnected = True
        client = Client()
        self.clients.append(client)
        return client


def config(**kwargs):
    return replace(Config(), sensors=(), initial_scan=30, tick=0.001,
                   retry_max=0.002, gatt_timeout=0.01, gatt_read_timeout=0.01,
                   cleanup_timeout=0.02, gatt_attempts=1, **kwargs)


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


async def cancel(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_inprogress_start_failure_cleans_before_replacement(publications):
    radio = Radio(start_failures=2)
    gateway = Gateway(config(), publications, radio.scanner, radio.client)
    task = asyncio.create_task(gateway.run())
    await until(lambda: gateway.scanner_state == "running")
    assert radio.events[:5] == ["start", "stop", "start", "stop", "start"]
    assert gateway.scanner_restarts == 2
    assert len(radio.scanners) == 3 and radio.active == 1
    await cancel(task)
    assert radio.active == 0
    assert json.loads(publications.value("van/bluetooth/health"))["scanner"] == "stopped"


async def test_silent_scanner_stop_restarts_via_watchdog(publications):
    radio = Radio()
    gateway = Gateway(config(scanner_idle=0.02), publications, radio.scanner, radio.client)
    task = asyncio.create_task(gateway.run())
    await until(lambda: radio.active == 1)
    radio.scanners[0].running = False
    radio.active = 0
    await until(lambda: len(radio.scanners) == 2 and gateway.scanner_state == "running")
    assert gateway.scanner_restarts == 1
    await cancel(task)
    assert radio.active == 0


async def test_scanner_constructor_adapter_failure_recovers(publications):
    radio = Radio()
    calls = 0
    def factory(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("adapter unavailable")
        return radio.scanner(**kwargs)
    gateway = Gateway(config(), publications, factory, radio.client)
    task = asyncio.create_task(gateway.run())
    await until(lambda: gateway.scanner_state == "running")
    assert calls == 2 and radio.active == 1
    await cancel(task)


async def test_uncertain_discovery_cleanup_never_creates_replacement(publications):
    radio = Radio(start_failures=1, stop_failure=True)
    gateway = Gateway(config(), publications, radio.scanner, radio.client)
    with pytest.raises(BluetoothCleanupError):
        await gateway.run()
    assert len(radio.scanners) == 1
    assert gateway.scanner_state == "cleanup_failed"


@pytest.mark.parametrize("failure", ["connect_timeout", "connect_error", "read_timeout", "read_error"])
async def test_gatt_disconnects_on_all_failures(failure):
    radio = Radio(gatt_failure=failure)
    with pytest.raises((TimeoutError, RuntimeError)):
        await read_inkbird_gatt(SimpleNamespace(address="AA:BB:CC:DD:EE:FF"), {"battery":24}, config(), radio.client)
    assert radio.clients[0].disconnected
    assert radio.connections == 0


async def test_cancellation_disconnects_and_leaves_no_tasks():
    radio = Radio(gatt_failure="read_timeout")
    before = set(asyncio.all_tasks())
    task = asyncio.create_task(read_inkbird_gatt(SimpleNamespace(address="AA:BB:CC:DD:EE:FF"), {}, config(), radio.client))
    await until(lambda: "read" in radio.events)
    await cancel(task)
    assert radio.connections == 0 and radio.clients[0].disconnected
    assert set(asyncio.all_tasks()) == before


async def test_uncertain_gatt_cleanup_is_fatal():
    radio = Radio(gatt_failure="disconnect_error")
    with pytest.raises(BluetoothCleanupError):
        await read_inkbird_gatt(SimpleNamespace(address="AA:BB:CC:DD:EE:FF"), {}, config(), radio.client)


async def test_temperature_cycle_pauses_scanner_and_isolates_inkbird_failure(publications):
    radio = Radio(gatt_failure="read_error")
    rows = frames("temperature.json")[:3]
    sensors = tuple(Sensor(row["name"],row["address"],row["protocol"]) for row in rows)
    settings = replace(config(), sensors=sensors, missed_cycles=1)
    gateway = Gateway(settings, publications, radio.scanner, radio.client)
    await gateway.start_scanner()
    for row in rows:
        gateway.on_advertisement(*advertisement(row))
    await gateway.temperature_cycle()
    assert radio.events == ["start", "stop", "connect", "read", "disconnect"]
    assert radio.active == radio.connections == 0
    assert publications.value("van/temperature/ca_pique/availability") == "online"
    assert publications.value("van/temperature/fruit_storage/availability") == "offline"
    await gateway.start_scanner()
    assert radio.active == 1
    await gateway.stop_scanner()


async def test_gatt_success_uses_authoritative_value_and_resumes_in_owner_loop(publications):
    radio = Radio()
    row = frames("temperature.json")[2]
    sensor = Sensor(row["name"],row["address"],row["protocol"])
    gateway = Gateway(replace(config(), sensors=(sensor,), initial_scan=0.02), publications, radio.scanner, radio.client)
    task = asyncio.create_task(gateway.run())
    await until(lambda: radio.active == 1)
    gateway.on_advertisement(*advertisement(row))
    await until(lambda: len(radio.scanners) == 2 and gateway.scanner_state == "running")
    assert json.loads(publications.value("van/temperature/fruit_storage"))["temperature"] == 7.7
    assert radio.connections == 0
    await cancel(task)


async def test_missing_sensor_only_marks_offline_after_threshold(publications):
    row = frames("temperature.json")[0]
    sensor = Sensor(row["name"], row["address"], row["protocol"])
    gateway = Gateway(replace(config(), sensors=(sensor,), missed_cycles=2), publications)
    await gateway.temperature_cycle()
    assert not any(topic.endswith("availability") for topic, _ in publications.messages)
    await gateway.temperature_cycle()
    assert publications.value("van/temperature/ca_pique/availability") == "offline"
    assert json.loads(publications.value("van/temperature/scan"))["found"] == 0


def test_secrets_do_not_leak_from_decoder_errors_or_health(publications, monkeypatch, caplog):
    row = frames("victron.json")[0]
    secret = "01" * 16
    password = "test-private-password"
    settings = replace(config(), victron_devices=(VictronDevice(row["address"], secret),), mqtt_password=password)
    monkeypatch.setattr("bluetooth_gateway.service.decode_victron", Mock(side_effect=ValueError(secret + password)))
    gateway = Gateway(settings, publications)
    gateway.on_advertisement(*advertisement(row))
    gateway.publish_health()
    assert "Victron decoding failed" in caplog.text
    assert secret not in caplog.text and password not in caplog.text
    assert secret not in publications.value("van/bluetooth/health")
    assert password not in publications.value("van/bluetooth/health")


async def test_sigterm_cancels_owner_and_waits_for_cleanup(monkeypatch, publications):
    from bluetooth_gateway import __main__ as entry
    import signal
    callbacks = {}
    def register(signum, handler):
        previous = callbacks.get(signum)
        callbacks[signum] = handler
        return previous
    radio = Radio()
    gateway = Gateway(config(), publications, radio.scanner, radio.client)
    monkeypatch.setattr(entry.signal, "signal", register)
    monkeypatch.setattr(entry, "Gateway", lambda *args: gateway)
    task = asyncio.create_task(entry.run_service(config(), publications))
    await until(lambda: gateway.scanner_state == "running")
    callbacks[signal.SIGTERM](signal.SIGTERM, None)
    await task
    assert radio.active == 0 and gateway.scanner is None


async def test_start_timeout_never_assumes_noop_stop_released_discovery(publications):
    radio = Radio()
    def factory(**kwargs):
        scanner = radio.scanner(**kwargs)
        async def start_without_reply():
            radio.active += 1
            await asyncio.Future()
        scanner.start = start_without_reply
        return scanner
    gateway = Gateway(config(scanner_timeout=0.01), publications, factory, radio.client)
    with pytest.raises(BluetoothCleanupError):
        await gateway.run()
    assert len(radio.scanners) == 1
    assert gateway.scanner_state == "cleanup_failed"
    assert "stop" not in radio.events

"""Contract simulation of two BlueZ discovery sessions, not a radio emulator."""
import asyncio
import importlib.util
from pathlib import Path
import sys
import json
import signal
from dataclasses import replace
from types import SimpleNamespace

import pytest

from bluetooth_gateway.service import Gateway
from tests.test_lifecycle import config
from tests.test_lifecycle import until, cancel
from tests.conftest import advertisement, frames
from bluetooth_gateway.config import Sensor, VictronDevice


@pytest.fixture
def gps_module():
    directory = Path(__file__).resolve().parents[2] / "ch.bus.burry-gps" / "low-level-esp32"
    if not directory.exists():
        pytest.skip("Place ch.bus.burry-gps beside this repository for coexistence simulation")
    spec = importlib.util.spec_from_file_location("gps_coexistence", directory / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, str(directory))
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path.remove(str(directory))
        sys.modules.pop(spec.name, None)


async def test_one_application_releasing_discovery_keeps_the_other_session(publications, gps_module):
    sessions = set()
    callbacks = {}
    class Scanner:
        def __init__(self, **kwargs):
            assert kwargs["bluez"]["adapter"] == "hci0"
            callbacks[self] = kwargs["detection_callback"]
        async def start(self):
            sessions.add(self)
        async def stop(self):
            sessions.discard(self)
    class Publisher:
        def health(self):
            return {}
        def publish(self, payload):
            pass
    gateway = Gateway(config(), publications, Scanner)
    gps = gps_module.GPSService(gps_module.Settings(scan_seconds=10), Publisher(), Scanner)
    await gateway.start_scanner()
    gps_scan = asyncio.create_task(gps.find_device())
    try:
        async with asyncio.timeout(2):
            while len(sessions) < 2:
                await asyncio.sleep(0)
        # A local watchdog cycle releases only the gateway lease.
        await gateway.stop_scanner()
        assert len(sessions) == 1
        await gateway.start_scanner()
        assert len(sessions) == 2
        device = SimpleNamespace(name="GPS-NanoESP32", address="AA:BB:CC:DD:EE:FF")
        advertisement = SimpleNamespace(local_name=device.name)
        for scanner in list(sessions):
            callbacks[scanner](device, advertisement)
        assert await gps_scan is device
        assert len(sessions) == 1 and gateway.scanner in sessions
        assert gateway.last_advertisement is not None
    finally:
        if not gps_scan.done():
            gps_scan.cancel()
            with pytest.raises(asyncio.CancelledError):
                await gps_scan
        await gateway.stop_scanner()
    assert not sessions


def test_deadline_helpers_match_between_repositories(gps_module):
    gateway = Path(__file__).resolve().parents[1] / "bluetooth_gateway" / "runtime.py"
    gps = Path(gps_module.__file__).with_name("runtime.py")
    assert gateway.read_text() == gps.read_text()


class BlueZSessions:
    """D-Bus ownership contract: each connection owns only its own resources.

    This model deliberately allows parallel discovery/GATT. It cannot establish
    whether a physical adapter has the capacity to do so.
    """
    def __init__(self, gps_module, gps_present=True, persistent=False):
        self.gps_module = gps_module
        self.gps_present = gps_present
        self.persistent = persistent
        self.sessions = {}
        self.connections = set()
        self.events = []
        self.gateway = None
        self.observations = 0
        self.gps_cycles = 0
        self.inkbird_reading = asyncio.Event()
        self.gps_connected = asyncio.Event()
        self.overlap = False
        self.device = SimpleNamespace(name="GPS-NanoESP32", address="AA:BB:CC:DD:EE:FF")

    def advertise(self):
        rows = frames("victron.json")
        # Distinct valid encrypted frames make each GPS cycle observable in MQTT.
        victron = dict(rows[self.observations % len(rows)], address=rows[0]["address"], name=rows[0]["name"])
        for callback in list(self.sessions.values()):
            callback(*advertisement(victron))
            callback(*advertisement(frames("temperature.json")[0]))
        self.observations += 1

    def scanner(self, owner):
        bus = self
        class Scanner:
            def __init__(self, **kwargs):
                assert kwargs["bluez"]["adapter"] == "hci0"
                self.callback = kwargs["detection_callback"]
            async def start(self):
                bus.events.append((owner, "StartDiscovery"))
                bus.sessions[self] = self.callback
                bus.advertise()
                if owner == "gps" and bus.gps_present:
                    self.callback(bus.device, SimpleNamespace(local_name=bus.device.name))
            async def stop(self):
                bus.events.append((owner, "StopDiscovery"))
                bus.sessions.pop(self, None)
                bus.advertise()
        return Scanner

    def client(self, owner):
        bus = self
        class Client:
            is_connected = False
            def __init__(self, device, timeout):
                assert not isinstance(device, str)
            async def connect(self):
                bus.events.append((owner, "Connect"))
                bus.connections.add(self)
                self.is_connected = True
                if owner == "gps":
                    bus.gps_connected.set()
                await asyncio.sleep(0)
            async def start_notify(self, characteristic, callback):
                assert owner == "gps"
                bus.gps_cycles += 1
                callback(None, b'{"lat":46.2,"speed":36}')
                bus.advertise()
                if not bus.persistent:
                    self.is_connected = False
            async def read_gatt_char(self, characteristic):
                assert owner == "gateway"
                bus.inkbird_reading.set()
                await bus.gps_connected.wait()
                bus.overlap = len(bus.connections) == 2
                return bytes.fromhex("02030000")
            async def disconnect(self):
                bus.events.append((owner, "Disconnect"))
                bus.connections.discard(self)
                self.is_connected = False
                bus.advertise()
        return Client


class GPSPublisher:
    def __init__(self):
        self.messages = []
    def publish(self, payload):
        self.messages.append(payload)
    def health(self):
        return {}


def shared_gateway(bus, publications, **kwargs):
    row = frames("temperature.json")[0]
    victron = frames("victron.json")[0]
    settings = config(**kwargs)
    settings = replace(settings, sensors=(Sensor(row["name"], row["address"], "ruuvi"),),
                       victron_devices=(VictronDevice(victron["address"], "00" * 16),),
                       victron_interval=0, temperature_interval=0.001, initial_scan=0)
    bus.gateway = Gateway(settings, publications, bus.scanner("gateway"), bus.client("gateway"))
    return bus.gateway


async def test_repeated_gps_reconnections_preserve_victron_and_ruuvi(publications, gps_module):
    bus = BlueZSessions(gps_module)
    gateway = shared_gateway(bus, publications)
    publisher = GPSPublisher()
    gps = gps_module.GPSService(gps_module.Settings(reconnect_delay=0.001, reconnect_max=0.001),
                                publisher, bus.scanner("gps"), bus.client("gps"))
    before = set(asyncio.all_tasks())
    gateway_task = asyncio.create_task(gateway.run())
    gps_task = asyncio.create_task(gps.run())
    try:
        await until(lambda: bus.gps_cycles >= 30)
        assert not gps_task.done() and not gateway_task.done()
        assert gateway.scanner_restarts == 0
        assert bus.events.count(("gateway", "StartDiscovery")) == 1
        assert bus.events.count(("gateway", "StopDiscovery")) == 0
        assert len(publisher.messages) >= 30
        assert sum(topic == "van/victron/smartsolar_pyleas" for topic, _ in publications.messages) > 10
        assert sum(topic == "van/temperature/ca_pique" for topic, _ in publications.messages) > 10
    finally:
        await cancel(gps_task)
        await cancel(gateway_task)
    assert not bus.sessions and not bus.connections
    assert set(asyncio.all_tasks()) == before


async def test_gps_connection_overlaps_inkbird_read_without_shared_lock(publications, gps_module):
    bus = BlueZSessions(gps_module, persistent=True)
    gateway = shared_gateway(bus, publications)
    gateway.config = replace(gateway.config, gatt_read_timeout=2)
    gps = gps_module.GPSService(gps_module.Settings(), GPSPublisher(),
                                bus.scanner("gps"), bus.client("gps"))
    await gateway.start_scanner()
    inkbird = frames("temperature.json")[2]
    sensor = Sensor(inkbird["name"], inkbird["address"], "inkbird")
    gateway.sensors[sensor.address] = sensor
    gateway.missed[sensor.address] = 0
    gateway.on_advertisement(*advertisement(inkbird))
    temperature = asyncio.create_task(gateway.temperature_cycle())
    gps_task = None
    try:
        await until(bus.inkbird_reading.is_set)
        gps_task = asyncio.create_task(gps.session(bus.device))
        await asyncio.wait_for(temperature, timeout=2)
        await until(lambda: gps.connection_state == "connected")
        assert bus.overlap
        assert json.loads(publications.value("van/temperature/fruit_storage"))["temperature"] == 7.7
        assert gps.connection_state == "connected"
        assert len(bus.connections) == 1
        await gateway.start_scanner()
        bus.advertise()
        assert gateway.scanner in bus.sessions
    finally:
        if gps_task is not None:
            await cancel(gps_task)
        if not temperature.done():
            await cancel(temperature)
        await gateway.stop_scanner()
    assert not bus.connections and not bus.sessions


async def test_gps_sigterm_and_restart_leave_gateway_session_intact(publications, gps_module, monkeypatch):
    bus = BlueZSessions(gps_module, persistent=True)
    gateway = shared_gateway(bus, publications)
    gateway_task = asyncio.create_task(gateway.run())
    callbacks = {}
    def register(signum, callback):
        previous = callbacks.get(signum)
        callbacks[signum] = callback
        return previous
    monkeypatch.setattr(gps_module.signal, "signal", register)
    settings = gps_module.Settings(startup_delay=0)
    gps_task = None
    try:
        await until(lambda: gateway.scanner_state == "running")
        scanner = gateway.scanner
        for cycle in range(3):
            gps = gps_module.GPSService(settings, GPSPublisher(), bus.scanner("gps"), bus.client("gps"))
            monkeypatch.setattr(gps_module, "GPSService", lambda *args: gps)
            gps_task = asyncio.create_task(gps_module.run_service(settings, gps.publisher))
            await until(lambda: gps.connection_state == "connected")
            callbacks[signal.SIGTERM](signal.SIGTERM, None)
            await asyncio.wait_for(gps_task, timeout=2)
            bus.advertise()
            await asyncio.sleep(0.005)
            assert gps.connection_state == "disconnected"
            assert gateway.scanner is scanner and scanner in bus.sessions
            assert not gateway_task.done() and not bus.connections
            assert bus.events.count(("gateway", "StartDiscovery")) == 1
            assert bus.events.count(("gateway", "StopDiscovery")) == 0
    finally:
        if gps_task is not None and not gps_task.done():
            callbacks[signal.SIGTERM](signal.SIGTERM, None)
            await gps_task
        await cancel(gateway_task)
    assert not bus.sessions and not bus.connections


async def test_eight_hour_absence_uses_real_scanner_lifecycle_without_gateway_restart(publications, gps_module):
    bus = BlueZSessions(gps_module, gps_present=False)
    gateway = shared_gateway(bus, publications)
    now = 0
    async def sleep(seconds):
        nonlocal now
        now += seconds
        bus.advertise()
        if now >= 8 * 3600:
            raise asyncio.CancelledError
        await asyncio.sleep(0)
    gps = gps_module.GPSService(gps_module.Settings(scan_seconds=0.00001), GPSPublisher(),
                                bus.scanner("gps"), bus.client("gps"),
                                monotonic=lambda: now, sleep=sleep)
    gateway_task = asyncio.create_task(gateway.run())
    try:
        await until(lambda: gateway.scanner_state == "running")
        with pytest.raises(asyncio.CancelledError):
            await gps.run()
        assert now >= 28800
        starts = bus.events.count(("gps", "StartDiscovery"))
        assert 30 < starts < 40
        assert bus.events.count(("gps", "StopDiscovery")) == starts
        assert gps.failures == gps.recovery_level == 0
        assert gateway.scanner_restarts == 0 and gateway.scanner in bus.sessions
        assert not bus.connections
    finally:
        await cancel(gateway_task)
    assert not bus.sessions

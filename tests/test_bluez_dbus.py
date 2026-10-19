"""Linux Bleak backend with an in-memory D-Bus peer, never a real adapter."""
import asyncio
import sys

import pytest

from bluetooth_gateway.runtime import BluetoothCleanupError, bounded
from bluetooth_gateway.service import Gateway
from tests.test_lifecycle import config
from tests.test_coexistence import gps_module


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux BlueZ backend / dbus-fast")
ADAPTER = "/org/bluez/hci0"


@pytest.fixture
def backend(monkeypatch):
    from dbus_fast import Message, MessageType
    from bleak.backends.bluezdbus import scanner as scanner_module
    from bleak.backends.bluezdbus.manager import BlueZManager
    from bleak import BleakScanner

    class Bus:
        def __init__(self):
            self.calls = []
            self.stall = None
            self.error = None
            self.leased = False
        async def call(self, message):
            self.calls.append(message.member)
            if message.member == "StartDiscovery":
                self.leased = True
            if message.member == self.stall:
                await asyncio.Future()
            if message.member == self.error:
                return Message(message_type=MessageType.ERROR, reply_serial=1,
                               error_name="org.bluez.Error.InProgress", signature="s", body=["simulated"])
            if message.member == "StopDiscovery":
                self.leased = False
            return Message(message_type=MessageType.METHOD_RETURN, reply_serial=1)

    manager = BlueZManager()
    manager._properties[ADAPTER] = {}
    bus = Bus()
    manager._bus = bus
    async def get_manager():
        return manager
    monkeypatch.setattr(scanner_module, "get_global_bluez_manager", get_manager)
    def scanner(**kwargs):
        return BleakScanner(backend=scanner_module.BleakScannerBlueZDBus, **kwargs)
    return manager, bus, scanner


@pytest.mark.parametrize("failure", [None, "StartDiscovery", "StopDiscovery"])
async def test_real_backend_removes_callbacks_and_unlocks_bus(backend, failure):
    manager, bus, scanner = backend
    bus.error = failure
    instance = scanner(detection_callback=lambda device, adv: None, bluez={"adapter": "hci0"})
    if failure == "StartDiscovery":
        with pytest.raises(Exception):
            await bounded(instance.start(), 0.1)
    else:
        await bounded(instance.start(), 0.1)
        if failure:
            with pytest.raises(Exception):
                await bounded(instance.stop(), 0.1)
        else:
            await bounded(instance.stop(), 0.1)
            assert not bus.leased
    assert not manager._advertisement_callbacks[ADAPTER]
    assert not manager._device_removed_callbacks
    assert not manager._bus_lock.locked()


@pytest.mark.parametrize("stall", ["StartDiscovery", "StopDiscovery"])
@pytest.mark.parametrize("owner", ["gateway", "gps"])
async def test_actual_backend_timeout_never_replaces_uncertain_session(backend, publications, stall, owner, request):
    manager, bus, scanner = backend
    bus.stall = stall
    gateway = Gateway(config(scanner_timeout=0.01), publications, scanner)
    if owner == "gps":
        gps_module = request.getfixturevalue("gps_module")
        publisher = type("Publisher", (), {"health": lambda self: {}})()
        gateway = gps_module.GPSService(gps_module.Settings(operation_timeout=0.01, cleanup_timeout=0.01,
                                                          scan_seconds=0.00001), publisher, scanner)
        with pytest.raises(gps_module.BluetoothCleanupError):
            await gateway.run()
    elif stall == "StartDiscovery":
        with pytest.raises(BluetoothCleanupError):
            await gateway.run()
    else:
        await gateway.start_scanner()
        with pytest.raises(BluetoothCleanupError):
            await gateway.stop_scanner()
    assert gateway.scanner_state == "cleanup_failed"
    assert bus.calls.count("StartDiscovery") == 1
    assert not manager._advertisement_callbacks[ADAPTER]
    assert not manager._device_removed_callbacks
    assert not manager._bus_lock.locked()
    # The peer accepted the request but withheld its reply. The lease is uncertain;
    # only process/socket exit can settle it, so no replacement is safe here.
    assert bus.leased


@pytest.mark.parametrize("stage", ["bus", "gatt"])
@pytest.mark.parametrize("failure", ["error", "timeout", "cancel"])
async def test_real_gps_client_releases_dbus_after_partial_connect(backend, gps_module, monkeypatch, stage, failure):
    from dbus_fast import Message, MessageType
    from bleak import BleakClient
    from bleak.backends.device import BLEDevice
    from bleak.backends.bluezdbus import client as client_module

    manager, _, _ = backend
    path = ADAPTER + "/dev_AA_BB_CC_DD_EE_FF"
    manager._properties[path] = {"org.bluez.Device1": {"Connected": False}}
    entered = asyncio.Event()

    async def fail():
        entered.set()
        if failure == "error":
            raise RuntimeError("simulated D-Bus failure")
        await asyncio.Future()

    class Bus:
        closes = 0
        calls = []
        async def connect(self):
            if stage == "bus":
                await fail()
            return self
        def disconnect(self):
            self.closes += 1
        async def wait_for_disconnect(self):
            pass
        async def call(self, message):
            self.calls.append(message.member)
            if message.member == "Connect":
                await fail()
            return Message(message_type=MessageType.METHOD_RETURN, reply_serial=1)

    bus = Bus()
    async def get_manager():
        return manager
    monkeypatch.setattr(client_module, "MessageBus", lambda **kwargs: bus)
    monkeypatch.setattr(client_module, "get_global_bluez_manager", get_manager)
    monkeypatch.setattr(client_module.BlueZFeatures, "checked_bluez_version", True)
    monkeypatch.setattr(client_module.BlueZFeatures, "supported_version", True)
    device = BLEDevice("AA:BB:CC:DD:EE:FF", "GPS-NanoESP32", {"path": path, "props": {}})
    client = BleakClient(device, backend=client_module.BleakClientBlueZDBus)
    service = gps_module.GPSService(gps_module.Settings(connect_timeout=0.02),
                                   object(), client_factory=lambda *args, **kwargs: client)
    before = set(asyncio.all_tasks())
    task = asyncio.create_task(service.session(device))
    if failure == "cancel":
        await entered.wait()
        task.cancel()
        expected = asyncio.CancelledError
    else:
        expected = RuntimeError if failure == "error" else TimeoutError
    with pytest.raises(expected):
        await task
    assert bus.closes >= 1
    assert client._backend._bus is None
    assert not manager._device_watchers.get(path)
    assert service.connection_state == "disconnected"
    if stage == "gatt":
        assert bus.calls == ["Connect", "Disconnect"]
    assert set(asyncio.all_tasks()) == before

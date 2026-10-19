"""Contract simulation of two BlueZ discovery sessions, not a radio emulator."""
import asyncio
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from bluetooth_gateway.service import Gateway
from tests.test_lifecycle import config


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

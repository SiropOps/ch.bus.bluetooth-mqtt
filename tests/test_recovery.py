import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bluetooth_gateway.config import Config
from bluetooth_gateway.runtime import BluetoothCleanupError, bounded
from bluetooth_gateway.service import Gateway
from tests.test_lifecycle import Radio, config, until, cancel


class Clock:
    def __init__(self):
        self.now = 0
        self.delays = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.delays.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


async def test_silence_for_hours_backs_off_without_process_escalation(publications):
    clock = Clock()
    radio = Radio()
    gateway = Gateway(replace(config(), scanner_idle=120, silence_retry_max=1800, tick=30,
                              temperature_interval=300, health_interval=300),
                      publications, radio.scanner, radio.client, clock, clock.sleep)
    task = asyncio.create_task(gateway.run())
    await until(lambda: clock.now >= 8 * 3600)
    assert not task.done()
    # Fewer than 25 attempts in 8 hours; no two-minute endless restart loop.
    assert 10 < gateway.scanner_restarts < 25
    assert gateway.silence_interval == 1800
    assert gateway.consecutive_failures == 0
    assert gateway.recovery_pending
    gateway.on_advertisement(SimpleNamespace(address="00:00:00:00:00:00"), SimpleNamespace())
    assert not gateway.recovery_pending
    assert gateway.silence_interval == 120
    assert gateway.silence_recoveries == 0
    await cancel(task)
    assert radio.active == 0


async def test_start_failures_escalate_after_limit_and_backoff(publications):
    clock = Clock()
    attempts = []
    class Scanner:
        async def start(self):
            attempts.append(clock.now)
            raise RuntimeError("DBus failure")

        async def stop(self):
            pass

    gateway = Gateway(replace(config(), retry_max=60, tick=1, failure_limit=4), publications,
                      lambda **kwargs: Scanner(), monotonic=clock, sleep=clock.sleep)
    with pytest.raises(BluetoothCleanupError):
        await gateway.run()
    assert attempts == [0, 1, 3, 7]
    assert gateway.recovery_level == 2
    assert gateway.consecutive_failures == 4


async def test_stop_timeout_never_creates_another_scanner(publications):
    radio = Radio()
    gateway = Gateway(config(), publications, radio.scanner, radio.client)
    await gateway.start_scanner()
    async def stuck():
        await asyncio.Future()
    gateway.scanner.stop = stuck
    with pytest.raises(BluetoothCleanupError):
        await gateway.stop_scanner()
    assert gateway.scanner_state == "cleanup_failed"
    assert gateway.recovery_level == 2
    with pytest.raises(RuntimeError):
        await gateway.start_scanner()
    assert len(radio.scanners) == 1


async def test_bounded_call_drains_cancelled_task():
    before = set(asyncio.all_tasks())
    with pytest.raises(TimeoutError):
        await bounded(asyncio.Future(), 0.001)
    assert set(asyncio.all_tasks()) == before


async def test_cancellation_resistant_operation_forces_delayed_exit(monkeypatch):
    import bluetooth_gateway.runtime as runtime
    release = asyncio.Event()
    calls = []
    async def stubborn():
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            await release.wait()
    task = asyncio.create_task(stubborn())
    monkeypatch.setattr(runtime.os, "_exit", lambda code: calls.append(code))
    delays = []
    real_sleep = asyncio.sleep
    async def sleep(seconds):
        delays.append(seconds)
        await real_sleep(0)
    monkeypatch.setattr(runtime.asyncio, "sleep", sleep)
    try:
        with pytest.raises(BluetoothCleanupError):
            await bounded(task, 0.001, grace=0.001, restart_delay=60)
        assert calls == [1] and delays == [60]
    finally:
        release.set()
        await task


async def test_process_supervisor_delays_failure_and_sigterm_interrupts_cooldown(monkeypatch, publications):
    from bluetooth_gateway import __main__ as entry
    import signal
    callbacks = {}
    def register(signum, handler):
        previous = callbacks.get(signum)
        callbacks[signum] = handler
        return previous
    monkeypatch.setattr(entry.signal, "signal", register)
    failing = SimpleNamespace(run=AsyncMock(side_effect=BluetoothCleanupError()))
    monkeypatch.setattr(entry, "Gateway", lambda *args: failing)
    task = asyncio.create_task(entry.run_service(config(process_restart_delay=60), publications))
    await until(lambda: failing.run.await_count == 1)
    await asyncio.sleep(0.01)
    assert not task.done()
    callbacks[signal.SIGTERM](signal.SIGTERM, None)
    await task


def test_health_ages_are_not_reset_by_scanner_restart(publications):
    clock = Clock()
    gateway = Gateway(config(), publications, monotonic=clock)
    gateway.on_advertisement(SimpleNamespace(address="00:00:00:00:00:00"), SimpleNamespace())
    clock.now = 240
    gateway.last_activity = clock.now  # A start can reset the watchdog, not the observation.
    gateway.publish_health()
    health = json.loads(publications.value("van/bluetooth/health"))
    assert health["advertisement_age_seconds"] == 240
    assert health["last_gatt_success"] is None


@pytest.mark.parametrize("env", [{"BLE_FAILURE_LIMIT": "1.5"}, {"BLE_FAILURE_LIMIT": "0"},
                                  {"SCANNER_SILENCE_RETRY_MAX_SECONDS": "nan"},
                                  {"BLE_PROCESS_RESTART_DELAY_SECONDS": "-1"}])
def test_invalid_recovery_configuration_rejected(env):
    with pytest.raises(ValueError):
        Config.from_env(env)

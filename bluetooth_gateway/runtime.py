"""Small process-local BLE safeguards; keep the GPS copy in sync."""
import asyncio
import logging
import os

logger = logging.getLogger(__name__)


class BluetoothCleanupError(RuntimeError):
    """Do not acquire another BLE resource in this process."""


async def bounded(operation, timeout, *, grace=2, restart_delay=60):
    """Bound even cancellation-resistant DBus calls, with process exit as last resort.

    wait_for alone can wait forever for a coroutine that ignores cancellation.
    A stuck task must never coexist with a replacement scanner/client.
    """
    task = asyncio.ensure_future(operation)
    cancelled = False
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if done:
            return task.result()
    except asyncio.CancelledError:
        cancelled = True
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=grace)
    if not done:
        logger.critical("BLE task ignored cancellation; recovery_level=2 exit_in_seconds=%s", restart_delay)
        try:
            await asyncio.sleep(restart_delay)
        finally:
            # asyncio.run would otherwise wait forever draining this task.
            os._exit(1)
        raise BluetoothCleanupError("Unresponsive BLE task")  # for injected exit in tests
    # Retrieve failures to avoid unhandled-task warnings. The deadline still failed.
    if not task.cancelled():
        task.exception()
    if cancelled:
        raise asyncio.CancelledError
    raise TimeoutError("BLE operation deadline exceeded")

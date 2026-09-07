import asyncio
import logging
import os
import signal
import sys
from contextlib import contextmanager
from pathlib import Path

from .config import Config
from .publishing import MqttPublisher
from .service import Gateway

logger = logging.getLogger(__name__)


@contextmanager
def owner_lock():
    # Share this directory with the host so two gateway containers cannot run.
    if sys.platform != "linux":
        raise RuntimeError("Bluetooth gateway requires Linux/BlueZ")
    import fcntl
    path = Path(os.environ.get("BLUETOOTH_OWNER_LOCK", "/run/bluetooth-mqtt/owner.lock"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another Bluetooth gateway already owns the adapter") from None
        yield


async def run_service(config, publisher):
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(Gateway(config, publisher).run())
    previous = {}
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        if not stopping:
            stopping = True
            loop.call_soon_threadsafe(task.cancel)

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, stop)
    try:
        await task
    except asyncio.CancelledError:
        if not stopping:
            raise
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = Config.from_env()
        with owner_lock():
            logger.info("Victron devices configured: %s", len(config.victron_devices))
            for device in config.victron_devices:
                logger.info("Victron MAC: %s", device.address)
            publisher = MqttPublisher(config)
            publisher.start()
            try:
                asyncio.run(run_service(config, publisher))
            finally:
                publisher.close()
    except Exception as exc:
        # Configuration and library exceptions must never echo secrets.
        logger.error("Gateway stopped (%s); check configuration or Bluetooth cleanup", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

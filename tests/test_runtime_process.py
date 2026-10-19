"""Exercise the real process boundary without Bluetooth or broker access."""
import subprocess
import sys
import time


def test_uncancellable_operation_exits_instead_of_hanging_asyncio_shutdown():
    program = '''
import asyncio
from bluetooth_gateway.runtime import bounded
async def stubborn():
    while True:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            pass
async def main():
    await bounded(stubborn(), .01, grace=.01, restart_delay=.05)
    print("unexpected replacement allowed")
asyncio.run(main())
'''
    started = time.monotonic()
    result = subprocess.run([sys.executable, "-c", program], capture_output=True,
                            text=True, timeout=5)
    assert result.returncode == 1
    assert time.monotonic() - started >= .05
    assert "ignored cancellation" in result.stderr
    assert "unexpected replacement" not in result.stdout

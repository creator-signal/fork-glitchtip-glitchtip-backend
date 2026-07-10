"""Periodic in-process memory trim for long-running ASGI processes.

Long-running processes accumulate freed-but-unreturned glibc heap pages
(malloc keeps freed chunks binned for reuse and only returns the top of the
heap automatically). Over days of steady ingest this adds tens of MB per
process that the OS counts against the pod. ``gc.collect()`` +
``malloc_trim(0)`` returns those pages.

The scheduled maintenance task already does this, but scheduled tasks run
once cluster-wide per interval — only the pod whose embedded worker picks
the task up gets trimmed. This wrapper runs the same trim inside every ASGI
process on a conservative timer, which costs far less than the worker
recycling (``GRANIAN_WORKERS_MAX_RSS``) it helps avoid.
"""

import asyncio
import ctypes
import gc
import logging
import os

logger = logging.getLogger(__name__)

# One hour between trims: gc.collect() briefly blocks the event loop
# (tens of ms on a busy heap), and freed-page accumulation is slow, so a
# long interval captures nearly all of the benefit at negligible cost.
DEFAULT_TRIM_INTERVAL = 3600


def malloc_trim():
    """Ask glibc to return freed memory to the OS. No-op on other libcs."""
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


class PeriodicMemoryTrim:
    """ASGI wrapper that periodically returns freed memory to the OS.

    The timer task is started lazily on the first ASGI call (of any scope
    type), when an event loop is guaranteed to be running — this works under
    servers with or without lifespan support. Each server worker process
    wraps its own application instance, so each process gets its own timer.

    ``GLITCHTIP_MALLOC_TRIM_INTERVAL`` (seconds) overrides the interval;
    ``0`` disables the timer entirely.
    """

    def __init__(self, app, interval: float | None = None):
        self.app = app
        if interval is None:
            interval = int(
                os.environ.get("GLITCHTIP_MALLOC_TRIM_INTERVAL", DEFAULT_TRIM_INTERVAL)
            )
        self.interval = interval
        self._task: asyncio.Task | None = None

    async def __call__(self, scope, receive, send):
        if self._task is None and self.interval > 0:
            self._task = asyncio.get_running_loop().create_task(self._run())
        await self.app(scope, receive, send)

    async def _run(self):
        while True:
            await asyncio.sleep(self.interval)
            try:
                gc.collect()
                malloc_trim()
            except Exception:
                logger.exception("Periodic memory trim failed")

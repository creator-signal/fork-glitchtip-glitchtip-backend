import asyncio
from unittest import mock

from django.test import SimpleTestCase

from glitchtip.memory_trim import PeriodicMemoryTrim


class MockASGIApp:
    def __init__(self):
        self.calls = 0
        self.scope = None

    async def __call__(self, scope, receive, send):
        self.calls += 1
        self.scope = scope


class PeriodicMemoryTrimTestCase(SimpleTestCase):
    async def test_passthrough(self):
        app = MockASGIApp()
        wrapper = PeriodicMemoryTrim(app, interval=0)
        scope = {"type": "http", "path": "/"}
        await wrapper(scope, None, None)
        self.assertEqual(app.calls, 1)
        self.assertIs(app.scope, scope)

    async def test_disabled_interval_starts_no_timer(self):
        wrapper = PeriodicMemoryTrim(MockASGIApp(), interval=0)
        await wrapper({"type": "http"}, None, None)
        self.assertIsNone(wrapper._task)

    async def test_timer_trims_periodically(self):
        wrapper = PeriodicMemoryTrim(MockASGIApp(), interval=0.01)
        with mock.patch("glitchtip.memory_trim.malloc_trim") as trim:
            await wrapper({"type": "http"}, None, None)
            self.assertIsNotNone(wrapper._task)
            await asyncio.sleep(0.05)
            self.assertGreater(trim.call_count, 0)
        wrapper._task.cancel()

    async def test_timer_started_once(self):
        wrapper = PeriodicMemoryTrim(MockASGIApp(), interval=60)
        await wrapper({"type": "http"}, None, None)
        task = wrapper._task
        await wrapper({"type": "http"}, None, None)
        self.assertIs(wrapper._task, task)
        task.cancel()

    def test_env_interval(self):
        with mock.patch.dict("os.environ", {"GLITCHTIP_MALLOC_TRIM_INTERVAL": "120"}):
            wrapper = PeriodicMemoryTrim(MockASGIApp())
        self.assertEqual(wrapper.interval, 120)

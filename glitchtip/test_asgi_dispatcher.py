import asyncio
from unittest import mock

from django.test import SimpleTestCase, TransactionTestCase

from glitchtip.asgi import (
    MCPDjangoDispatcher,
    RecycleConnectionsMiddleware,
    _recycle_db_connections,
)


class MockASGIApp:
    def __init__(self):
        self.called = False
        self.scope = None

    async def __call__(self, scope, receive, send):
        self.called = True
        self.scope = scope


class LifespanApp:
    """Mock ASGI app that handles lifespan protocol."""

    def __init__(self):
        self.started = False
        self.stopped = False

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    self.started = True
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    self.stopped = True
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        self.scope = scope


class DjangoLikeApp:
    """Mock that raises ValueError on non-http scope (like Django's ASGIHandler)."""

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            raise ValueError("Django can only handle ASGI/HTTP connections")


class MCPDjangoDispatcherTestCase(SimpleTestCase):
    async def test_mcp_routing(self):
        mock_django = MockASGIApp()
        mock_mcp = MockASGIApp()
        dispatcher = MCPDjangoDispatcher(mock_django, mock_mcp, mcp_prefix="/mcp")

        scope = {"type": "http", "path": "/mcp", "method": "POST"}

        await dispatcher(scope, lambda: None, lambda msg: None)

        self.assertTrue(mock_mcp.called)
        self.assertFalse(mock_django.called)

    async def test_django_routing(self):
        mock_django = MockASGIApp()
        mock_mcp = MockASGIApp()
        dispatcher = MCPDjangoDispatcher(mock_django, mock_mcp, mcp_prefix="/mcp")

        scope = {"type": "http", "path": "/api/0/projects/", "method": "GET"}

        await dispatcher(scope, lambda: None, lambda msg: None)

        self.assertTrue(mock_django.called)
        self.assertFalse(mock_mcp.called)

    async def test_lifespan_forwarded_to_both_apps(self):
        """Both django_app and mcp_app receive lifespan when django_lifespan=True."""
        django_app = LifespanApp()
        mcp_app = LifespanApp()
        dispatcher = MCPDjangoDispatcher(
            django_app, mcp_app, mcp_prefix="/mcp", django_lifespan=True
        )

        scope = {"type": "lifespan", "asgi": {"version": "3.0"}}
        messages = asyncio.Queue()
        await messages.put({"type": "lifespan.startup"})
        await messages.put({"type": "lifespan.shutdown"})

        sent = []

        async def send(msg):
            sent.append(msg)

        await dispatcher(scope, messages.get, send)

        self.assertTrue(django_app.started)
        self.assertTrue(django_app.stopped)
        self.assertTrue(mcp_app.started)
        self.assertTrue(mcp_app.stopped)
        self.assertEqual(sent[0]["type"], "lifespan.startup.complete")
        self.assertEqual(sent[1]["type"], "lifespan.shutdown.complete")

    async def test_lifespan_web_only_mode(self):
        """Lifespan only goes to MCP in web-only mode (django_lifespan=False)."""
        django_app = DjangoLikeApp()
        mcp_app = LifespanApp()
        dispatcher = MCPDjangoDispatcher(django_app, mcp_app, mcp_prefix="/mcp")

        scope = {"type": "lifespan", "asgi": {"version": "3.0"}}
        messages = asyncio.Queue()
        await messages.put({"type": "lifespan.startup"})
        await messages.put({"type": "lifespan.shutdown"})

        sent = []

        async def send(msg):
            sent.append(msg)

        await dispatcher(scope, messages.get, send)

        self.assertTrue(mcp_app.started)
        self.assertTrue(mcp_app.stopped)
        self.assertEqual(sent[0]["type"], "lifespan.startup.complete")
        self.assertEqual(sent[1]["type"], "lifespan.shutdown.complete")


class RecycleConnectionsMiddlewareTestCase(SimpleTestCase):
    async def test_http_recycles_before_and_after_request(self):
        """Stale DB connections are recycled around each HTTP request.

        The MCP sub-app never passes through Django's request signals or
        MIDDLEWARE, so this wrapper must recycle the connection itself both
        before (replace a connection that died while idle) and after
        (return it, mirroring request_finished) the app runs.
        """
        order = []
        inner = MockASGIApp()

        async def track_app(scope, receive, send):
            order.append("app")
            await inner(scope, receive, send)

        async def track_recycle():
            order.append("recycle")

        with mock.patch(
            "glitchtip.asgi._recycle_db_connections", side_effect=track_recycle
        ):
            middleware = RecycleConnectionsMiddleware(track_app)
            scope = {"type": "http", "path": "/mcp", "method": "POST"}
            await middleware(scope, lambda: None, lambda msg: None)

        self.assertTrue(inner.called)
        self.assertEqual(order, ["recycle", "app", "recycle"])

    async def test_non_http_scope_skips_recycle(self):
        """Lifespan/websocket scopes never touch the ORM and pass through."""
        inner = MockASGIApp()
        with mock.patch(
            "glitchtip.asgi._recycle_db_connections", new_callable=mock.AsyncMock
        ) as recycle:
            middleware = RecycleConnectionsMiddleware(inner)
            scope = {"type": "lifespan", "asgi": {"version": "3.0"}}
            await middleware(scope, lambda: None, lambda msg: None)

        self.assertTrue(inner.called)
        recycle.assert_not_awaited()

    async def test_recycle_runs_after_request_raises(self):
        """Cleanup still runs (in finally) when the wrapped app errors."""

        async def failing_app(scope, receive, send):
            raise RuntimeError("boom")

        with mock.patch(
            "glitchtip.asgi._recycle_db_connections", new_callable=mock.AsyncMock
        ) as recycle:
            middleware = RecycleConnectionsMiddleware(failing_app)
            scope = {"type": "http", "path": "/mcp", "method": "POST"}
            with self.assertRaises(RuntimeError):
                await middleware(scope, lambda: None, lambda msg: None)

        # Once before the request, once in the finally.
        self.assertEqual(recycle.await_count, 2)


class RecycleDBConnectionsTestCase(TransactionTestCase):
    async def test_recycle_leaves_connection_usable(self):
        """_recycle_db_connections is safe against live connections.

        Recycling should recover a dead connection (via
        close_if_unusable_or_obsolete) without breaking subsequent queries,
        so a query works both before and after a recycle.
        """
        from apps.projects.models import Project

        # Warm a connection with a real query, recycle, then query again.
        await Project.objects.acount()
        await _recycle_db_connections()
        # Would raise "the connection is closed" if recycling left the
        # store wedged instead of reconnecting on demand.
        await Project.objects.acount()

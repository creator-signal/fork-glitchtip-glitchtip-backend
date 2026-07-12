"""
ASGI config for glitchtip project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/dev/howto/deployment/asgi/
"""

import asyncio
import os

from django.core.asgi import get_asgi_application

from glitchtip.startup import print_startup_banner

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "glitchtip.settings")

application = get_asgi_application()

# Print startup banner
print_startup_banner()

# Route ingest paths to a lightweight handler with minimal middleware
from glitchtip.ingest_asgi import IngestDispatcher  # noqa: E402

application = IngestDispatcher(application)

_embed_worker = os.environ.get("GLITCHTIP_EMBED_WORKER") == "true"
if _embed_worker:
    from django_vtasks.asgi import get_worker_application

    application = get_worker_application(application)


class MCPDjangoDispatcher:
    """Route MCP and OAuth requests to the MCP Starlette app, everything else to Django.

    The MCP SDK creates OAuth routes (/authorize, /token, /register, /revoke)
    internally at root level. We set issuer_url to include /mcp so the
    well-known metadata advertises them as /mcp/authorize, /mcp/register, etc.
    This avoids conflicts with Django routes (e.g. /register for the SPA).

    Clients discover all OAuth endpoints via /.well-known/oauth-authorization-server
    metadata, so the prefixed paths work transparently.

    Handles ASGI lifespan by forwarding startup/shutdown to both apps so
    the MCP Starlette app can initialise its session-manager task group.
    """

    # OAuth endpoints the MCP SDK creates (under /mcp prefix externally,
    # but the Starlette app expects them at root level internally)
    _OAUTH_SUFFIXES = frozenset({"/authorize", "/token", "/register", "/revoke"})

    def __init__(self, django_app, mcp_app, mcp_prefix="/mcp", django_lifespan=False):
        self.django_app = django_app
        self.mcp_app = mcp_app
        self.mcp_prefix = mcp_prefix
        self._django_lifespan = django_lifespan

    # Well-known prefixes that should be routed to the MCP Starlette app
    _WELL_KNOWN_PREFIXES = (
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
    )

    def _is_mcp_path(self, path: str) -> bool:
        return path.startswith(self.mcp_prefix) or any(
            path.startswith(p) for p in self._WELL_KNOWN_PREFIXES
        )

    def _rewrite_path(self, path: str) -> str:
        """Rewrite external paths to internal paths the MCP SDK expects.

        - /mcp/authorize → /authorize (OAuth endpoints)
        - /.well-known/oauth-authorization-server/mcp → /.well-known/oauth-authorization-server
          (RFC 8414: issuer with path gets suffix on well-known, but SDK registers without it)
        """
        # Strip /mcp prefix from OAuth sub-paths
        if path.startswith(self.mcp_prefix):
            suffix = path[len(self.mcp_prefix) :]
            if suffix in self._OAUTH_SUFFIXES:
                return suffix
        # Strip /mcp suffix from authorization server well-known path only.
        # RFC 8414: the SDK registers oauth-authorization-server WITHOUT the suffix.
        # RFC 9728: the SDK registers oauth-protected-resource WITH the /mcp suffix,
        # so it must NOT be rewritten.
        if path == f"/.well-known/oauth-authorization-server{self.mcp_prefix}":
            return "/.well-known/oauth-authorization-server"
        return path

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self._handle_lifespan(scope, receive, send)
        elif scope["type"] == "http" and self._is_mcp_path(scope["path"]):
            path = self._rewrite_path(scope["path"])
            await self.mcp_app(dict(scope, path=path), receive, send)
        else:
            await self.django_app(scope, receive, send)

    async def _handle_lifespan(self, scope, receive, send):
        """Multiplex ASGI lifespan to sub-applications that need it.

        The MCP Starlette app always needs lifespan to initialise its
        session-manager task group.  In all-in-one mode the django_app is
        the django-vtasks wrapper which also needs lifespan to start the
        background worker (set django_lifespan=True).  In web-only mode
        django_app is Django's ASGIHandler which does not support lifespan.
        """
        if not self._django_lifespan:
            # Web-only mode: only MCP needs lifespan, forward directly.
            await self.mcp_app(scope, receive, send)
            return

        django_queue: asyncio.Queue = asyncio.Queue()
        mcp_queue: asyncio.Queue = asyncio.Queue()
        django_ok = asyncio.Event()
        mcp_ok = asyncio.Event()
        django_done = asyncio.Event()
        mcp_done = asyncio.Event()
        failed = False

        async def django_send(msg):
            nonlocal failed
            if msg["type"] == "lifespan.startup.complete":
                django_ok.set()
            elif msg["type"] == "lifespan.startup.failed":
                failed = True
                django_ok.set()
            elif msg["type"] == "lifespan.shutdown.complete":
                django_done.set()

        async def mcp_send(msg):
            nonlocal failed
            if msg["type"] == "lifespan.startup.complete":
                mcp_ok.set()
            elif msg["type"] == "lifespan.startup.failed":
                failed = True
                mcp_ok.set()
            elif msg["type"] == "lifespan.shutdown.complete":
                mcp_done.set()

        async def run_django():
            await self.django_app(scope, django_queue.get, django_send)

        async def run_mcp():
            await self.mcp_app(scope, mcp_queue.get, mcp_send)

        tasks = [
            asyncio.create_task(run_django()),
            asyncio.create_task(run_mcp()),
        ]

        try:
            msg = await receive()
            await django_queue.put(msg)
            await mcp_queue.put(msg)
            await django_ok.wait()
            await mcp_ok.wait()

            if failed:
                await send({"type": "lifespan.startup.failed", "message": ""})
                return

            await send({"type": "lifespan.startup.complete"})

            msg = await receive()
            await django_queue.put(msg)
            await mcp_queue.put(msg)
            await django_done.wait()
            await mcp_done.wait()

            await send({"type": "lifespan.shutdown.complete"})
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


from asgiref.sync import sync_to_async  # noqa: E402
from django.conf import settings  # noqa: E402
from django.db import close_old_connections  # noqa: E402
from django_async_backend.db import close_old_async_connections  # noqa: E402


class RecycleConnectionsMiddleware:
    """Recycle stale Django DB connections around a mounted ASGI sub-app.

    The MCP Starlette app is mounted directly in the ASGI callable, so its
    requests never pass through Django's ``BaseHandler`` nor the configured
    ``MIDDLEWARE``. Neither of Django's two connection-recycling mechanisms
    fires for it:

    * the sync, thread-local store (``django.db.connections``), normally
      recycled by ``close_old_connections`` on the ``request_started`` /
      ``request_finished`` signals that ``BaseHandler`` emits; and
    * django-async-backend's per-task ``async_connections`` store, normally
      recycled by the ``close_async_connections`` middleware.

    A connection that dies while held (Postgres failover, pgbouncer restart,
    ``pg_terminate_backend``, a network blip) is therefore never detected or
    replaced, and every later MCP DB query raises
    ``OperationalError: the connection is closed`` until the worker is
    restarted. The regular request path recovers on its own because those
    signals/middleware recycle the dead connection on the next request.

    This wrapper mirrors that behaviour: it recycles both stores before and
    after each HTTP request the sub-app handles. ``close_old_connections``
    is a sync signal receiver, so it is run through ``sync_to_async`` (which
    is thread-sensitive by default) — the same thread-sensitive executor the
    async ORM uses for its sync work — so it sees and recycles the very
    connection wrappers the sub-app's ``aget``/``async for`` queries opened.
    On a healthy connection both calls are no-ops, so steady-state cost is
    negligible.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        # Only HTTP requests touch the ORM; forward lifespan/websocket as-is.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Recycle before the request so a connection that died while idle is
        # replaced up front rather than surfacing as a 500 on the first query.
        await _recycle_db_connections()
        try:
            await self.app(scope, receive, send)
        finally:
            # shield so a client disconnect mid-response can't cancel cleanup
            # and leak the connection into the next request (mirrors
            # django_async_backend.middleware.close_async_connections).
            await asyncio.shield(_recycle_db_connections())


async def _recycle_db_connections():
    await sync_to_async(close_old_connections)()
    await close_old_async_connections()


if settings.GLITCHTIP_ENABLE_MCP:
    from apps.mcp.server import mcp as _mcp_server

    _mcp_app = _mcp_server.streamable_http_app()
    application = MCPDjangoDispatcher(
        django_app=application,
        mcp_app=RecycleConnectionsMiddleware(_mcp_app),
        django_lifespan=_embed_worker,
    )

# Wrap application with granian proxy headers support
# This allows granian to properly handle X-Forwarded-For and X-Forwarded-Proto headers
# when running behind a reverse proxy (nginx, traefik, k8s ingress, etc.)
try:
    from granian.utils.proxies import wrap_asgi_with_proxy_headers

    # TRUSTED_PROXIES can be:
    # - "*" to trust all proxies (default, safe when granian isn't directly exposed)
    # - A comma-separated list of IPs/CIDR ranges (e.g., "10.0.0.0/8,172.16.0.0/12")
    # - For k8s: "*" is recommended since pod IPs are dynamic and pods aren't exposed
    trusted_proxies = os.environ.get("TRUSTED_PROXIES", "*")
    if "," in trusted_proxies:
        trusted_proxies = [ip.strip() for ip in trusted_proxies.split(",")]

    application = wrap_asgi_with_proxy_headers(
        application, trusted_hosts=trusted_proxies
    )
except ImportError:
    # Graceful fallback if granian can't be imported (custom installations, etc.)
    # The wrapper is safe to use with uwsgi but this provides defensive error handling
    pass

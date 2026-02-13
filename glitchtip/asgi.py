"""
ASGI config for glitchtip project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/dev/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application

from glitchtip.startup import print_startup_banner

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "glitchtip.settings")

application = get_asgi_application()

# Print startup banner
print_startup_banner()

if os.environ.get("GLITCHTIP_EMBED_WORKER") == "true":
    from django_vtasks.asgi import get_worker_application

    application = get_worker_application(application)


class MCPDjangoDispatcher:
    """Route /mcp* requests to the MCP Starlette app, everything else to Django.

    Handles ASGI lifespan by forwarding startup/shutdown to both apps so
    the MCP Starlette app can initialise its session-manager task group.
    """

    def __init__(self, django_app, mcp_app, mcp_prefix="/mcp"):
        self.django_app = django_app
        self.mcp_app = mcp_app
        self.mcp_prefix = mcp_prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self._handle_lifespan(scope, receive, send)
        elif scope["type"] == "http" and scope["path"].startswith(self.mcp_prefix):
            await self.mcp_app(scope, receive, send)
        else:
            await self.django_app(scope, receive, send)

    async def _handle_lifespan(self, scope, receive, send):
        """Multiplex ASGI lifespan to both the Django app and MCP app.

        The MCP Starlette app needs lifespan to initialise its session-manager
        task group.  In all-in-one mode the django_app is the django-vtasks
        wrapper which also needs lifespan to start the background worker.
        In web-only mode django_app is Django's ASGIHandler which raises
        ValueError for lifespan — that is caught and treated as a no-op.
        """
        import asyncio

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
            try:
                await self.django_app(scope, django_queue.get, django_send)
            except Exception:
                # Django's ASGIHandler raises ValueError for lifespan scope.
                # In web-only mode (no vtasks wrapper) this is expected.
                django_ok.set()
                django_done.set()

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


from django.conf import settings  # noqa: E402

if settings.GLITCHTIP_ENABLE_MCP:
    from apps.mcp.server import mcp as _mcp_server

    _mcp_app = _mcp_server.streamable_http_app()
    application = MCPDjangoDispatcher(django_app=application, mcp_app=_mcp_app)

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

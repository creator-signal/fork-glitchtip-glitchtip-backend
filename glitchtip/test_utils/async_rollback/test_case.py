"""
AsyncioRollbackTestCase — Django TestCase that also rolls back async writes.

Vendored from django-async-backend PR #20:
https://github.com/Arfey/django-async-backend/pull/20

Copied from upstream commit ea07ec8 (branch
``feat/asyncio-rollback-testcase``). Remove this file and import from
``django_async_backend.test`` once upstream merges.

What this gives you
-------------------

A drop-in replacement for ``django.test.TestCase`` that also covers
writes performed via ``django_async_backend.db.async_connections``.
Inside the test, ``async_connections[alias].cursor()`` returns a proxy
that routes through Django's sync connection — the same one Django's
``TestCase`` opened a transaction on — so the per-test savepoint
rollback covers writes from both pools.

Class-scope install (the GlitchTip-driven addition)
---------------------------------------------------

The proxy is installed once per test class in ``setUpClass`` by
swapping ``async_connections._connections`` (and Django's
``connections._connections``) to a thread-blind ``SimpleNamespace``
for the class duration. That means even sync test methods that drive
async code through ``async_to_sync(...)`` see the proxy on the worker
thread — without that swap, the worker resolves a fresh per-thread
async connection that opens its own autocommit transaction.

Tradeoff: there's no real concurrency between two tasks using the
bridged connection during a test — they serialise through Django's
sync wrapper. Same shape as Django's regular TestCase semantics
(one transaction, no parallelism). Production keeps its real async
pool unchanged.
"""

import asyncio
import types

from asgiref.sync import async_to_sync
from django.db import connections as django_sync_connections
from django.test import TestCase as DjangoTestCase
from django_async_backend.db import async_connections

from glitchtip.test_utils.async_rollback.sync_bridge import SyncBridgedAsyncWrapper


class AsyncioRollbackTestCase(DjangoTestCase):
    """Django ``TestCase`` that also rolls back ``async_connections`` writes."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Capture the sync conns Django's TestCase opened its
        # transaction on (this runs on the main test thread).
        aliases = list(cls._databases_names(include_mirrors=False))
        sync_conns = {alias: django_sync_connections[alias] for alias in aliases}

        # Allow worker threads spawned by ``async_to_sync`` to use the
        # sync conn opened on the main thread.
        for conn in sync_conns.values():
            conn.inc_thread_sharing()

        # Build one proxy per alias and stash on the class so test
        # methods can reset their per-test state.
        proxies = {
            alias: SyncBridgedAsyncWrapper(alias, sync_conn=conn)
            for alias, conn in sync_conns.items()
        }
        cls._async_rollback_proxies = proxies

        # Swap both connection stores for thread-blind SimpleNamespaces.
        # ``django_sync_connections._connections`` is normally an
        # asgiref.Local → ContextVar; the SimpleNamespace makes the
        # asgiref worker thread reuse the same wrapper instead of
        # opening its own autocommit connection. Same trick for
        # ``async_connections._connections`` so any task accessing
        # ``async_connections[alias]`` — main thread or worker — gets
        # the proxy.
        cls._async_rollback_orig_sync_storage = django_sync_connections._connections
        django_sync_connections._connections = types.SimpleNamespace(**sync_conns)

        cls._async_rollback_orig_async_storage = async_connections._connections
        async_connections._connections = types.SimpleNamespace(**proxies)

    @classmethod
    def tearDownClass(cls):
        # Restore original storage before parent tearDownClass closes
        # transactions etc.
        async_connections._connections = cls._async_rollback_orig_async_storage
        django_sync_connections._connections = cls._async_rollback_orig_sync_storage
        for alias in cls._databases_names(include_mirrors=False):
            django_sync_connections[alias].dec_thread_sharing()
        super().tearDownClass()

    def _reset_proxy_state(self):
        """Reset per-test mutable state on every proxy.

        The class-scope proxies persist for the whole class; their
        atomic/savepoint bookkeeping needs to start fresh for each
        test method (Django's ``TestCase`` opens a per-test savepoint
        on the sync conn separately).
        """
        for proxy in self._async_rollback_proxies.values():
            proxy.in_atomic_block = False
            proxy.savepoint_state = 0
            proxy.savepoint_ids = []
            proxy.atomic_blocks = []
            proxy.commit_on_exit = True
            proxy.needs_rollback = False
            proxy.rollback_exc = None
            proxy.run_on_commit = []
            proxy._task = None

    def _callTestMethod(self, method):
        self._reset_proxy_state()

        # Django's SimpleTestCase.__call__ has already wrapped async
        # test methods with async_to_sync, so ``method`` here is sync.
        # Look at the original on the class to decide which path to take.
        raw = getattr(type(self), self._testMethodName, None)
        if asyncio.iscoroutinefunction(raw):
            async_to_sync(self._run_async_test)(raw.__get__(self, type(self)))
        else:
            # Sync test method. Anything inside that calls
            # ``async_to_sync(some_async)`` will run on the asgiref
            # worker thread — the SimpleNamespace storage swap means
            # it still sees the proxies installed in setUpClass.
            super()._callTestMethod(method)

    async def _run_async_test(self, method):
        current_task = asyncio.current_task()
        for proxy in self._async_rollback_proxies.values():
            proxy._task = current_task

        async_setup = getattr(self, "asyncSetUp", None)
        if asyncio.iscoroutinefunction(async_setup):
            await async_setup()
        if method is not None:
            await method()
        async_teardown = getattr(self, "asyncTearDown", None)
        if asyncio.iscoroutinefunction(async_teardown):
            await async_teardown()

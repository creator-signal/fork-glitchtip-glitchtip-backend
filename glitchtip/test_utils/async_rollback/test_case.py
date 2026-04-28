"""
AsyncioRollbackTestCase — Django TestCase that also rolls back async writes.

Vendored from django-async-backend PR #20:
https://github.com/Arfey/django-async-backend/pull/20

Copied from upstream commit 6e9d8fe (branch
``feat/asyncio-rollback-testcase``). Remove this file and import from
``django_async_backend.test`` once upstream merges.
"""

import asyncio
import types

from asgiref.sync import async_to_sync
from django.db import connections as django_sync_connections
from django.test import TestCase as DjangoTestCase
from django_async_backend.db import async_connections

from glitchtip.test_utils.async_rollback.sync_bridge import SyncBridgedAsyncWrapper


class AsyncioRollbackTestCase(DjangoTestCase):
    """Django ``TestCase`` that also rolls back ``async_connections`` writes.

    Routes every ``async_connections[alias]`` operation through Django's sync
    connection for the duration of the test. The single transaction Django
    opens around the class then covers writes done via either pool, and
    per-test savepoints roll both back together.

    Async test methods (``async def test_*``) are run via ``async_to_sync``;
    optional ``asyncSetUp`` / ``asyncTearDown`` hooks share the same event
    loop and task as the test method.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Allow the dedicated async_to_sync worker thread to use the sync conn
        # opened on the main test thread.
        for alias in cls._databases_names(include_mirrors=False):
            django_sync_connections[alias].inc_thread_sharing()

    @classmethod
    def tearDownClass(cls):
        for alias in cls._databases_names(include_mirrors=False):
            django_sync_connections[alias].dec_thread_sharing()
        super().tearDownClass()

    def _callTestMethod(self, method):
        # Django's SimpleTestCase.__call__ has already wrapped async test
        # methods with async_to_sync, so ``method`` here is sync. Look at
        # the original on the class to decide which path to take.
        raw = getattr(type(self), self._testMethodName, None)
        if asyncio.iscoroutinefunction(raw):
            sync_conns = {
                alias: django_sync_connections[alias]
                for alias in type(self)._databases_names(include_mirrors=False)
            }
            async_to_sync(self._run_async_test)(
                raw.__get__(self, type(self)), sync_conns
            )
        else:
            super()._callTestMethod(method)

    async def _run_async_test(self, method, sync_conns):
        proxies = {
            alias: SyncBridgedAsyncWrapper(alias, sync_conn=conn)
            for alias, conn in sync_conns.items()
        }

        # Make sync_to_async calls inside this test see the same Django
        # sync wrapper the TestCase opened its transaction on. Django's
        # default connections._connections is thread-local; swapping in a
        # plain SimpleNamespace makes it thread-blind for the test's
        # duration so the asgiref worker thread reuses the main wrapper
        # instead of opening a new autocommit connection.
        sync_conn_storage = types.SimpleNamespace(**sync_conns)
        original_sync_storage = django_sync_connections._connections
        django_sync_connections._connections = sync_conn_storage

        current_task = asyncio.current_task()
        originals = {}
        for alias, proxy in proxies.items():
            proxy._task = current_task
            try:
                originals[alias] = getattr(async_connections._connections, alias)
            except AttributeError:
                originals[alias] = None
            setattr(async_connections._connections, alias, proxy)
        try:
            async_setup = getattr(self, "asyncSetUp", None)
            if asyncio.iscoroutinefunction(async_setup):
                await async_setup()
            if method is not None:
                await method()
            async_teardown = getattr(self, "asyncTearDown", None)
            if asyncio.iscoroutinefunction(async_teardown):
                await async_teardown()
        finally:
            for alias, original in originals.items():
                if original is None:
                    if hasattr(async_connections._connections, alias):
                        delattr(async_connections._connections, alias)
                else:
                    setattr(async_connections._connections, alias, original)
            django_sync_connections._connections = original_sync_storage

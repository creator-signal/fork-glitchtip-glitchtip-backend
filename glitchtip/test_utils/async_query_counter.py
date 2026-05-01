"""Sync-callable counter for queries that run on async-backend cursors.

Django's ``assertNumQueries`` watches ``connections[alias]``, which is
distinct from ``async_connections[alias]`` — the two have separate cursor
classes, separate ``queries_log`` deques, and separate per-task wrappers.
So once the ingest hot path moved to ``async_connections``, the existing
assertions silently stopped seeing the queries they were meant to count.

``AsyncCaptureQueriesContext`` from django-async-backend solves this for
async test methods, but our hot-path tests are sync (they POST through
the test client, which dispatches to async views via ``async_to_sync``).
The async wrapper that actually executes the queries lives on a different
task than the test thread, so reading its ``queries_log`` from outside
isn't reliable.

Patching ``AsyncCursorWrapper.execute`` / ``executemany`` at the class
level sidesteps the per-task storage entirely. The patch is shared by
every wrapper in every task, so the counter sees queries no matter where
they ran. This intentionally counts only queries that go through async
cursors; sync queries (test ``setUp`` fixtures, middleware, ORM calls
that haven't been ported yet) are invisible.
"""

from django_async_backend.db.backends.utils import AsyncCursorWrapper


class AsyncQueryCounter:
    """Sync context manager that counts async-cursor executions.

    Usage::

        with AsyncQueryCounter() as counter:
            self.client.post(url, payload)
            task_backends["default"].flush_batches()
        self.assertEqual(len(counter), 8)
    """

    def __init__(self) -> None:
        self.count = 0

    def __enter__(self) -> "AsyncQueryCounter":
        self._orig_execute = AsyncCursorWrapper.execute
        self._orig_executemany = AsyncCursorWrapper.executemany
        counter = self
        orig_execute = self._orig_execute
        orig_executemany = self._orig_executemany

        async def execute(cursor_self, sql, params=None):
            counter.count += 1
            return await orig_execute(cursor_self, sql, params)

        async def executemany(cursor_self, sql, param_list):
            counter.count += 1
            return await orig_executemany(cursor_self, sql, param_list)

        AsyncCursorWrapper.execute = execute
        AsyncCursorWrapper.executemany = executemany
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        AsyncCursorWrapper.execute = self._orig_execute
        AsyncCursorWrapper.executemany = self._orig_executemany

    def __len__(self) -> int:
        return self.count

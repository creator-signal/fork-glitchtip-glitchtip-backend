"""
Async database helpers that use django-async-backend when available,
falling back to sync_to_async wrappers otherwise.
"""

import contextlib

from asgiref.sync import sync_to_async
from django.db import connection

try:
    from django_async_backend.db.utils import async_connections

    _has_async_backend = True
except ImportError:
    _has_async_backend = False


def has_async_backend():
    return _has_async_backend


@contextlib.asynccontextmanager
async def async_cursor(using="default"):
    """Async context manager for a native async database cursor.

    Only available when django-async-backend is installed. Falls back
    to wrapping a sync cursor in sync_to_async otherwise.
    """
    if _has_async_backend:
        conn = async_connections[using]
        await conn.ensure_connection()
        cursor = await conn.cursor()
        try:
            yield cursor
        finally:
            await cursor.close()
    else:
        # Fallback: yield a wrapper that runs sync cursor ops in a thread.
        # The wrapper holds all cursor state in a single thread via sync_to_async.
        wrapper = _ThreadBoundCursorWrapper()
        await wrapper._open()
        try:
            yield wrapper
        finally:
            await wrapper._close()


class _ThreadBoundCursorWrapper:
    """Wraps a sync cursor, ensuring all operations run in the same thread."""

    def __init__(self):
        self._cursor = None

    @sync_to_async
    def _open(self):
        self._cursor = connection.cursor()

    @sync_to_async
    def _close(self):
        if self._cursor:
            self._cursor.close()

    def mogrify(self, sql, params):
        # mogrify in psycopg3 ClientCursor is CPU-only string formatting.
        # It uses the connection's type adapters but doesn't touch the socket.
        return self._cursor.mogrify(sql, params)

    @sync_to_async
    def execute(self, sql, params=None):
        return self._cursor.execute(sql, params)

    @sync_to_async
    def executemany(self, sql, params_list):
        return self._cursor.executemany(sql, params_list)

    @sync_to_async
    def fetchone(self):
        return self._cursor.fetchone()

    @sync_to_async
    def fetchall(self):
        return self._cursor.fetchall()

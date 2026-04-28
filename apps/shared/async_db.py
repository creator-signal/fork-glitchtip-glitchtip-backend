"""Async raw-SQL helpers for the ingest hot paths (events, logs, spans).

Uses django-async-backend's ``async_connections`` directly. The DB
ENGINE is set to async-backend's postgres backend in
:mod:`glitchtip.settings`, so this is the only path.

Tests cover these helpers via
:class:`glitchtip.test_utils.async_rollback.AsyncioRollbackTestCase`,
which routes ``async_connections[alias]`` through Django's sync conn
inside a TestCase so the per-test transaction rolls back writes from
both pools together.
"""

from __future__ import annotations

from typing import Any

from django_async_backend.db import async_connections


async def afetchall(
    sql: str,
    params: Any | None = None,
    db_alias: str = "default",
) -> tuple[list[str], list[tuple]]:
    """Execute ``sql`` with ``params`` and return ``(columns, rows)``."""
    async with await async_connections[db_alias].cursor() as cursor:
        await cursor.execute(sql, params)
        columns = [c[0] for c in cursor.description]
        rows = await cursor.fetchall()
        return columns, rows


async def aexecute(
    sql: str,
    params: Any | None = None,
    db_alias: str = "default",
) -> int:
    """Execute ``sql`` with ``params`` and return ``cursor.rowcount``.

    For UPDATE/INSERT/DELETE that doesn't need rows back. Use
    :func:`afetchall` for SELECT.
    """
    async with await async_connections[db_alias].cursor() as cursor:
        await cursor.execute(sql, params)
        return cursor.rowcount


async def afetchall_mogrified_values(
    sql_template: str,
    values_fragment: str,
    value_params: list[tuple],
    db_alias: str = "default",
) -> tuple[list[str], list[tuple]]:
    """Mogrify ``value_params`` into ``values_fragment`` (one row each),
    substitute the joined literals into ``sql_template`` where
    ``{values}`` appears, then execute."""
    conn = async_connections[db_alias]
    parts: list[str] = []
    for row in value_params:
        # compose_sql may return bytes (psycopg2-style) or str
        # (psycopg3 ClientCursor). Normalise before joining.
        part = await conn.ops.compose_sql(values_fragment, row)
        if isinstance(part, (bytes, bytearray)):
            part = part.decode()
        parts.append(part)
    final = sql_template.format(values=",".join(parts))
    async with await conn.cursor() as cursor:
        await cursor.execute(final)
        columns = [c[0] for c in cursor.description]
        rows = await cursor.fetchall()
        return columns, rows


async def aexecute_mogrified_values(
    sql_template: str,
    values_fragment: str,
    value_params: list[tuple],
    db_alias: str = "default",
) -> int:
    """Like :func:`afetchall_mogrified_values` but for UPDATE/INSERT —
    discards any result rows and returns ``cursor.rowcount``."""
    conn = async_connections[db_alias]
    parts: list[str] = []
    for row in value_params:
        part = await conn.ops.compose_sql(values_fragment, row)
        if isinstance(part, (bytes, bytearray)):
            part = part.decode()
        parts.append(part)
    final = sql_template.format(values=",".join(parts))
    async with await conn.cursor() as cursor:
        await cursor.execute(final)
        return cursor.rowcount

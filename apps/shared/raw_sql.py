"""Async raw-SQL helpers for the ingest hot paths (events, logs, spans).

Uses django-async-backend's native async cursor via ``async_connections``.
"""

from collections.abc import Iterable
from typing import Any

from django.db import connections
from django_async_backend.db import async_connections


async def fetchall(
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


async def fetchone(
    sql: str,
    params: Any | None = None,
    db_alias: str = "default",
) -> tuple | None:
    """Execute ``sql`` with ``params`` and return a single row (or None)."""
    async with await async_connections[db_alias].cursor() as cursor:
        await cursor.execute(sql, params)
        return await cursor.fetchone()


async def execute(
    sql: str,
    params: Any | None = None,
    db_alias: str = "default",
) -> int:
    """Execute ``sql`` with ``params`` and return ``cursor.rowcount``.

    For UPDATE/INSERT/DELETE that doesn't need rows back. Use
    :func:`fetchall` for SELECT.
    """
    async with await async_connections[db_alias].cursor() as cursor:
        await cursor.execute(sql, params)
        return cursor.rowcount


async def fetchall_mogrified_values(
    sql_template: str,
    values_fragment: str,
    value_params: list[tuple],
    db_alias: str = "default",
) -> tuple[list[str], list[tuple]]:
    """Mogrify ``value_params`` into ``values_fragment`` (one row each),
    substitute the joined literals into ``sql_template`` where
    ``{values}`` appears, then execute.

    Mirrors the ``cursor.mogrify("(%s,%s::uuid)", pair)`` pattern we
    use in :func:`~apps.event_ingest.process_event._fetch_issue_hashes_raw`.
    """
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


async def execute_mogrified_values(
    sql_template: str,
    values_fragment: str,
    value_params: list[tuple],
    db_alias: str = "default",
) -> int:
    """Like :func:`fetchall_mogrified_values` but for UPDATE/INSERT —
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


async def execute_unnest(
    sql: str,
    value_params: list[tuple],
    db_alias: str = "default",
) -> int:
    """Transpose row-major ``value_params`` to per-column arrays and execute.

    ``sql`` is expected to call ``unnest(%s::T[], %s::T[], ...)`` with one
    ``%s`` per column. This avoids the per-row mogrify round-trip and the
    65535 bind-parameter cap that ``execute_mogrified_values`` hits with
    wide schemas, and gives Postgres a single statement shape for the
    plan cache regardless of batch size.
    """
    if not value_params:
        return 0
    columns = [list(c) for c in zip(*value_params)]
    return await execute(sql, columns, db_alias=db_alias)


async def fetchall_unnest(
    sql: str,
    value_params: list[tuple],
    db_alias: str = "default",
) -> tuple[list[str], list[tuple]]:
    """Like :func:`execute_unnest` but returns ``(columns, rows)``."""
    if not value_params:
        return [], []
    columns = [list(c) for c in zip(*value_params)]
    return await fetchall(sql, columns, db_alias=db_alias)


def copy_from_supported(db_alias: str = "default") -> bool:
    """Whether :func:`copy_rows` (psycopg ``COPY FROM STDIN``) applies.

    The Rust driver caps its per-connection buffers internally, so its
    INSERT path doesn't retain batch-sized memory and COPY buys nothing
    there; it also has its own COPY semantics. libpq has no such cap —
    a composed INSERT permanently grows the connection's wire buffer to
    the statement size — so COPY is the bounded bulk-write path for the
    psycopg engine specifically.
    """
    return "gt_rust" not in connections.databases[db_alias]["ENGINE"]


async def copy_rows(
    table: str,
    columns: list[str],
    rows: Iterable[tuple],
    db_alias: str = "default",
) -> int:
    """COPY ``rows`` into ``table`` (text format, streamed row-by-row).

    Unlike a composed INSERT statement — which stages the entire batch in
    the connection's libpq output buffer and permanently grows it to the
    largest batch ever sent — COPY streams in small chunks, so connection
    memory stays bounded regardless of batch size. It also skips composing
    the batch into one SQL string in Python.

    COPY cannot express ON CONFLICT: a conflicting row aborts the whole
    batch (raised as ``django.db.IntegrityError``). Callers that need
    conflict tolerance must catch it and fall back to their INSERT path.
    """
    cols = ", ".join(f'"{c}"' for c in columns)
    count = 0
    async with await async_connections[db_alias].cursor() as cursor:
        # ``cursor.copy`` reaches the underlying psycopg cursor via the
        # wrapper's attribute proxy, which doesn't wrap exceptions — do it
        # here so callers see Django's IntegrityError, not psycopg's.
        with cursor.db.wrap_database_errors:
            async with cursor.copy(f'COPY "{table}" ({cols}) FROM STDIN') as copy:
                for row in rows:
                    await copy.write_row(row)
                    count += 1
    return count

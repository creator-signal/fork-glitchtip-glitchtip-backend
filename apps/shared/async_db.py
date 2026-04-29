"""Async raw-SQL helpers used by the ingest hot paths.

The multi-row ``VALUES`` insert pattern needs ``compose_sql`` per row
and a final ``str.format`` substitution before execute. Two helpers
for that — read variant and write variant. Plain
``await async_connections[alias].cursor()`` is preferred everywhere
else.
"""

from django_async_backend.db import async_connections


async def fetchall_mogrified_values(
    sql_template: str,
    values_fragment: str,
    value_params: list[tuple],
    db_alias: str = "default",
) -> tuple[list[str], list[tuple]]:
    """Mogrify ``value_params`` into ``values_fragment`` (one row each),
    substitute the joined literals into ``sql_template`` where
    ``{values}`` appears, then execute and return ``(columns, rows)``.

    One round-trip vs. ``executemany``'s one-per-row.
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

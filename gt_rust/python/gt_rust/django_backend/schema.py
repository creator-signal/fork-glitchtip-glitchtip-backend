"""Schema editor for the gt_rust backend.

PostgreSQL does not accept bind parameters in DDL (``ALTER TABLE ... ADD
COLUMN ... DEFAULT $1`` is rejected with "expected 0 parameters but got
N"), so the stock postgresql schema editor inlines params client-side via
``psycopg.ClientCursor.mogrify``. We don't want to route through psycopg
just for this step, so we provide a minimal quote-and-inline path that
covers the value shapes Django actually emits during migrations.
"""

from __future__ import annotations

import datetime
import decimal
import json
import uuid

from django.db.backends.ddl_references import Statement
from django.db.backends.postgresql.schema import (
    DatabaseSchemaEditor as PgSchemaEditor,
)


def _quote(value) -> str:
    """Literal SQL representation of ``value``.

    Mirrors psycopg.sql.quote for the types Django's migrations use:
    strings, numbers, booleans, None, datetimes, UUIDs, bytes. Anything
    else falls through to ``str()`` with single-quoting.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, decimal.Decimal)):
        return str(value)
    if isinstance(value, bytes):
        return "'\\x" + value.hex() + "'::bytea"
    if isinstance(value, datetime.datetime):
        # Emit an explicit cast. Without it, Postgres infers TEXT in a
        # VALUES clause, which breaks downstream comparisons like
        # ``GREATEST(timestamptz_col, v.last_seen)``.
        cast = "timestamptz" if value.tzinfo is not None else "timestamp"
        return "'" + value.isoformat() + "'::" + cast
    if isinstance(value, datetime.date):
        return "'" + value.isoformat() + "'::date"
    if isinstance(value, datetime.time):
        return "'" + value.isoformat() + "'::time"
    if isinstance(value, uuid.UUID):
        return "'" + str(value) + "'::uuid"
    if isinstance(value, (list, tuple)):
        # Emit PG array literal string form ('{...}') rather than
        # ARRAY[...]: the string form participates in implicit cast
        # resolution within a DEFAULT clause, so an empty default like
        # ``DEFAULT '{}'`` works without knowing the element type here.
        # The ARRAY[] form requires an explicit cast we don't have
        # context to emit.
        parts: list[str] = []
        for v in value:
            if v is None:
                parts.append("NULL")
            elif isinstance(v, bool):
                parts.append("t" if v else "f")
            elif isinstance(v, (int, float, decimal.Decimal)):
                parts.append(str(v))
            else:
                # Array elements: escape backslash and double-quote, then
                # wrap in double quotes.
                s = str(v).replace("\\", "\\\\").replace('"', '\\"')
                parts.append('"' + s + '"')
        return "'{" + ",".join(parts) + "}'"
    # psycopg JSON wrappers: Django wraps JSONField defaults as
    # psycopg.types.json.Jsonb/Json so psycopg encodes them as JSONB.
    # We need to unwrap and emit a quoted JSON literal cast to jsonb.
    type_name = type(value).__name__
    if type_name in ("Jsonb", "Json") and hasattr(value, "obj"):
        encoded = json.dumps(value.obj).replace("'", "''")
        suffix = "::jsonb" if type_name == "Jsonb" else "::json"
        return "'" + encoded + "'" + suffix
    if isinstance(value, (dict,)):
        encoded = json.dumps(value).replace("'", "''")
        return "'" + encoded + "'::jsonb"
    # Text fallback: escape single quotes. E'' prefix allows backslash
    # escapes to work on databases where standard_conforming_strings is
    # off; with it on (PG default since 9.1) backslash is literal.
    text = str(value).replace("'", "''")
    return "'" + text + "'"


def _inline_params(sql: str, params) -> str:
    """Replace each ``%s`` / ``%(name)s`` in ``sql`` with a quoted literal.

    Matches psycopg's ClientCursor.mogrify. Skips placeholders inside
    single-quoted string literals, double-quoted identifiers, dollar-
    quoted bodies, and SQL comments — the same surface ``convert_paramstyle``
    handles. Hot-path ingest code (``apps.event_ingest``) calls into this
    via ``cursor.mogrify``, so the quote-aware skip is load-bearing, not
    just defence-in-depth.
    """
    if params is None:
        return sql.replace("%%", "%")
    is_mapping = isinstance(params, dict)
    seq: list = [] if is_mapping else list(params)
    out_parts: list[str] = []
    idx = 0
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        # line comment
        if c == "-" and i + 1 < n and sql[i + 1] == "-":
            end = sql.find("\n", i + 2)
            if end < 0:
                out_parts.append(sql[i:])
                i = n
                continue
            out_parts.append(sql[i : end + 1])
            i = end + 1
            continue
        # block comment
        if c == "/" and i + 1 < n and sql[i + 1] == "*":
            end = sql.find("*/", i + 2)
            if end < 0:
                raise ValueError("unterminated block comment")
            out_parts.append(sql[i : end + 2])
            i = end + 2
            continue
        # single-quoted literal (incl. E'..' escape strings)
        if c == "'":
            prev = sql[i - 1] if i > 0 else ""
            prev_prev = sql[i - 2] if i > 1 else ""
            is_e_string = prev in ("E", "e") and not (
                prev_prev.isalnum() or prev_prev == "_"
            )
            j = i + 1
            while j < n:
                ch = sql[j]
                if is_e_string and ch == "\\" and j + 1 < n:
                    j += 2
                    continue
                if ch == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out_parts.append(sql[i:j])
            i = j
            continue
        # double-quoted identifier
        if c == '"':
            j = i + 1
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out_parts.append(sql[i:j])
            i = j
            continue
        # dollar-quoted body: $[tag]$ ... $[tag]$
        if c == "$":
            tag_end = sql.find("$", i + 1)
            if tag_end > i:
                tag = sql[i : tag_end + 1]
                if all(ch.isalnum() or ch == "_" for ch in tag[1:-1]):
                    end = sql.find(tag, tag_end + 1)
                    if end > tag_end:
                        out_parts.append(sql[i : end + len(tag)])
                        i = end + len(tag)
                        continue
            # fall through — not a dollar-quote opener
        # placeholder
        if c == "%" and i + 1 < n:
            nxt = sql[i + 1]
            if nxt == "%":
                out_parts.append("%")
                i += 2
                continue
            if nxt == "s":
                if is_mapping:
                    raise ValueError(
                        "positional %s placeholder used with mapping params"
                    )
                if idx >= len(seq):
                    raise IndexError(
                        f"Not enough params for %s placeholders in {sql!r}"
                    )
                out_parts.append(_quote(seq[idx]))
                idx += 1
                i += 2
                continue
            if nxt == "(":
                close = sql.find(")", i + 2)
                if close < 0 or close + 1 >= n or sql[close + 1] != "s":
                    raise ValueError(
                        f"malformed named placeholder at position {i}"
                    )
                name = sql[i + 2 : close]
                if not is_mapping:
                    raise ValueError(
                        "named %(name)s placeholder used with sequence params"
                    )
                if name not in params:
                    raise KeyError(f"missing parameter: {name!r}")
                out_parts.append(_quote(params[name]))
                i = close + 2
                continue
        out_parts.append(c)
        i += 1
    if not is_mapping and idx != len(seq):
        raise ValueError(
            f"Param count mismatch: {idx} placeholders consumed, "
            f"{len(seq)} params supplied"
        )
    # Mirror convert_paramstyle's psycopg-compat behaviour: ``%%`` is a
    # literal ``%`` even inside string literals (since the user is in
    # ``%s`` paramstyle land and cannot otherwise express a literal
    # ``%`` in the SQL). The placeholder branch above already handled
    # ``%%`` outside strings; mop up the inside-strings case here so
    # ``compose_sql`` and ``cursor.query`` agree with the wire SQL.
    return "".join(out_parts).replace("%%", "%")


class DatabaseSchemaEditor(PgSchemaEditor):
    def execute(self, sql, params=()):
        if isinstance(sql, Statement):
            sql = sql.template % {k: str(v) for k, v in sql.parts.items()}
        if params is None:
            # Django uses params=None to mean "already fully resolved SQL".
            final = sql
        else:
            final = _inline_params(sql, params)
        if self.collect_sql:
            ending = "" if final.rstrip().endswith(";") else ";"
            self.collected_sql.append(final + ending)
            return
        # DDL can't use extended-protocol binds (Postgres rejects bind
        # placeholders on ALTER/CREATE). Since we already inlined params
        # above, route straight to the simple_query protocol which also
        # accepts multi-statement bodies — Django regularly emits
        # "SET CONSTRAINTS ...; ALTER TABLE ..." as one string.
        self.connection.connection._batch_execute_sync(final)

    def quote_value(self, value):
        return _quote(value)

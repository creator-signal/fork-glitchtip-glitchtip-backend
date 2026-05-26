"""DB-API 2.0 (PEP 249) shim over the RustPgDriver extension.

Minimal surface so Django's `django.db.backends.postgresql` backend can
drive us in place of psycopg. Not a full psycopg3 clone — we only
implement what Django's postgresql backend and migration/ORM paths
actually touch.

Key mismatches we bridge:

* Django emits ``%s`` placeholders; tokio-postgres wants ``$1..$N``.
  :func:`convert_paramstyle` rewrites these while respecting single-quoted
  strings, double-quoted identifiers, dollar-quoted bodies, and ``%%``.
* Django expects DB-API exception types; the Rust layer raises
  ``PyValueError``/``PyConnectionError``/``PyRuntimeError``. We catch
  those and re-raise as the right DB-API type.
* Django expects ``connection.info.server_version`` and
  ``parameter_status``; we stub both.

Implemented surface (psycopg-compat): named server-side cursors via
``connection.cursor(name=...)`` (real ``DECLARE``/``FETCH``/``CLOSE``),
``COPY TO STDOUT`` via ``cursor.copy()``, ``cursor.query`` (post-substitution
SQL bytes), ``cursor.mogrify``, ``callproc``, ``compose_sql`` /
``last_executed_query`` Django operations hooks. Binary params and async
prepared-statement pipelining are not exposed."""

import re
import threading as _threading
from typing import Any, Iterable

from gt_rust._rust import RustPgDriver  # type: ignore

# ── DB-API 2.0 module-level attributes ──────────────────────────────────
__version__ = "0.1.0"
apilevel = "2.0"
threadsafety = 2  # threads may share the module and connections
paramstyle = "pyformat"  # Django emits %s and %(name)s; we convert to $N


# ── Exceptions (PEP 249 hierarchy) ──────────────────────────────────────
class Warning(Exception):  # noqa: A001 — name required by PEP 249
    pass


class Error(Exception):
    pass


class InterfaceError(Error):
    pass


class DatabaseError(Error):
    pass


class DataError(DatabaseError):
    pass


class OperationalError(DatabaseError):
    pass


class IntegrityError(DatabaseError):
    pass


class InternalError(DatabaseError):
    pass


class ProgrammingError(DatabaseError):
    pass


class NotSupportedError(DatabaseError):
    pass


# SQLSTATE class code → exception class. Django's postgresql backend
# re-wraps these via django.db.utils.DatabaseErrorWrapper, so the exact
# type matters for IntegrityError handling (get_or_create, etc.).
_SQLSTATE_MAP: dict[str, type[DatabaseError]] = {
    "23": IntegrityError,  # integrity constraint violation
    "22": DataError,  # data exception
    "42": ProgrammingError,  # syntax error or access rule violation
    "08": OperationalError,  # connection exception
    "53": OperationalError,  # insufficient resources
    "57": OperationalError,  # operator intervention
    "58": OperationalError,  # system error
    "XX": InternalError,  # internal error
    "40": OperationalError,  # transaction rollback (serialization failures)
}


def _translate_rust_error(
    exc: BaseException,
    *,
    sql: str | None = None,
    params: Any | None = None,
) -> Error:
    """Map a Rust-side exception to a DB-API exception type.

    The Rust extension encodes its classification by the Python
    exception class (PyValueError → Integrity, PyConnectionError →
    Operational, PyRuntimeError → Programming/Database). It also
    prefixes the message with ``[SQLSTATE] `` when available, which we
    use to refine the mapping (e.g., 23505 → IntegrityError even if the
    Rust side misclassified it).
    """
    msg = str(exc)
    if sql is not None:
        # Short-form SQL snippet to disambiguate during development; keep
        # it bounded so debug logs stay manageable.
        snippet = sql if len(sql) < 400 else sql[:397] + "..."
        msg = f"{msg}\nSQL: {snippet}\nparams: {params!r}"
    m = re.match(r"\[([0-9A-Z]{5})\]\s*", msg)
    if m:
        sqlstate = m.group(1)
        cls_code = sqlstate[:2]
        if cls_code in _SQLSTATE_MAP:
            cls = _SQLSTATE_MAP[cls_code]
            return cls(msg)
    if isinstance(exc, ValueError):
        return IntegrityError(msg)
    if isinstance(exc, ConnectionError):
        return OperationalError(msg)
    if isinstance(exc, RuntimeError):
        return DatabaseError(msg)
    return DatabaseError(msg)


# ── Parameter-style conversion ──────────────────────────────────────────
# Django's ORM emits %s placeholders. tokio-postgres wants $1..$N. We
# convert while respecting string literals, identifiers, comments, and
# dollar-quoted bodies. Django also occasionally emits %(name)s; we
# support both forms but require the caller to pass either a sequence
# (for %s) or a mapping (for %(name)s), matching psycopg behavior.

_PCT_RE = re.compile(r"%%|%\(([^)]+)\)s|%s")

# A schema-qualified PG identifier: optional schema, dot, name. Each part
# is either an unquoted identifier (letters/digits/underscores, not
# starting with a digit) or a double-quoted identifier. Accepting either
# form lets us refuse anything that contains shell-style separators or
# arbitrary characters that would let a caller inject DML through
# ``callproc(name, ...)``.
_PG_IDENT_PART = r'(?:[A-Za-z_][A-Za-z0-9_]*|"(?:[^"]|"")+")'
_PG_QUALIFIED_IDENT_RE = re.compile(
    rf"\A{_PG_IDENT_PART}(?:\.{_PG_IDENT_PART})?\Z"
)


def _validate_callable_ident(procname: str) -> str:
    """Reject callproc target names that aren't a single (optionally
    schema-qualified) Postgres identifier. Without this, a caller passing
    user-controlled text would inject arbitrary SQL into the synthesized
    ``SELECT * FROM <procname>(...)``.

    Returns the validated text unchanged. Quoting is left to the caller —
    we want exact equality with what Django's existing callers pass
    (e.g., ``get_project_auth_info``), and our acceptance regex already
    bars anything outside identifier syntax."""
    if not isinstance(procname, str) or not _PG_QUALIFIED_IDENT_RE.match(procname):
        raise ProgrammingError(
            f"callproc: invalid procedure name {procname!r} — expected an "
            "unquoted or double-quoted PG identifier (optionally "
            "schema-qualified). Use cursor.execute() for dynamic names."
        )
    return procname


def convert_paramstyle(
    sql: str, params: Any
) -> tuple[str, list[Any]]:
    """Rewrite %s / %(name)s placeholders to $1..$N and flatten params.

    Pre-validated by the outer cursor: ``params`` is either None, a
    sequence, or a mapping. Returns (new_sql, params_list).
    """
    if not params:
        return sql.replace("%%", "%"), []

    is_mapping = isinstance(params, dict)
    flat: list[Any] = []
    counter = [0]
    # We only want to rewrite placeholders outside of:
    #   - single-quoted string literals: 'foo'
    #   - double-quoted identifiers:    "col"
    #   - dollar-quoted bodies:         $tag$...$tag$
    #   - line comments:                -- ...\n
    #   - block comments:               /* ... */
    # The ORM's SQL is well-formed so we do a single left-to-right pass
    # skipping those regions.
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        # line comment
        if c == "-" and i + 1 < n and sql[i + 1] == "-":
            end = sql.find("\n", i + 2)
            if end < 0:
                out.append(sql[i:])
                break
            out.append(sql[i : end + 1])
            i = end + 1
            continue
        # block comment
        if c == "/" and i + 1 < n and sql[i + 1] == "*":
            end = sql.find("*/", i + 2)
            if end < 0:
                raise ProgrammingError("unterminated block comment")
            out.append(sql[i : end + 2])
            i = end + 2
            continue
        # single-quoted string literal. PG's E'..' escape strings honour
        # backslash escapes (`\'` ends as a literal apostrophe, `\\` as a
        # backslash); plain '..' literals do not, even with
        # standard_conforming_strings=off. Detect the E-prefix by peeking
        # at the previous character — must be E/e and not part of a
        # broader identifier.
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
                    j += 2  # consume the next char as an escape
                    continue
                if ch == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2  # escaped quote
                        continue
                    j += 1
                    break
                j += 1
            out.append(sql[i:j])
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
            out.append(sql[i:j])
            i = j
            continue
        # dollar-quoted body: $[tag]$ ... $[tag]$
        if c == "$":
            tag_end = sql.find("$", i + 1)
            if tag_end > i:
                tag = sql[i : tag_end + 1]
                # tag must be all alphanumeric/underscore between the $'s
                if all(ch.isalnum() or ch == "_" for ch in tag[1:-1]):
                    end = sql.find(tag, tag_end + 1)
                    if end > tag_end:
                        out.append(sql[i : end + len(tag)])
                        i = end + len(tag)
                        continue
            # fall through — $ wasn't a dollar-quote opener
        # placeholder
        if c == "%":
            if i + 1 < n and sql[i + 1] == "%":
                out.append("%")
                i += 2
                continue
            if i + 1 < n and sql[i + 1] == "s":
                if is_mapping:
                    raise ProgrammingError(
                        "positional %s placeholder used with mapping params"
                    )
                counter[0] += 1
                out.append(f"${counter[0]}")
                # params is a sequence; we'll flatten at the end
                i += 2
                continue
            if i + 1 < n and sql[i + 1] == "(":
                close = sql.find(")", i + 2)
                if close < 0 or close + 1 >= n or sql[close + 1] != "s":
                    raise ProgrammingError(
                        f"malformed named placeholder at position {i}"
                    )
                name = sql[i + 2 : close]
                if not is_mapping:
                    raise ProgrammingError(
                        "named %(name)s placeholder used with sequence params"
                    )
                if name not in params:
                    raise ProgrammingError(f"missing parameter: {name!r}")
                counter[0] += 1
                out.append(f"${counter[0]}")
                flat.append(params[name])
                i = close + 2
                continue
        out.append(c)
        i += 1

    # psycopg's ``%s``-paramstyle contract: ``%%`` always means a literal
    # ``%``, even inside string literals — i.e. you cannot embed a
    # literal ``%%`` in the SQL when using this paramstyle. The
    # placeholder branch above already handles the outside-strings
    # case; mop up the inside-strings case here so behaviour is
    # uniform with psycopg (and so Django's escaping tests pass).
    joined = "".join(out).replace("%%", "%")
    if is_mapping:
        return joined, flat
    # positional: flatten straight from params preserving order
    params_list = list(params)
    if counter[0] != len(params_list):
        raise ProgrammingError(
            f"parameter count mismatch: SQL has {counter[0]} placeholders, "
            f"got {len(params_list)} values"
        )
    return joined, params_list


# ── Cursor ──────────────────────────────────────────────────────────────
class Cursor:
    """DB-API 2.0 cursor backed by a shared RustPgDriver.

    Inside a manual transaction (autocommit off), the connection is
    pinned at the ``Connection`` level via ``driver.begin()`` and every
    cursor on the connection dispatches against that pinned connection.

    In autocommit mode, each cursor lazily pins its own pool connection
    on first ``execute`` (via ``driver.pin()``) and holds it for the
    cursor's lifetime, so multi-statement patterns within one cursor
    (TEMP tables, advisory locks, ``SET LOCAL``, ``LOCK TABLE`` + the
    follow-up ``SELECT``) see the same backend session — psycopg3-like
    semantics within a cursor. The pin is released on ``close``. Note
    that two different cursors on the same Connection in autocommit
    mode get DIFFERENT backends (psycopg3 shares one across cursors);
    this is a deliberate tradeoff to keep pool dynamics tractable
    under high concurrency.
    """

    arraysize = 1
    # psycopg-compat stubs Django's postgresql backend reads through
    # without us having to fork those modules. ``_query`` holds the
    # last-executed SQL text on psycopg3; returning None makes Django's
    # last_executed_query() fall through to its default formatter.
    _query = None
    statusmessage: str | None = None

    def __init__(self, connection: "Connection") -> None:
        self.connection = connection
        self._rows: list[tuple] | None = None
        self._row_iter_idx = 0
        self._description: list[tuple] | None = None
        self._rowcount = -1
        self._closed = False
        # Lazily acquired in autocommit mode on first execute. None
        # means "use connection-level routing (per-stmt pool checkout
        # in autocommit, _tx in transaction)".
        self._pin = None
        # psycopg-compat: ``cursor.query`` is the SQL of the last
        # executed statement with parameters inlined (bytes), or
        # ``None`` if no query has run yet. Django's CursorDebugWrapper,
        # debug-toolbar, and the ``last_executed_query`` operations
        # hook all read this attribute.
        self.query: bytes | None = None

    # DB-API required attributes
    @property
    def description(self) -> list[tuple] | None:
        return self._description

    @property
    def rowcount(self) -> int:
        return self._rowcount

    @property
    def closed(self) -> bool:
        # psycopg-style. Django reaches for this in test_utils to verify
        # context-manager cleanup.
        return self._closed

    def close(self) -> None:
        # Release the autocommit-mode pin before tearing down state so
        # the pooled connection goes back to deadpool even if a later
        # ``self._closed = True`` raises (it shouldn't, but be safe).
        if self._pin is not None:
            try:
                self._pin.release_sync()
            except Exception:
                pass
            self._pin = None
        self._rows = None
        self._description = None
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    def _check(self) -> None:
        if self._closed:
            raise InterfaceError("cursor is closed")
        if self.connection._closed:
            raise InterfaceError("connection is closed")

    def _record_query(self, sql_str: str, params: Any) -> None:
        """Set ``self.query`` (bytes) to the SQL with params inlined.
        psycopg-compat — Django's CursorDebugWrapper / debug-toolbar /
        ``last_executed_query`` operations hook all read this attribute.
        Best-effort: any exception during inlining leaves ``query`` set
        to the raw SQL so debug paths still see something useful."""
        from gt_rust.django_backend.schema import _inline_params

        try:
            inlined = _inline_params(sql_str, params)
        except Exception:
            inlined = sql_str
        self.query = inlined.encode("utf-8", errors="replace")

    def execute(self, sql: Any, params: Any | None = None) -> "Cursor":
        self._check()
        sql_str = _as_sql_str(sql)
        new_sql, flat = convert_paramstyle(sql_str, params)
        self._record_query(sql_str, params)
        try:
            result = self.connection._execute_sync(new_sql, flat, cursor=self)
        except Exception as e:
            # Include SQL in the error to make mismatches easy to debug.
            raise _translate_rust_error(e, sql=new_sql, params=flat) from e
        self._apply_result(result)
        return self

    def executemany(self, sql: Any, params_seq: Iterable[Any]) -> "Cursor":
        # PEP 249 says ``executemany`` does not have to provide row data;
        # we drop any rows from the last iteration so callers can't
        # accidentally fetch from a stale buffer. ``description`` and the
        # iterator index are reset for the same reason.
        self._check()
        sql_str = _as_sql_str(sql)
        total = 0
        last_params: Any = None
        for params in params_seq:
            new_sql, flat = convert_paramstyle(sql_str, params)
            try:
                result = self.connection._execute_sync(new_sql, flat, cursor=self)
            except Exception as e:
                raise _translate_rust_error(e, sql=new_sql, params=flat) from e
            self._apply_result(result)
            if self._rowcount >= 0:
                total += self._rowcount
            last_params = params
        self._record_query(sql_str, last_params)
        self._rows = None
        self._row_iter_idx = 0
        self._description = None
        self._rowcount = total
        return self

    def _apply_result(self, result: Any) -> None:
        # RustPgDriver.query_sync returns (rows, description_tuples) for
        # queries and an int for execute. We call the query_sync path
        # always (via _execute_sync) so we can consistently extract
        # columns.
        if isinstance(result, tuple) and len(result) == 2:
            rows, cols = result
            self._rows = list(rows)
            self._row_iter_idx = 0
            self._rowcount = len(self._rows)
            # psycopg-style description: 7-tuple (name, type_code, ...)
            self._description = [
                (c[0], c[1], None, None, None, None, None) for c in cols
            ]
        elif isinstance(result, int):
            self._rows = None
            self._row_iter_idx = 0
            self._rowcount = result
            self._description = None
        elif result is None:
            self._rows = None
            self._rowcount = -1
            self._description = None
        else:
            raise InterfaceError(f"unexpected result shape: {type(result)!r}")

    def fetchone(self) -> tuple | None:
        self._check()
        if self._rows is None:
            return None
        if self._row_iter_idx >= len(self._rows):
            return None
        row = self._rows[self._row_iter_idx]
        self._row_iter_idx += 1
        return row

    def fetchmany(self, size: int | None = None) -> list[tuple]:
        self._check()
        if self._rows is None:
            return []
        n = size if size is not None else self.arraysize
        end = min(self._row_iter_idx + n, len(self._rows))
        chunk = self._rows[self._row_iter_idx : end]
        self._row_iter_idx = end
        return chunk

    def fetchall(self) -> list[tuple]:
        self._check()
        if self._rows is None:
            return []
        chunk = self._rows[self._row_iter_idx :]
        self._row_iter_idx = len(self._rows)
        return chunk

    def setinputsizes(self, sizes) -> None:  # noqa: D401 — DB-API no-op
        pass

    def setoutputsize(self, size, column=None) -> None:  # noqa: D401
        pass

    def copy(self, sql: Any) -> "_CopyOut":
        """psycopg3-style ``cursor.copy()`` context manager.

        Only supports ``COPY ... TO STDOUT`` for now — the direction used
        by GlitchTip's cold-storage archive. Returns an iterable of raw
        byte chunks split on newlines so callers can buffer rows.
        """
        self._check()
        sql_str = _as_sql_str(sql)
        driver = self.connection._driver
        if driver is None:
            raise InterfaceError("connection is closed")
        try:
            data = driver.copy_out_sync(sql_str)
        except Exception as e:
            raise _translate_rust_error(e, sql=sql_str) from e
        return _CopyOut(data)

    def mogrify(self, sql: Any, params: Any | None = None) -> str:
        """psycopg3-style param inlining. Returns str (psycopg3 ClientCursor
        semantics; psycopg2 returned bytes but GT code paths rely on the
        str form for ``",".join(cursor.mogrify(...) for ...)``).
        """
        self._check()
        sql_str = _as_sql_str(sql)
        from gt_rust.django_backend.schema import _inline_params

        return _inline_params(sql_str, params)

    def callproc(self, procname: str, params: Any | None = None) -> Any:
        """Call a stored procedure/function via ``SELECT * FROM proc(...)``.

        Postgres functions are regular queries — no dedicated wire call
        like DB2/Oracle — so we synthesize the SELECT here. Matches how
        psycopg2 implemented callproc before psycopg3 dropped it.
        ``procname`` is validated against PG identifier syntax to refuse
        injection through user-controlled text.
        """
        self._check()
        name = _validate_callable_ident(procname)
        if params is None:
            placeholders = ""
            flat: list[Any] = []
        else:
            placeholders = ",".join(["%s"] * len(params))
            flat = list(params)
        sql = f"SELECT * FROM {name}({placeholders})"
        self.execute(sql, flat)
        return params


class _CopyOut:
    """Iterator over newline-delimited byte chunks from ``COPY TO STDOUT``.

    psycopg3 returns a context-manager that yields ``bytes`` for each
    row line. We buffer the whole body first (the caller's usage in
    GlitchTip's archive paths is size-bounded per call) and split on
    ``\\n`` so downstream CSV processing sees one line per iteration.
    The closing newline is preserved on each line to match psycopg3.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        if self._pos >= len(self._data):
            raise StopIteration
        end = self._data.find(b"\n", self._pos)
        if end == -1:
            line = self._data[self._pos :]
            self._pos = len(self._data)
            return line
        line = self._data[self._pos : end + 1]
        self._pos = end + 1
        return line


def _as_sql_str(sql: Any) -> str:
    """Accept str, bytes, or psycopg.sql.Composable; return str.

    Django's postgresql ops builds some queries with ``psycopg.sql`` for
    safe identifier quoting. If psycopg is installed we lean on its
    ``as_string`` method; otherwise we ``str()`` it as a fallback.
    """
    if isinstance(sql, str):
        return sql
    if isinstance(sql, (bytes, bytearray)):
        return bytes(sql).decode()
    # psycopg.sql.Composed / SQL / Identifier — expose as_string(conn).
    # We pass None since the Composable forms we get from Django don't
    # need a live connection to render (Identifier quoting is static).
    as_string = getattr(sql, "as_string", None)
    if callable(as_string):
        try:
            return as_string(None)
        except Exception:
            pass
    return str(sql)


# ── Connection info / adapters stubs ────────────────────────────────────
class _ConnectionInfo:
    """Subset of psycopg.Connection.info that Django's backend reads."""

    def __init__(self, conn: "Connection") -> None:
        self._conn = conn

    def parameter_status(self, name: str) -> str | None:
        # Django calls this with "TimeZone" to decide whether to issue
        # SET TIME ZONE. Round-trip via SHOW.
        try:
            cur = self._conn.cursor()
            try:
                cur.execute(f"SHOW {name}")
                row = cur.fetchone()
                return row[0] if row else None
            finally:
                cur.close()
        except Error:
            return None

    @property
    def server_version(self) -> int:
        if self._conn._server_version is None:
            cur = self._conn.cursor()
            try:
                cur.execute("SHOW server_version_num")
                row = cur.fetchone()
                self._conn._server_version = int(row[0]) if row else 0
            finally:
                cur.close()
        return self._conn._server_version


class _AdaptersStub:
    """Minimal adapters map. Django reads get_loader(oid, Format.TEXT)
    during create_cursor to register a timezone loader; we return a
    permissive no-op loader so that code path is a no-op for us.
    """

    class _DummyLoader:
        timezone = None

    def get_loader(self, oid, format):  # noqa: ARG002
        return self._DummyLoader()

    def register_loader(self, *args, **kwargs) -> None:
        pass

    def register_dumper(self, *args, **kwargs) -> None:
        pass


# ── Server-side cursor ──────────────────────────────────────────────────
class ServerSideCursor(Cursor):
    """``DECLARE`` / ``FETCH`` / ``CLOSE`` over a real PostgreSQL named
    cursor. Backs Django's ``QuerySet.iterator()`` chunked-cursor path.

    The cursor pins a single connection for its lifetime so DECLARE,
    every FETCH, and the final CLOSE all hit the same backend session.
    Inside a Django ``atomic()`` block the cursor cooperates with the
    surrounding transaction — DECLARE runs without ``WITH HOLD`` and the
    cursor is implicitly closed on commit/rollback. In autocommit mode
    we wrap the DECLARE in our own ``BEGIN; ... WITH HOLD; COMMIT;``
    micro-transaction so the cursor outlives the implicit tx.

    Parameters in the ``execute(sql, params)`` SQL are inlined client-
    side (mogrify-style) before DECLARE: PG's extended-query protocol
    does not bind parameters into a cursor's stored plan, so we burn
    the values into the SQL text the way psycopg's ``ClientCursorMixin``
    does for the same reason.
    """

    def __init__(self, connection: "Connection", *, name: str, withhold: bool):
        super().__init__(connection)
        self._name = name
        self._withhold = withhold
        self._declared = False
        # In transaction mode we don't own a pin — we ride
        # ``connection._tx``'s pin. ``_pin`` stays None and ``close()``
        # leaves _tx alone. In autocommit mode we acquire our own pin
        # in ``execute()`` and own the BEGIN/COMMIT around DECLARE.
        self._owns_micro_tx = False

    @property
    def name(self) -> str:
        return self._name

    def _quote_ident(self) -> str:
        # PG cursor names are identifiers — quote any embedded `"`.
        escaped = self._name.replace('"', '""')
        return f'"{escaped}"'

    def execute(self, sql: Any, params: Any | None = None) -> "ServerSideCursor":
        from gt_rust.django_backend.schema import _inline_params

        self._check()
        if self._declared:
            raise InterfaceError(
                "ServerSideCursor.execute() may only be called once; "
                "named cursors cannot be re-declared."
            )

        sql_str = _as_sql_str(sql)
        # Mogrify params into the SQL — DECLARE CURSOR's body is a
        # plain SELECT statement that gets stored verbatim by PG.
        # Parameters cannot ride the bind protocol into the stored plan.
        new_sql, flat = convert_paramstyle(sql_str, params)
        inlined = _inline_params(new_sql, flat) if flat else new_sql

        hold_clause = " WITH HOLD" if self._withhold else ""
        declare = f"DECLARE {self._quote_ident()} NO SCROLL CURSOR{hold_clause} FOR {inlined}"

        try:
            if self.connection._autocommit:
                # No surrounding tx; run our own micro-tx so DECLARE has
                # something to live inside. WITH HOLD makes the cursor
                # outlive the COMMIT (within the same backend session).
                if self._pin is None:
                    self._pin = self.connection._driver.pin()
                self._pin.execute_sync("BEGIN", [])
                self._pin.execute_sync(declare, [])
                self._pin.execute_sync("COMMIT", [])
                self._owns_micro_tx = True
            else:
                if self.connection._tx is None:
                    self.connection._tx = self.connection._driver.begin()
                self.connection._tx.execute_sync(declare, [])
        except Exception as e:
            raise _translate_rust_error(e, sql=declare) from e

        self._declared = True
        # description / rowcount stay unset until the first fetch — PG
        # only sends the RowDescription in response to FETCH. Django's
        # iterator path doesn't read description before fetching.
        return self

    def _runner(self):
        # In autocommit + WITH HOLD the cursor lives on our pin.
        # Otherwise it lives on the connection's transaction.
        return self._pin if self._owns_micro_tx else self.connection._tx

    def _fetch(self, count: str) -> list[tuple]:
        if not self._declared:
            return []
        runner = self._runner()
        if runner is None:
            raise InterfaceError(
                "named cursor's session is gone "
                "(transaction was committed or connection released)"
            )
        sql = f"FETCH FORWARD {count} FROM {self._quote_ident()}"
        try:
            result = runner.query_sync(sql, [])
        except Exception as e:
            raise _translate_rust_error(e, sql=sql) from e
        rows, cols = result
        # Update description / rowcount on the first fetch the same way
        # ``Cursor._apply_result`` does for unnamed cursors.
        if self._description is None:
            self._description = [
                (c[0], c[1], None, None, None, None, None) for c in cols
            ]
        rows_list = list(rows)
        # rowcount semantics: report the running total of rows produced
        # by this cursor across all FETCHes. Matches psycopg.
        if self._rowcount < 0:
            self._rowcount = 0
        self._rowcount += len(rows_list)
        return rows_list

    def fetchone(self) -> tuple | None:
        rows = self._fetch("1")
        return rows[0] if rows else None

    def fetchmany(self, size: int | None = None) -> list[tuple]:
        n = size if size is not None else self.arraysize
        return self._fetch(str(int(n)))

    def fetchall(self) -> list[tuple]:
        return self._fetch("ALL")

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    def close(self) -> None:
        if self._closed:
            return
        if self._declared:
            close_sql = f"CLOSE {self._quote_ident()}"
            runner = self._runner()
            # Best-effort: if the underlying transaction is already gone
            # (rolled back, connection closed) the CLOSE will fail and
            # the cursor was implicitly cleaned up anyway.
            if runner is not None:
                try:
                    runner.execute_sync(close_sql, [])
                except Exception:
                    pass
            self._declared = False
        if self._owns_micro_tx and self._pin is not None:
            try:
                self._pin.release_sync()
            except Exception:
                pass
            self._pin = None
            self._owns_micro_tx = False
        # Fall through to base teardown (clears state, marks closed).
        super().close()


# ── Connection ──────────────────────────────────────────────────────────
class Connection:
    """psycopg-compatible Connection wrapping RustPgDriver.

    One Connection owns one driver (pool). Two ways a pool connection
    gets pinned:

    1. Manual transaction (``autocommit=False`` or
       ``@transaction.atomic``): ``_execute_sync`` lazily calls
       ``driver.begin()`` and stores the result in ``self._tx``. Every
       cursor on this Connection then routes through that pinned
       backend until ``commit()`` / ``rollback()``.
    2. Autocommit mode: each ``Cursor`` lazily acquires its own pin via
       ``driver.pin()`` on first ``execute``, held for the cursor's
       lifetime. Multi-statement patterns within one cursor (TEMP
       tables, advisory locks, ``SET LOCAL``, ``LOCK TABLE`` + the
       follow-up ``SELECT``) see a single backend session. Released
       on ``cursor.close()``.

    Divergence from psycopg3 — psycopg holds one backend per
    Connection across all cursors. We pin per-cursor in autocommit
    instead, so two simultaneous cursors on the same Connection see
    different backends. Deliberate tradeoff: a Connection-level pin
    would force ``pool_size >= max concurrent open Connections``
    (≈ request concurrency), making pool sizing painful at scale.
    Per-cursor pinning fixes the patterns that actually appear in
    practice without that constraint.
    """

    def __init__(self, driver: RustPgDriver, *, alias: str = "default") -> None:
        self._driver = driver
        self._autocommit = True
        self._closed = False
        self._tx = None  # RustTransaction when in a manual transaction
        self._server_version: int | None = None
        self.info = _ConnectionInfo(self)
        self.adapters = _AdaptersStub()
        self.isolation_level = None
        self.alias = alias

    # psycopg-style entry points Django calls:
    def cursor(self, name=None, scrollable=None, withhold=False, **kwargs) -> Cursor:  # noqa: ARG002
        if name is not None:
            return ServerSideCursor(self, name=name, withhold=bool(withhold))
        return Cursor(self)

    @property
    def autocommit(self) -> bool:
        return self._autocommit

    @autocommit.setter
    def autocommit(self, value: bool) -> None:
        if value == self._autocommit:
            return
        if self._tx is not None:
            # Django sets autocommit after BEGIN in some paths; finish
            # the pending tx in the requested direction.
            if value:
                self.commit()
            else:
                self.rollback()
        self._autocommit = bool(value)

    def commit(self) -> None:
        if self._tx is None:
            return
        try:
            self._tx.commit_sync()
        except Exception as e:
            raise _translate_rust_error(e) from e
        finally:
            self._tx = None

    def rollback(self) -> None:
        if self._tx is None:
            return
        try:
            self._tx.rollback_sync()
        except Exception as e:
            raise _translate_rust_error(e) from e
        finally:
            self._tx = None

    def close(self) -> None:
        """Mark this Connection closed — the underlying Rust pool is
        process-shared and stays alive.

        Django reuses the same ``DatabaseWrapper`` per alias across
        threads and calls ``close()`` when a thread finishes or when
        ``CONN_MAX_AGE`` expires. Tearing down the Rust pool here would
        force every subsequent request to rebuild it (and re-do the
        eager connection warmup), which is what made the pre-shared
        version drown under concurrent load. Only the test-runner path
        calls ``close_pool()`` on the DatabaseWrapper, which eventually
        drains the pool via a dedicated Rust call.
        """
        if self._closed:
            return
        if self._tx is not None:
            try:
                self._tx.rollback_sync()
            except Exception:
                pass
            self._tx = None
        # Leave ``self._driver`` in place — further execute() calls will
        # raise ``InterfaceError`` because ``_closed`` is set. The Rust
        # pool keeps its connections open for other threads.
        self._closed = True

    def drop_shared_driver(self) -> None:
        """Evict the shared Rust driver from the process cache and
        drain its pool. Only the Django test-runner close_pool() path
        should call this — it's required for ``DROP DATABASE`` to
        succeed."""
        driver = self._driver
        if driver is None:
            return
        try:
            driver.close_sync()
        except Exception:
            pass
        _evict_driver_from_cache(driver)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            try:
                self.commit()
            except Error:
                pass
        else:
            try:
                self.rollback()
            except Error:
                pass
        self.close()

    # Internal: called by Cursor.execute. Routes the statement through
    # the right pinned object — connection-level _tx in a manual
    # transaction, cursor-level pin in autocommit. Falls back to a
    # per-statement pool checkout when no cursor is provided (legacy
    # callers / batch paths) and we're in autocommit.
    def _execute_sync(
        self, sql: str, params: list[Any], *, cursor: "Cursor | None" = None
    ) -> Any:
        if self._driver is None:
            raise InterfaceError("connection is closed")
        # INSERT/UPDATE/DELETE without RETURNING produces no rows and we
        # need the affected-row count for Django's save() machinery
        # (which raises NotUpdated when rowcount < 1). tokio-postgres's
        # query() path returns Vec<Row> and discards the CommandComplete
        # tag. Route those statements through execute_sync which surfaces
        # the tag as an int.
        dml_no_returning = _looks_like_dml_without_returning(sql)
        if not self._autocommit:
            if self._tx is None:
                self._tx = self._driver.begin()
            if dml_no_returning:
                return self._tx.execute_sync(sql, params)
            return self._tx.query_sync(sql, params)
        # Autocommit. If a Cursor was passed, pin a connection for its
        # lifetime; otherwise per-stmt pool checkout (back-compat).
        if cursor is not None:
            if cursor._pin is None:
                cursor._pin = self._driver.pin()
            if dml_no_returning:
                return cursor._pin.execute_sync(sql, params)
            return cursor._pin.query_sync(sql, params)
        if dml_no_returning:
            return self._driver.execute_sync(sql, params)
        return self._driver.query_sync(sql, params)

    def _batch_execute_sync(self, sql: str) -> None:
        """Run one-or-more statements separated by ``;`` with no params.

        Routes to tokio-postgres's simple_query protocol, which accepts
        multi-statement bodies (unlike the extended protocol used by
        query_sync). Schema editor DDL goes through this path because
        Django regularly emits "SET CONSTRAINTS ...; ALTER TABLE ...".
        """
        if self._driver is None:
            raise InterfaceError("connection is closed")
        try:
            if self._autocommit or self._tx is None:
                self._driver.batch_execute_sync(sql)
            else:
                self._tx.batch_execute_sync(sql)
        except Exception as e:
            raise _translate_rust_error(e, sql=sql) from e


_DML_LEADING = re.compile(r"^\s*(?:--[^\n]*\n|/\*.*?\*/|\s)*", re.DOTALL)
# Top-level DML keyword detection. ``MERGE`` (PG 15+) is included.
# CTE-prefixed DML (``WITH ... UPDATE foo``) is intentionally NOT matched:
# distinguishing it from CTE-prefixed SELECT-with-DML-inside (``WITH x AS
# (DELETE ... RETURNING *) SELECT * FROM x``) would require paren-aware
# parsing. The unprefixed DML form is what Django's ORM emits by default;
# CTE-prefixed forms route through query_sync and Django reads the returned
# rowcount via the wire RowDescription instead. Document if anyone hits
# this — fix is to always read the command tag from the wire (see Rust).
_DML_STMT_RE = re.compile(r"^(INSERT|UPDATE|DELETE|MERGE)\b", re.IGNORECASE)
_RETURNING_RE = re.compile(r"\bRETURNING\b", re.IGNORECASE)


def _looks_like_dml_without_returning(sql: str) -> bool:
    # Strip leading comments/whitespace, then check the leading keyword.
    m = _DML_LEADING.match(sql)
    prefix_end = m.end() if m else 0
    rest = sql[prefix_end:]
    if not _DML_STMT_RE.match(rest):
        return False
    return _RETURNING_RE.search(rest) is None


# Process-wide cache of RustPgDriver instances keyed by (alias,
# connection-params). Django creates a new ``Connection`` for every
# thread (and often per request if CONN_MAX_AGE=0), but the Rust pool
# itself is thread-safe and expensive to build — each pool eagerly
# warms N connections. Sharing one driver per alias across all threads
# is the only way the gt_rust backend behaves sensibly under realistic
# multi-worker ASGI load; otherwise each thread hammers Postgres with
# fresh TCP/TLS handshakes and ``max_connections`` saturates.
_driver_cache: dict[tuple, RustPgDriver] = {}
_driver_cache_lock = _threading.Lock()


def _driver_key(
    *,
    host: str,
    port: int,
    dbname: str,
    user: str,
    password: str,
    pool_size: int,
    sslmode: str,
    ca_cert_path: str | None,
    prepared_statements: bool,
    server_settings: dict[str, str] | None,
    application_name: str | None,
    client_cert_path: str | None,
    client_key_path: str | None,
    connect_timeout: float | None,
    keepalives: bool | None,
    keepalives_idle: float | None,
    pool_min_size: int | None,
    pool_wait_timeout: float | None,
    pool_max_lifetime: float | None,
    pool_max_idle: float | None,
    alias: str,
) -> tuple:
    # Every connection-shaping field belongs in the key. Two aliases that
    # only differ in OPTIONS["server_settings"] (e.g., a maintenance alias
    # with a longer statement_timeout) must NOT share a driver. Same for
    # rotated passwords, swapped TLS roots, etc.
    server_settings_key = (
        tuple(sorted(server_settings.items())) if server_settings else None
    )
    return (
        alias,
        host,
        port,
        dbname,
        user,
        password,
        pool_size,
        sslmode,
        ca_cert_path,
        prepared_statements,
        server_settings_key,
        application_name,
        client_cert_path,
        client_key_path,
        connect_timeout,
        keepalives,
        keepalives_idle,
        pool_min_size,
        pool_wait_timeout,
        pool_max_lifetime,
        pool_max_idle,
    )


def _shared_driver(
    *,
    host: str,
    port: int,
    dbname: str,
    user: str,
    password: str,
    pool_size: int,
    sslmode: str,
    ca_cert_path: str | None,
    prepared_statements: bool,
    server_settings: dict[str, str] | None,
    application_name: str | None = None,
    client_cert_path: str | None = None,
    client_key_path: str | None = None,
    connect_timeout: float | None = None,
    keepalives: bool | None = None,
    keepalives_idle: float | None = None,
    pool_min_size: int | None = None,
    pool_wait_timeout: float | None = None,
    pool_max_lifetime: float | None = None,
    pool_max_idle: float | None = None,
    alias: str,
) -> RustPgDriver:
    key = _driver_key(
        host=host,
        port=port,
        dbname=dbname,
        user=user,
        password=password,
        pool_size=pool_size,
        sslmode=sslmode,
        ca_cert_path=ca_cert_path,
        prepared_statements=prepared_statements,
        server_settings=server_settings,
        application_name=application_name,
        client_cert_path=client_cert_path,
        client_key_path=client_key_path,
        connect_timeout=connect_timeout,
        keepalives=keepalives,
        keepalives_idle=keepalives_idle,
        pool_min_size=pool_min_size,
        pool_wait_timeout=pool_wait_timeout,
        pool_max_lifetime=pool_max_lifetime,
        pool_max_idle=pool_max_idle,
        alias=alias,
    )
    with _driver_cache_lock:
        drv = _driver_cache.get(key)
        if drv is not None:
            return drv
        drv = RustPgDriver.connect(
            host=host,
            port=int(port),
            dbname=dbname,
            user=user,
            password=password or "",
            pool_size=pool_size,
            sslmode=sslmode,
            ca_cert_path=ca_cert_path,
            prepared_statements=prepared_statements,
            server_settings=server_settings,
            application_name=application_name,
            client_cert_path=client_cert_path,
            client_key_path=client_key_path,
            connect_timeout=connect_timeout,
            keepalives=keepalives,
            keepalives_idle=keepalives_idle,
            pool_min_size=pool_min_size,
            pool_wait_timeout=pool_wait_timeout,
            pool_max_lifetime=pool_max_lifetime,
            pool_max_idle=pool_max_idle,
        )
        _driver_cache[key] = drv
        return drv


def _evict_driver_from_cache(driver: RustPgDriver) -> None:
    """Drop the driver from the process-wide cache. Used by the Django
    test-runner tear-down path so ``DROP DATABASE`` can succeed."""
    with _driver_cache_lock:
        dead = [k for k, v in _driver_cache.items() if v is driver]
        for k in dead:
            del _driver_cache[k]


def close_drivers_for_alias(alias: str) -> None:
    """Drain and evict every cached driver matching the given Django
    ``DATABASES`` alias. Called from ``DatabaseWrapper.close_pool``
    when the wrapper's own connection has already been released — the
    process-wide cache still holds idle PG sessions that block
    ``DROP DATABASE`` on the test runner's teardown.

    Targets only this alias so other live wrappers (e.g., a
    ``maintenance`` connection on a different DB) keep their pools."""
    with _driver_cache_lock:
        keys = [k for k in _driver_cache if k and k[0] == alias]
        drivers = [_driver_cache.pop(k) for k in keys]
    for drv in drivers:
        try:
            drv.close_sync()
        except Exception:
            pass


def connect(
    *,
    host: str,
    port: int = 5432,
    dbname: str,
    user: str,
    password: str = "",
    pool_size: int = 10,
    sslmode: str = "prefer",
    ca_cert_path: str | None = None,
    prepared_statements: bool = True,
    server_settings: dict[str, str] | None = None,
    application_name: str | None = None,
    client_cert_path: str | None = None,
    client_key_path: str | None = None,
    connect_timeout: float | None = None,
    keepalives: bool | None = None,
    keepalives_idle: float | None = None,
    pool_min_size: int | None = None,
    pool_wait_timeout: float | None = None,
    pool_max_lifetime: float | None = None,
    pool_max_idle: float | None = None,
    autocommit: bool = True,
    alias: str = "default",
    **ignored,
) -> Connection:
    """Open a Connection backed by a shared (per-process) RustPgDriver.

    Extra kwargs from Django's get_connection_params (isolation_level,
    pool config, etc.) are accepted and ignored; wire them in as we hit
    test failures that need them.
    """
    driver = _shared_driver(
        host=host,
        port=int(port),
        dbname=dbname,
        user=user,
        password=password,
        pool_size=pool_size,
        sslmode=sslmode,
        ca_cert_path=ca_cert_path,
        prepared_statements=prepared_statements,
        server_settings=server_settings,
        application_name=application_name,
        client_cert_path=client_cert_path,
        client_key_path=client_key_path,
        connect_timeout=connect_timeout,
        keepalives=keepalives,
        keepalives_idle=keepalives_idle,
        pool_min_size=pool_min_size,
        pool_wait_timeout=pool_wait_timeout,
        pool_max_lifetime=pool_max_lifetime,
        pool_max_idle=pool_max_idle,
        alias=alias,
    )
    conn = Connection(driver, alias=alias)
    conn._autocommit = bool(autocommit)
    return conn

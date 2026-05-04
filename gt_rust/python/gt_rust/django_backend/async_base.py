"""Async DatabaseWrapper for django-async-backend, backed by gt_rust.

Plugs into ``django_async_backend.db.async_connections``: when code
does ``async_connections[alias].cursor()``, it gets a native async
cursor whose ``.execute()`` awaits directly on the Rust driver's
async methods (which yield to the event loop during DB I/O).

The sync ``gt_rust.django_backend.DatabaseWrapper`` stays in place for
Django's synchronous paths (migrations, tests, the stock ORM when no
one calls the async ORM). Both wrappers share the same underlying
``RustPgDriver`` via the process-wide driver cache in
``gt_rust.dbapi._shared_driver``, so there is exactly one pool per
process per alias regardless of how many Django-side wrappers live.

Requires ``django-async-backend`` to be installed. gt_rust depends on
it — there is no standalone-sync story.
"""

from __future__ import annotations

from typing import Any

from django.db.backends.postgresql.base import (
    DatabaseWrapper as _PgSyncDatabaseWrapper,
)
from django.db.backends.postgresql.features import (
    DatabaseFeatures as _PgFeatures,
)
from django_async_backend.db.backends.base.base import (
    BaseAsyncDatabaseWrapper,
)
from django_async_backend.db.backends.postgresql.async_base import (
    AsyncDatabaseOperations as _PgAsyncDatabaseOperations,
)

from gt_rust import dbapi as _dbapi
from gt_rust._rust import RustPgDriver  # noqa: F401  (re-exported via Database)


class AsyncDatabaseOperations(_PgAsyncDatabaseOperations):
    """gt_rust-aware async ops.

    The upstream ``compose_sql`` reaches into psycopg3 internals
    (``async_mogrify`` constructs a psycopg ``AsyncCursor`` from
    ``cursor.connection``). Our cursors don't have a psycopg connection,
    so we route ``compose_sql`` through our own param-inliner — same
    one ``GtRustAsyncCursor.mogrify`` already uses.
    """

    async def compose_sql(self, sql, params):
        from gt_rust.django_backend.schema import _inline_params

        return _inline_params(_dbapi._as_sql_str(sql), params)


class GtRustAsyncConnection:
    """Lightweight async-side "connection" wrapper.

    gt_rust's pool lives in Rust and is shared across the process —
    we don't open a per-request PG socket. What Django's async
    machinery calls "the connection" is this small Python object that
    (a) holds a reference to the shared driver and (b) optionally
    holds a ``RustTransaction`` when a transaction is in flight.

    ``close()`` is a no-op — the shared driver stays alive across
    requests. Only the test-runner's ``close_pool`` path actually
    drains the pool (via ``DatabaseWrapper.close_pool`` on the sync
    side).
    """

    def __init__(self, driver) -> None:
        self.driver = driver
        self.tx = None  # set to a RustTransaction while in a transaction
        self.autocommit = True
        self.closed = False

    async def commit(self) -> None:
        if self.tx is not None:
            try:
                await self.tx.commit()
            finally:
                self.tx = None

    async def rollback(self) -> None:
        if self.tx is not None:
            try:
                await self.tx.rollback()
            finally:
                self.tx = None

    async def close(self) -> None:
        if self.tx is not None:
            try:
                await self.tx.rollback()
            except Exception:
                # Best-effort cleanup — connection may already be torn
                # down. Cancel/SystemExit/KeyboardInterrupt still
                # propagate so the caller's stack unwind isn't masked.
                pass
            self.tx = None
        self.closed = True


class GtRustAsyncCursor:
    """Async cursor backed by ``RustPgDriver.query`` / ``.execute``.

    Behaves like a DB-API 2.0 cursor with async method equivalents.
    Every ``await cursor.execute(sql, params)`` either checks out a
    connection from the Rust pool (autocommit mode) or dispatches the
    statement to the currently-pinned ``RustTransaction``.
    """

    arraysize = 1
    # psycopg-compat stubs Django's postgresql backend reads through.
    _query = None
    statusmessage: str | None = None

    def __init__(self, conn: GtRustAsyncConnection) -> None:
        self._conn = conn
        self._rows: list[tuple] | None = None
        self._row_iter_idx = 0
        self._description: list[tuple] | None = None
        self._rowcount = -1
        self._closed = False

    @property
    def description(self):
        return self._description

    @property
    def rowcount(self) -> int:
        return self._rowcount

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def close(self) -> None:
        self._rows = None
        self._description = None
        self._closed = True

    def _check(self) -> None:
        if self._closed:
            raise _dbapi.InterfaceError("cursor is closed")
        if self._conn.closed:
            raise _dbapi.InterfaceError("connection is closed")

    async def execute(self, sql: Any, params: Any | None = None):
        self._check()
        sql_str = _dbapi._as_sql_str(sql)
        new_sql, flat = _dbapi.convert_paramstyle(sql_str, params)
        dml = _dbapi._looks_like_dml_without_returning(new_sql)
        try:
            if self._conn.tx is not None:
                if dml:
                    result = await self._conn.tx.execute(new_sql, flat)
                else:
                    result = await self._conn.tx.query(new_sql, flat)
            elif self._conn.autocommit:
                if dml:
                    result = await self._conn.driver.execute(new_sql, flat)
                else:
                    result = await self._conn.driver.query(new_sql, flat)
            else:
                # Lazy-begin a transaction on first execute when
                # autocommit is off. Pins one pool connection for the
                # rest of this Django connection's lifetime.
                self._conn.tx = await _begin_tx_async(self._conn.driver)
                if dml:
                    result = await self._conn.tx.execute(new_sql, flat)
                else:
                    result = await self._conn.tx.query(new_sql, flat)
        except Exception as e:
            # NB: must NOT be ``except BaseException`` — that would
            # swallow ``asyncio.CancelledError`` (BaseException-derived
            # since 3.8), turning ``asyncio.timeout()`` /
            # ``task.cancel()`` into a generic empty ``DatabaseError``
            # and breaking cancellation propagation entirely.
            # KeyboardInterrupt / SystemExit also stay BaseException so
            # they fall through unchanged.
            raise _dbapi._translate_rust_error(
                e, sql=new_sql, params=flat
            ) from e
        self._apply_result(result)
        return self

    async def executemany(self, sql: Any, seq_of_params) -> "GtRustAsyncCursor":
        # Sequential awaits — keeps the same semantics as psycopg3's
        # default executemany. Batch optimisation (query_many) is a
        # follow-up. Reset row buffer / description / iterator so callers
        # can't fetch stale data from the last iteration.
        self._check()
        total = 0
        for params in seq_of_params:
            await self.execute(sql, params)
            if self._rowcount >= 0:
                total += self._rowcount
        self._rows = None
        self._row_iter_idx = 0
        self._description = None
        self._rowcount = total
        return self

    def _apply_result(self, result: Any) -> None:
        if isinstance(result, tuple) and len(result) == 2:
            rows, cols = result
            self._rows = list(rows)
            self._row_iter_idx = 0
            self._rowcount = len(self._rows)
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
            raise _dbapi.InterfaceError(
                f"unexpected result shape: {type(result)!r}"
            )

    async def fetchone(self) -> tuple | None:
        self._check()
        if self._rows is None:
            return None
        if self._row_iter_idx >= len(self._rows):
            return None
        row = self._rows[self._row_iter_idx]
        self._row_iter_idx += 1
        return row

    async def fetchmany(self, size: int | None = None) -> list[tuple]:
        self._check()
        if self._rows is None:
            return []
        n = size if size is not None else self.arraysize
        end = min(self._row_iter_idx + n, len(self._rows))
        chunk = self._rows[self._row_iter_idx : end]
        self._row_iter_idx = end
        return chunk

    async def fetchall(self) -> list[tuple]:
        self._check()
        if self._rows is None:
            return []
        chunk = self._rows[self._row_iter_idx :]
        self._row_iter_idx = len(self._rows)
        return chunk

    async def callproc(self, procname: str, params=None):
        """Emulate ``callproc`` via ``SELECT * FROM proc(...)`` (same
        as psycopg2 used to do). Needed because AsyncCursor/tokio-
        postgres have no direct ``callproc`` wire primitive.
        ``procname`` is validated as a PG identifier to refuse injection
        through user-controlled text."""
        self._check()
        name = _dbapi._validate_callable_ident(procname)
        if params is None:
            placeholders = ""
            flat: list[Any] = []
        else:
            placeholders = ",".join(["%s"] * len(params))
            flat = list(params)
        sql = f"SELECT * FROM {name}({placeholders})"
        await self.execute(sql, flat)
        return params

    def mogrify(self, sql: Any, params: Any | None = None) -> str:
        """psycopg3 ``ClientCursor.mogrify`` equivalent — inlines
        params into the SQL text. Used by hot-path raw-SQL builders
        (see ``apps.event_ingest._async_db``)."""
        from gt_rust.django_backend.schema import _inline_params

        return _inline_params(_dbapi._as_sql_str(sql), params)


async def _begin_tx_async(driver):
    """Start a transaction on the shared driver asynchronously.

    ``driver.begin()`` is sync-only today — it blocks the current
    thread while running BEGIN on a pooled connection. Wrap it in
    ``run_in_executor`` so async callers don't stall the event loop
    here. We keep the sync API as-is for the sync ``DatabaseWrapper``
    path.
    """
    import asyncio

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, driver.begin)


class AsyncDatabaseWrapper(BaseAsyncDatabaseWrapper):
    """Async DatabaseWrapper for gt_rust, exposed as
    ``gt_rust.django_backend.AsyncDatabaseWrapper`` — the attribute
    name ``django_async_backend.db.utils.AsyncConnectionHandler``
    looks for via ``load_backend``.
    """

    vendor = "postgresql"
    display_name = "PostgreSQL (gt_rust async)"
    data_types = _PgSyncDatabaseWrapper.data_types
    data_type_check_constraints = (
        _PgSyncDatabaseWrapper.data_type_check_constraints
    )
    data_types_suffix = _PgSyncDatabaseWrapper.data_types_suffix
    operators = _PgSyncDatabaseWrapper.operators
    pattern_esc = _PgSyncDatabaseWrapper.pattern_esc
    pattern_ops = _PgSyncDatabaseWrapper.pattern_ops

    Database = _dbapi
    features_class = _PgFeatures
    ops_class = AsyncDatabaseOperations

    # No psycopg pool. ``pool`` is a property the Base class reads
    # when building connection params; returning None marks us as
    # pool-less from async-backend's perspective (our pool is in Rust
    # and invisible to it).
    @property
    def pool(self):
        return None

    def get_connection_params(self):
        # Mirror the sync wrapper's implementation so both share one
        # OPTIONS/DATABASES config.
        from gt_rust.django_backend.base import DatabaseWrapper as _SyncDW

        # _SyncDW is a class, this calls the method unbound — same
        # body, different wrapper instance. Keep in sync if the sync
        # version changes.
        return _SyncDW.get_connection_params(self)

    async def get_new_connection(self, conn_params):
        driver = _dbapi._shared_driver(**conn_params)
        return GtRustAsyncConnection(driver)

    def create_cursor(self, name=None):
        if name:
            from django.db.utils import NotSupportedError

            raise NotSupportedError(
                "gt_rust does not support server-side/named cursors"
            )
        return GtRustAsyncCursor(self.connection)

    async def _set_autocommit(self, autocommit: bool) -> None:
        # async-backend's ``set_autocommit`` awaits this — the Base
        # class declares the hook as sync but the wrapper expects an
        # awaitable. Match psycopg3's AsyncDatabaseWrapper, which
        # also exposes this as ``async def``.
        self.connection.autocommit = bool(autocommit)

    async def get_database_version(self):
        # ``driver.server_version`` returns server_version_num as an
        # int (e.g. 160001 → (16, 1)).
        num = await self.connection.driver.server_version()
        return divmod(int(num), 10000)

    async def connect(self):
        # BaseAsyncDatabaseWrapper.connect sends ``connection_created``
        # at the end. Django's postgres contrib registers a receiver
        # (``register_type_handlers`` → ``get_hstore_oids``) that uses
        # the SYNC ``connections[alias].cursor()`` inside the handler,
        # which raises ``SynchronousOnlyOperation`` when the signal
        # fires from an async task. We still want to dispatch the
        # signal so third-party receivers run, but we have to catch
        # SynchronousOnlyOperation specifically and ignore it; any
        # other exception propagates.
        import logging
        import time

        from django.core.exceptions import SynchronousOnlyOperation
        from django.db.backends.signals import connection_created

        self.check_settings()
        self.in_atomic_block = False
        self.savepoint_ids = []
        self.atomic_blocks = []
        self.needs_rollback = False
        self.health_check_enabled = self.settings_dict["CONN_HEALTH_CHECKS"]
        max_age = self.settings_dict["CONN_MAX_AGE"]
        self.close_at = None if max_age is None else time.monotonic() + max_age
        self.closed_in_transaction = False
        self.errors_occurred = False
        self.health_check_done = True

        conn_params = self.get_connection_params()
        self.connection = await self.get_new_connection(conn_params)
        await self.set_autocommit(self.settings_dict["AUTOCOMMIT"])
        await self.init_connection_state()

        # Run signal receivers but tolerate the postgres contrib hstore
        # handler refusing to run from an async context. Anything else
        # is a real bug and should surface.
        try:
            connection_created.send(sender=self.__class__, connection=self)
        except SynchronousOnlyOperation:
            logging.getLogger(__name__).debug(
                "connection_created receiver raised SynchronousOnlyOperation "
                "(likely Django's hstore handler); skipping."
            )

        self.run_on_commit = []

"""DatabaseWrapper that swaps psycopg IO for gt_rust, keeping Django's
postgresql machinery (schema editor, introspection, operations, features)
intact.

Strategy: subclass ``django.db.backends.postgresql.base.DatabaseWrapper``
and override only:

* ``Database`` — points at :mod:`gt_rust.dbapi` instead of ``psycopg``.
* ``get_new_connection`` — returns a :class:`gt_rust.dbapi.Connection`.
* ``get_connection_params`` — filters psycopg-only kwargs the stock
  implementation tries to pass (``prepare_threshold``, ``cursor_factory``,
  ``pool``, etc.).
* ``create_cursor`` — skips the psycopg-specific tz-loader registration.
* ``_nodb_cursor`` — avoids psycopg-specific error class checks.

Everything else — migrations, schema, introspection, the connection
lifecycle, autocommit/transaction handling — is inherited from
``BaseDatabaseWrapper`` and ``postgresql.DatabaseWrapper``.
"""

from __future__ import annotations

from contextlib import contextmanager

from django.db.backends.postgresql.base import (
    DatabaseWrapper as PgDatabaseWrapper,
)
from django.utils.asyncio import async_unsafe

from gt_rust import dbapi as _dbapi
from gt_rust.django_backend.operations import DatabaseOperations
from gt_rust.django_backend.schema import DatabaseSchemaEditor

# Re-export ``AsyncDatabaseWrapper`` so ``django.db.utils.load_backend``
# (which appends ``.base`` to the ENGINE path) finds it. Without this,
# ``django_async_backend.db.async_connections`` raises "The async
# connection 'default' doesn't exist." because ``hasattr(backend,
# "AsyncDatabaseWrapper")`` sees only the sync wrapper here.
try:
    from gt_rust.django_backend.async_base import (
        AsyncDatabaseWrapper,  # noqa: F401
    )
except ImportError:  # pragma: no cover — async-backend optional at import
    pass


class DatabaseWrapper(PgDatabaseWrapper):
    """Postgres-over-gt_rust wrapper.

    We keep ``vendor = "postgresql"`` so Django's feature flags,
    introspection dispatch, and third-party django-postgres-extras code
    continue to treat us as a postgres backend.
    """

    display_name = "PostgreSQL (gt_rust)"
    Database = _dbapi  # type: ignore[assignment]
    # Point at our schema editor so migrations don't reach for
    # psycopg.ClientCursor via ops.compose_sql.
    SchemaEditorClass = DatabaseSchemaEditor
    ops_class = DatabaseOperations

    def get_connection_params(self):
        # Stock get_connection_params assembles psycopg-flavored kwargs
        # (prepare_threshold, cursor_factory, context-managed pool,
        # application_name, etc.). We only need the raw connection info
        # plus a couple of gt_rust-specific options. Build from scratch
        # to avoid inheriting psycopg-only defaults.
        settings_dict = self.settings_dict
        # NAME may be unset during test-database creation (Django uses a
        # "nodb" cursor that talks to the default "postgres" DB to run
        # CREATE DATABASE). Fall back to "postgres" in that case.
        name = settings_dict.get("NAME") or "postgres"
        if name and len(name) > self.ops.max_name_length():
            from django.core.exceptions import ImproperlyConfigured

            raise ImproperlyConfigured(
                "The database name '%s' (%d characters) is longer than "
                "PostgreSQL's limit of %d characters. Supply a shorter NAME "
                "in settings.DATABASES."
                % (name, len(name), self.ops.max_name_length())
            )
        options = settings_dict.get("OPTIONS") or {}
        # Reuse the same OPTIONS["pool"] dict shape Django's psycopg3
        # backend respects so a single DATABASES entry configures both
        # backends. We honour the five keys an operator actually tunes
        # (max_size, min_size, timeout, max_lifetime, max_idle) plus
        # ``name`` which we route to ``application_name`` so it shows
        # up in pg_stat_activity. Other psycopg-pool keys (num_workers,
        # reconnect_timeout, max_waiting) don't have deadpool analogues
        # and emit a one-time warning below.
        pool_opts = options.get("pool") or {}
        _SUPPORTED_POOL_KEYS = {
            "max_size", "min_size", "timeout",
            "max_lifetime", "max_idle", "name",
        }
        if isinstance(pool_opts, dict):
            unknown = set(pool_opts) - _SUPPORTED_POOL_KEYS
            if unknown and not getattr(self, "_gt_rust_pool_warned", False):
                import warnings
                warnings.warn(
                    f"gt_rust ignores unsupported OPTIONS['pool'] keys: "
                    f"{sorted(unknown)}. Supported: "
                    f"{sorted(_SUPPORTED_POOL_KEYS)}.",
                    stacklevel=2,
                )
                self._gt_rust_pool_warned = True
        else:
            pool_opts = {}
        pool_size = (
            options.get("pool_size")
            or pool_opts.get("max_size")
            or 10
        )
        # Pool name flows to application_name when an explicit
        # application_name isn't set — psycopg's pool ``name`` is just
        # an internal identifier, but operators actually want pool
        # identity visible in pg_stat_activity / pg_stat_statements.
        default_app_name = f"gt_rust:{self.alias}"
        if pool_opts.get("name"):
            default_app_name = pool_opts["name"]
        conn_params = {
            "host": settings_dict["HOST"] or "localhost",
            "port": int(settings_dict["PORT"] or 5432),
            "dbname": name,
            "user": settings_dict["USER"] or "",
            "password": settings_dict["PASSWORD"] or "",
            # libpq-style sslmode names (disable / allow / prefer /
            # require / verify-ca / verify-full) so a Django settings
            # dict that works for psycopg works here too.
            "sslmode": options.get("sslmode", "prefer"),
            "ca_cert_path": options.get("sslrootcert"),
            "client_cert_path": options.get("sslcert"),
            "client_key_path": options.get("sslkey"),
            # Visible in pg_stat_activity. Falls back to
            # f"gt_rust:{alias}" (or pool name) so unconfigured
            # deployments still have something useful during triage.
            "application_name": options.get(
                "application_name", default_app_name
            ),
            # Network-tuning knobs travel through the same OPTIONS keys
            # libpq / psycopg accept. None means "leave to OS / driver
            # default"; positive values activate the corresponding
            # tokio-postgres Config field.
            "connect_timeout": options.get("connect_timeout"),
            "keepalives": options.get("keepalives"),
            "keepalives_idle": options.get("keepalives_idle"),
            # pgbouncer-in-transaction-mode skips server-side prepared
            # statements. GlitchTip enables a pool via psycopg OPTIONS;
            # that's fine here because gt_rust has its own pool and the
            # "pgbouncer" toggle is independent.
            "prepared_statements": not options.get("pgbouncer", False),
            "pool_size": int(pool_size),
            # Pool tuning. ``min_size`` pre-warms idle connections at
            # startup; ``timeout`` is the wait-for-slot fail-fast
            # threshold; ``max_lifetime`` and ``max_idle`` recycle
            # stale connections during the deadpool pre_recycle hook.
            "pool_min_size": int(pool_opts["min_size"])
                if pool_opts.get("min_size") is not None else None,
            "pool_wait_timeout": float(pool_opts["timeout"])
                if pool_opts.get("timeout") is not None else None,
            "pool_max_lifetime": float(pool_opts["max_lifetime"])
                if pool_opts.get("max_lifetime") is not None else None,
            "pool_max_idle": float(pool_opts["max_idle"])
                if pool_opts.get("max_idle") is not None else None,
            "alias": self.alias,
        }
        server_settings: dict[str, str] = dict(options.get("server_settings") or {})
        if self.timezone_name:
            server_settings.setdefault("TimeZone", self.timezone_name)

        # Isolation level — accept either psycopg's IsolationLevel enum
        # (1..4) or a libpq-style string. Apply via the PG GUC
        # ``default_transaction_isolation`` so every transaction on this
        # connection picks it up; psycopg sets it on the connection
        # object, but we don't have a psycopg connection to set on.
        isolation_value = options.get("isolation_level")
        if isolation_value is not None:
            from django.core.exceptions import ImproperlyConfigured
            from django.db.backends.postgresql.psycopg_any import (
                IsolationLevel,
            )
            try:
                level = IsolationLevel(isolation_value)
            except ValueError:
                raise ImproperlyConfigured(
                    f"Invalid transaction isolation level "
                    f"{isolation_value!r}. Use one of the "
                    f"psycopg.IsolationLevel values."
                )
            level_to_pg = {
                IsolationLevel.READ_UNCOMMITTED: "read uncommitted",
                IsolationLevel.READ_COMMITTED: "read committed",
                IsolationLevel.REPEATABLE_READ: "repeatable read",
                IsolationLevel.SERIALIZABLE: "serializable",
            }
            server_settings.setdefault(
                "default_transaction_isolation", level_to_pg[level]
            )
            # Stash on the wrapper so Django code that reads
            # ``connection.isolation_level`` (stock postgresql backend
            # convention) sees the configured value.
            self.isolation_level = level

        if server_settings:
            conn_params["server_settings"] = server_settings
        return conn_params

    @async_unsafe
    def get_new_connection(self, conn_params):
        # gt_rust's Connection is autocommit-by-default; Django will toggle
        # this via _set_autocommit after the connection is opened.
        return self.Database.connect(**conn_params)

    @async_unsafe
    def create_cursor(self, name=None):
        if name:
            # Named cursor → real DECLARE/FETCH/CLOSE backed by a
            # pinned connection (gt_rust.dbapi.ServerSideCursor). In
            # autocommit the cursor is declared WITH HOLD so it
            # outlives the implicit transaction, matching psycopg's
            # behaviour and Django's ``chunked_cursor()`` contract.
            return self.connection.cursor(
                name, scrollable=False, withhold=self.connection.autocommit
            )
        return self.connection.cursor()

    def is_usable(self):
        if self.connection is None:
            return False
        try:
            with self.connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except self.Database.Error:
            return False
        return True

    @contextmanager
    def _nodb_cursor(self):
        # Bypass the stock implementation's psycopg-specific error
        # catching; fall back to BaseDatabaseWrapper's _nodb_cursor which
        # just opens a cursor on the current connection.
        from django.db.backends.base.base import BaseDatabaseWrapper

        with BaseDatabaseWrapper._nodb_cursor(self) as cursor:
            yield cursor

    def _set_autocommit(self, autocommit):
        # Match the postgresql backend's contract: the connection object
        # itself owns the autocommit flag.
        with self.wrap_database_errors:
            self.connection.autocommit = autocommit

    def check_constraints(self, table_names=None):
        with self.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cursor.execute("SET CONSTRAINTS ALL DEFERRED")

    def _close(self):
        # The stock postgresql _close reaches for ``self.connection._pool``
        # (psycopg3's internal pool handle). gt_rust's Connection has no
        # such attribute — our pool lives in Rust and is shared across
        # threads. Fall back to the base wrapper's plain close() path.
        if self.connection is not None:
            with self.wrap_database_errors:
                return self.connection.close()

    @property
    def pool(self):
        # Django's postgresql backend exposes this as a psycopg3-pool
        # object. Return None so callers treat us as pool-less from
        # their perspective (our Rust pool is invisible to Django).
        return None

    def close_pool(self):
        # Django calls close_pool() in two contexts:
        #
        #   1. Test-runner teardown (``_destroy_test_db``) — drain so a
        #      subsequent DROP DATABASE doesn't see lingering sessions.
        #   2. ``ensure_timezone`` after a TZ-affecting setting change —
        #      drop the pool so new connections pick up the new TZ.
        #
        # If self.connection is set, drain via its driver reference and
        # let the wrapper rebuild on the next request. If self.connection
        # is None (test teardown after Django already closed the
        # wrapper) drain any cached driver for this alias directly.
        # Either way, only this alias's driver is touched — sibling
        # wrappers (other aliases) keep their pools.
        from gt_rust import dbapi as _dbapi

        if self.connection is not None:
            try:
                self.connection.drop_shared_driver()
            except Exception:
                pass
            try:
                self.connection.close()
            except Exception:
                pass
            self.connection = None
        else:
            _dbapi.close_drivers_for_alias(self.alias)

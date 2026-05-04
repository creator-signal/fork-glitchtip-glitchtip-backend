"""DatabaseOperations override for gt_rust.

The stock postgresql DatabaseOperations.compose_sql calls into
psycopg's ``mogrify``; ``last_executed_query`` reaches for psycopg3's
``cursor._query.query``. Replace both with our own param-inliner so
Django's query introspection (CaptureQueriesContext, CursorDebugWrapper,
``last_executed_query``-based test fixtures) sees the SQL with
parameters substituted.
"""

from __future__ import annotations

from django.db.backends.postgresql.operations import (
    DatabaseOperations as PgDatabaseOperations,
)


class DatabaseOperations(PgDatabaseOperations):
    def compose_sql(self, sql, params):
        from gt_rust.django_backend.schema import _inline_params

        if isinstance(sql, bytes):
            sql = sql.decode()
        return _inline_params(str(sql), params)

    def last_executed_query(self, cursor, sql, params):
        # Prefer the actual executed query the cursor recorded — that
        # captures any pre-execute mutations (e.g. ExecuteWrapper that
        # prepends a comment before sending). Fall back to mogrifying
        # the supplied (sql, params) when the cursor didn't record one.
        recorded = getattr(cursor, "query", None)
        if recorded is not None:
            if isinstance(recorded, bytes):
                try:
                    return recorded.decode()
                except UnicodeDecodeError:
                    return None
            return recorded
        try:
            return self.compose_sql(sql, params)
        except Exception:
            return None

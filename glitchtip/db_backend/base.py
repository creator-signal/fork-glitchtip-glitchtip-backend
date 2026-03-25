"""
PostgreSQL backend that disables the sync connection pool.

Under ASGI, the async pool (AsyncDatabaseWrapper) handles connection
pooling for async_objects queries. The sync DatabaseWrapper only serves
sync_to_async fallback calls (write operations, raw SQL) and doesn't
need its own pool — avoiding double pool overhead and excess PG connections.

Under WSGI, there is no async pool, so the sync pool would be useful.
However, WSGI deployments can set DATABASE_CONN_MAX_AGE=None for
persistent per-thread connections instead.
"""

from functools import cached_property

# Re-export everything from the async backend's postgresql module
# so Django can find all standard backend attributes.
from django_async_backend.db.backends.postgresql.base import *  # noqa: F401, F403
from django_async_backend.db.backends.postgresql.base import (
    DatabaseWrapper as _DatabaseWrapper,
)


class DatabaseWrapper(_DatabaseWrapper):
    @cached_property
    def pool(self):
        return None

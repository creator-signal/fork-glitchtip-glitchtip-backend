"""
PostgreSQL backend extending django-async-backend with suppressed pool warning.

The async backend warns when OPTIONS.pool is set because WSGI creates a new
event loop per request, breaking async pool state. However, our sync pool works
fine (it's psycopg3's sync ConnectionPool, not async), and OPTIONS.pool is also
read by the async AsyncDatabaseWrapper for the async pool. So the warning is a
false positive — suppress it by skipping the parent's get_connection_params().
"""

# Re-export everything from the async backend's postgresql module
# so Django can find all standard backend attributes.
from django_async_backend.db.backends.postgresql.base import *  # noqa: F401, F403
from django_async_backend.db.backends.postgresql.base import (
    DatabaseWrapper as _DatabaseWrapper,
)


class DatabaseWrapper(_DatabaseWrapper):
    def get_connection_params(self):
        # Skip the async-backend's get_connection_params which emits a
        # misleading RuntimeWarning about pool + sync mode. Call Django's
        # stock implementation directly.
        from django.db.backends.postgresql.base import DatabaseWrapper as _DjangoDW

        return _DjangoDW.get_connection_params(self)

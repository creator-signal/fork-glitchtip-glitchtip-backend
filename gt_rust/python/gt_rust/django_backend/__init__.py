"""Django database backend backed by the gt_rust PostgreSQL driver.

Wire up in ``DATABASES``::

    DATABASES = {
        "default": {
            "ENGINE": "gt_rust.django_backend",
            "NAME": "glitchtip",
            "USER": "postgres",
            ...
        }
    }

The backend subclasses ``django.db.backends.postgresql`` and replaces
only the connection/cursor IO layer with :mod:`gt_rust.dbapi`. Schema
introspection, migrations, operations, features — all inherit from the
stock postgresql backend.
"""

from gt_rust.django_backend.base import DatabaseWrapper

# ``AsyncDatabaseWrapper`` is discovered by
# ``django_async_backend.db.utils.AsyncConnectionHandler`` via
# ``load_backend(ENGINE)``. Importing it lazily would also work, but
# keeping it at module top means ``hasattr(backend,
# "AsyncDatabaseWrapper")`` returns True immediately.
#
# If django-async-backend isn't installed, importing this submodule
# fails. That's intentional — gt_rust's async path is built on top
# of async-backend, it isn't a standalone driver.
try:
    from gt_rust.django_backend.async_base import AsyncDatabaseWrapper
except ImportError:  # pragma: no cover — async-backend optional at runtime
    AsyncDatabaseWrapper = None  # type: ignore[assignment]

__all__ = ["DatabaseWrapper", "AsyncDatabaseWrapper"]

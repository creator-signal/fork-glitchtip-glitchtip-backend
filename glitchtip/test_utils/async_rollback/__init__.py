"""Vendored test utilities for rolling back django-async-backend writes.

Vendored from django-async-backend PR #20:
https://github.com/Arfey/django-async-backend/pull/20

Copied from upstream commit ea07ec8 (branch
``feat/asyncio-rollback-testcase`` in https://github.com/bufke/django-async-backend).

Remove this module and import directly from
``django_async_backend.test`` once upstream merges.
"""

from glitchtip.test_utils.async_rollback.test_case import AsyncioRollbackTestCase

__all__ = ["AsyncioRollbackTestCase"]

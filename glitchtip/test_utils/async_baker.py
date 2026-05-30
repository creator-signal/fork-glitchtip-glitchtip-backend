"""Async model-bakery fixtures.

Re-exports model_bakery's async ``amake``/``aprepare`` through a single local
module so test call sites are insulated from where the implementation lives.

Today these come from the async-support branch of model_bakery, pinned in
``pyproject.toml`` under ``[tool.uv.sources]``. ``amake`` persists via Django's
native async ORM (``asave``), so it works whether or not ``USE_ASYNC_BACKEND``
is enabled: with the async backend off it runs on the default connection like
``baker.make``; with it on, fixtures land on the async connection, which is the
prerequisite for per-test async rollback instead of ``TransactionTestCase``
truncation.

When the upstream PR releases (model_bakery#599), only this module's import
changes; call sites that do ``from glitchtip.test_utils.async_baker import
amake`` stay put.
"""

from model_bakery.baker import amake, aprepare

__all__ = ["amake", "aprepare"]

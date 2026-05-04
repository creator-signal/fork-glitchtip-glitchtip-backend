"""Rust PostgreSQL driver for GlitchTip.

Thin Python shim over the compiled ``gt_rust._rust`` extension. Exposes the
driver plus a helper that builds one from Django's DATABASES settings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from gt_rust._rust import RustAwaitable, RustPgDriver, RustTransaction

if TYPE_CHECKING:  # pragma: no cover
    from django.conf import LazySettings


__all__ = [
    "RustAwaitable",
    "RustPgDriver",
    "RustTransaction",
    "driver_from_django_settings",
]


def driver_from_django_settings(
    settings: LazySettings | None = None,
    alias: str = "default",
) -> RustPgDriver:
    """Build a ``RustPgDriver`` from ``settings.DATABASES[alias]``.

    Reads the same HOST/PORT/NAME/USER/PASSWORD/OPTIONS fields Django uses.
    Honors these optional module-level settings:

    * ``RUST_PG_POOL_SIZE`` (default: 50)
    * ``RUST_PG_ISOLATION_LEVEL`` — e.g. ``"read committed"``
    """
    if settings is None:
        from django.conf import settings as _settings

        settings = _settings

    db = settings.DATABASES[alias]
    opts = db.get("OPTIONS", {})

    server_settings: dict[str, Any] = {"TimeZone": settings.TIME_ZONE or "UTC"}
    iso = getattr(settings, "RUST_PG_ISOLATION_LEVEL", None)
    if iso:
        server_settings["default_transaction_isolation"] = iso

    return RustPgDriver.connect(
        host=db["HOST"],
        port=int(db["PORT"]),
        dbname=db["NAME"],
        user=db["USER"],
        password=db["PASSWORD"],
        pool_size=getattr(settings, "RUST_PG_POOL_SIZE", 50),
        sslmode=opts.get("sslmode", "prefer"),
        ca_cert_path=opts.get("sslrootcert"),
        prepared_statements=not opts.get("pgbouncer", False),
        server_settings=server_settings,
    )

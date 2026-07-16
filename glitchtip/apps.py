from django.apps import AppConfig


class GlitchtipConfig(AppConfig):
    name = "glitchtip"

    def ready(self):
        from . import task_signals  # noqa: F401

        self._skip_hstore_oid_lookup()
        self._add_dbapi_binary()

    @staticmethod
    def _add_dbapi_binary():
        # Django's BinaryField calls ``connection.Database.Binary()`` (the
        # DB-API 2.0 constructor) in get_db_prep_value — the DB task broker's
        # payload column hits it. gt_rust.dbapi lacks the attribute until
        # glitchtip-rust 0.6.1; ``bytes`` is the correct constructor (the
        # driver marshals bytes params to BYTEA). Remove once the pin
        # reaches 0.6.1.
        from gt_rust import dbapi

        if not hasattr(dbapi, "Binary"):
            dbapi.Binary = bytes

    @staticmethod
    def _skip_hstore_oid_lookup():
        # django.contrib.postgres wires register_type_handlers to
        # connection_created, which runs `SELECT ... FROM pg_type WHERE
        # typname = 'hstore'` on every new connection so psycopg can
        # auto-decode hstore columns. We use jsonb instead of hstore, so
        # the round-trip is pure overhead — and the receiver uses the
        # sync ORM, which raises SynchronousOnlyOperation when a task
        # opens its first async connection.
        from django.contrib.postgres.signals import register_type_handlers
        from django.db.backends.signals import connection_created

        connection_created.disconnect(register_type_handlers)

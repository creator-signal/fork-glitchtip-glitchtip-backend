# gt_rust — known gaps vs. Django's stock psycopg backend

Validated against Django 6.0.4's `tests/backends/` suite under
`ENGINE=gt_rust.django_backend`. Of 232 contract-relevant tests
(excluding the 96 skipped for non-PG backends), the gaps below
account for the remaining failures. Counts will fluctuate by ±5
across Django patch releases — the GlitchTip suite (1022 tests)
passes in full under both backends.

We use ``prepare_cached`` (untyped) and let Postgres infer parameter
types from SQL context. We tried ``prepare_typed_cached`` to pass the
``WHERE x = $1 + $2``-style Django contract tests, but sending explicit
types broke real GlitchTip code (tsvector inserts and function-call
overload resolution where ``bigint → integer`` is not implicit). The
inference path is what psycopg has always done; we stick with it.

## Server-side cursors — supported, but `pg_cursors`-introspection tests fail

`connection.cursor(name=...)` now returns a real
`gt_rust.dbapi.ServerSideCursor` that issues `DECLARE` / `FETCH FORWARD`
/ `CLOSE` against a pinned connection (cooperating with the surrounding
Django `atomic()` transaction, or wrapping in a `WITH HOLD`
micro-transaction in autocommit). The cursor itself works correctly —
incremental fetches return typed rows, `description` is populated,
`rowcount` accumulates, and `close()` removes the cursor from
`pg_cursors`.

`backends.postgresql.test_server_side_cursors` still fails (8 tests) on
this divergence: tokio-postgres uses the extended-query protocol for
every `SELECT`, which materialises an unnamed portal that is visible
in `pg_cursors` *while the introspecting query itself is executing*.
psycopg3 uses the simple-query protocol for parameter-less `SELECT`s,
so its inspect queries don't show up in their own results. The tests
assert `len(pg_cursors_rows) == 1` and find 2: their `_django_curs_*`
plus our portal. The cursor functionality is fine; only the
self-introspection assertion fails.

Closing this gap would require a parallel simple-query code path with
its own text-format type coercion. Not pursued for shipping —
GlitchTip sets `DISABLE_SERVER_SIDE_CURSORS=True` globally and never
hits this path in production.

## Pool configuration (psycopg-compatible subset)

`OPTIONS["pool"]` accepts the five keys an operator actually tunes,
plus a `name` that flows to `application_name` for `pg_stat_activity`
visibility. Mapped onto deadpool-postgres:

- `max_size` → deadpool max_size (hard pool ceiling).
- `min_size` → eager pre-warm count at startup (capped by `max_size`).
- `timeout` → `PoolConfig.timeouts.wait`, fail-fast threshold when
  every slot is busy.
- `max_lifetime` → pre_recycle hook discards a connection older than
  this (seconds since `created`).
- `max_idle` → pre_recycle hook discards a connection that has been
  idle longer than this (since the last recycle / creation).
- `name` → routed to `application_name` if no explicit
  `OPTIONS["application_name"]` is set, so pools show up distinctly
  in `pg_stat_activity` / `pg_stat_statements`. (Deviation from
  psycopg, where `name` is just a pool-internal identifier.)

Other psycopg-pool keys (`num_workers`, `reconnect_timeout`,
`max_waiting`) emit a one-time `UserWarning` naming the unsupported
key — deadpool's architecture has no analogue. File an issue with a
real workload if you need one.

Tests under `backends.postgresql.test_server_side_binding` and
`backends.postgresql.tests` related to psycopg-specific connect
internals still fail; these read psycopg-only state and don't apply:

- `test_connect_role`, `test_connect_server_side_binding`,
  `test_connect_custom_cursor_factory` — psycopg-specific connect
  parameters. (`OPTIONS["isolation_level"]` IS supported and applied
  via the ``default_transaction_isolation`` GUC.)
- `test_client_encoding_utf8_enforce`, `test_check_database_version_supported`
  — read psycopg's version / encoding state.
- `test_compose_sql_when_no_connection` — psycopg's `compose_sql` API
  contract for a not-yet-opened connection.
- `test_bypass_timezone_configuration` — Django's mechanism for
  bypassing `ensure_timezone` checks the underlying connection's
  `info.parameter_status("TimeZone")`, which is psycopg-specific.
- `test_nodb_cursor`, `test_nodb_cursor_raises_postgres_authentication_failure`
  — Django's `_nodb_cursor()` (used to talk to the maintenance DB)
  uses a psycopg-specific connection-rebuild path.

The pool-config tests in the backends suite (`test_connect_pool`,
`test_pooling_health_checks`) still fail because they introspect
psycopg's pool object directly via `connection.pool`, which we
deliberately return as `None` (our pool lives in Rust and isn't
psycopg-shaped).

## Cascade artefacts (4 errors)

- `tearDownClass`, `test_can_reference_existent`,
  `test_can_reference_non_existent`, `test_many_to_many` — cascade
  fallout from earlier tearDown failures in the same module.
  Re-running the affected tests in isolation produces no error.

## How to reproduce

```sh
git clone --depth 1 --branch 6.0.4 https://github.com/django/django.git
cat > django/tests/test_gt_rust.py <<'PY'
DATABASES = {
    "default": {"ENGINE": "gt_rust.django_backend",
                "USER": "postgres", "PASSWORD": "postgres",
                "NAME": "django_tests", "HOST": "postgres", "PORT": 5432,
                "DISABLE_SERVER_SIDE_CURSORS": True,
                "OPTIONS": {"pool": {"min_size": 1, "max_size": 5, "timeout": 30}}},
    "other": {"ENGINE": "gt_rust.django_backend",
              "USER": "postgres", "PASSWORD": "postgres",
              "NAME": "django_tests2", "HOST": "postgres", "PORT": 5432,
              "DISABLE_SERVER_SIDE_CURSORS": True,
              "OPTIONS": {"pool": {"min_size": 1, "max_size": 5, "timeout": 30}}},
}
SECRET_KEY = "django_tests_secret_key"
USE_TZ = False
INSTALLED_APPS = ["django.contrib.postgres"]
PY
docker compose run --rm -v $PWD/django:/django_src --workdir /django_src/tests web \
    bash -c "pip install -q django-async-backend && \
             python -u runtests.py --settings=test_gt_rust --noinput \
                                   --parallel=1 backends"
```

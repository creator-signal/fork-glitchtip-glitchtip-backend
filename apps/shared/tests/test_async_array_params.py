"""Round-trip tests for typed-array parameters via the async cursor.

Driver-agnostic: covers the patterns the ingest hot paths use to send
``unnest($1::uuid[], $2::timestamptz[], ...)`` style queries. String
elements must coerce to the destination type the same way scalar
parameters do, so callsites that pass hex digests or ISO timestamps
work under any driver behind ``django_async_backend``.
"""

import json
import multiprocessing
import os
import sys
import uuid
from datetime import datetime, timezone

from django.conf import settings
from django.db import DataError, connection
from django.test import TransactionTestCase
from django_async_backend.db import async_connections


class AsyncArrayParamRoundtripTests(TransactionTestCase):
    """``unnest($1::T[])`` over async cursor for each typed array."""

    async def _one(self, sql: str, params: list) -> list[tuple]:
        async with await async_connections["default"].cursor() as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()

    async def test_uuid_array_from_strings_no_dashes(self):
        rows = await self._one(
            "SELECT * FROM unnest(%s::uuid[]) AS k",
            [["9c895553a08c789e3a29c02a2ba0ae03", "87b2035414c542763dbda05eb69717af"]],
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], uuid.UUID("9c895553a08c789e3a29c02a2ba0ae03"))

    async def test_uuid_array_from_strings_with_dashes(self):
        rows = await self._one(
            "SELECT * FROM unnest(%s::uuid[]) AS k",
            [["9c895553-a08c-789e-3a29-c02a2ba0ae03"]],
        )
        self.assertEqual(rows[0][0], uuid.UUID("9c895553a08c789e3a29c02a2ba0ae03"))

    async def test_uuid_array_from_uuid_objects(self):
        u = uuid.UUID("9c895553a08c789e3a29c02a2ba0ae03")
        rows = await self._one("SELECT * FROM unnest(%s::uuid[]) AS k", [[u]])
        self.assertEqual(rows[0][0], u)

    async def test_uuid_array_with_nulls(self):
        rows = await self._one(
            "SELECT * FROM unnest(%s::uuid[]) AS k",
            [
                [
                    "9c895553a08c789e3a29c02a2ba0ae03",
                    None,
                    "87b2035414c542763dbda05eb69717af",
                ]
            ],
        )
        self.assertEqual(len(rows), 3)
        self.assertIsNotNone(rows[0][0])
        self.assertIsNone(rows[1][0])
        self.assertIsNotNone(rows[2][0])

    async def test_timestamptz_array_from_iso_strings(self):
        now = datetime.now(timezone.utc)
        iso = now.isoformat()
        rows = await self._one(
            "SELECT * FROM unnest(%s::timestamptz[]) AS k",
            [[iso, iso]],
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], now)

    async def test_timestamptz_array_from_datetime(self):
        now = datetime.now(timezone.utc)
        rows = await self._one(
            "SELECT * FROM unnest(%s::timestamptz[]) AS k",
            [[now]],
        )
        self.assertEqual(rows[0][0], now)

    async def test_two_arg_unnest_mixed_types(self):
        """Pattern used by ``_fetch_issue_hashes_raw``."""
        sql = """
            SELECT k.project_id, k.value
            FROM unnest(%s::bigint[], %s::uuid[]) AS k(project_id, value)
        """
        ids = [1, 2, 3]
        hashes = [
            "9c895553a08c789e3a29c02a2ba0ae03",
            "87b2035414c542763dbda05eb69717af",
            "6a75fd261f3f2ca09d13d4c21fce90bd",
        ]
        rows = await self._one(sql, [ids, hashes])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][0], 1)
        self.assertEqual(rows[0][1], uuid.UUID(hashes[0]))

    async def test_text_array_unchanged(self):
        """Control: ordinary text[] still works."""
        rows = await self._one(
            "SELECT * FROM unnest(%s::text[]) AS k", [["a", "b", "c"]]
        )
        self.assertEqual([r[0] for r in rows], ["a", "b", "c"])

    async def test_bigint_array(self):
        rows = await self._one("SELECT * FROM unnest(%s::bigint[]) AS k", [[1, 2, 3]])
        self.assertEqual([r[0] for r in rows], [1, 2, 3])

    async def test_date_array_from_iso_strings(self):
        rows = await self._one(
            "SELECT * FROM unnest(%s::date[]) AS k",
            [["2026-01-15", "2026-05-06"]],
        )
        self.assertEqual(len(rows), 2)
        from datetime import date

        self.assertEqual(rows[0][0], date(2026, 1, 15))

    async def test_jsonb_array_from_json_strings(self):
        rows = await self._one(
            "SELECT * FROM unnest(%s::jsonb[]) AS k",
            [['{"a": 1}', '{"b": [2, 3]}', "{}"]],
        )
        self.assertEqual(len(rows), 3)
        # Drivers differ on whether jsonb columns come back as parsed
        # objects (rust ENGINE) or raw strings (psycopg without the
        # default JSON loader installed on the connection). Compare the
        # parsed form so the contract is "JSON round-trips intact".
        self.assertEqual(_as_json(rows[0][0]), {"a": 1})
        self.assertEqual(_as_json(rows[1][0]), {"b": [2, 3]})
        self.assertEqual(_as_json(rows[2][0]), {})

    async def test_jsonb_array_with_nulls(self):
        rows = await self._one(
            "SELECT * FROM unnest(%s::jsonb[]) AS k",
            [['{"x": 1}', None, "{}"]],
        )
        self.assertEqual(len(rows), 3)
        self.assertEqual(_as_json(rows[0][0]), {"x": 1})
        self.assertIsNone(rows[1][0])
        self.assertEqual(_as_json(rows[2][0]), {})


def _as_json(value):
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def _rust_engine_active() -> bool:
    return "gt_rust" in settings.DATABASES["default"]["ENGINE"]


class GtRustErrorClassificationTests(TransactionTestCase):
    """Driver-specific: jsonb[] coercion failures must surface as DataError.

    The rust ENGINE writes raw JSON bytes for ``jsonb[]`` parameters and
    lets PG validate (skipping a client-side serde round-trip). PG
    rejects malformed JSON with SQLSTATE 22P02 (``invalid_text_representation``),
    which the dbapi shim's class-code matcher routes to ``DataError`` —
    matching psycopg semantics.

    The classifier in ``classify_pg_error`` also synthesizes [22P02] for
    tokio-postgres ToSql/FromSql failures (other typed-array arms still
    parse client-side, e.g. ``::uuid[]``). If upstream renames either
    Display prefix, that synthesis goes missing and this test fails
    loudly.
    """

    async def test_jsonb_array_malformed_raises_data_error(self):
        if not _rust_engine_active():
            self.skipTest(
                "rust-ENGINE only — psycopg parses jsonb at a different layer"
            )
        with self.assertRaises(DataError) as ctx:
            async with await async_connections["default"].cursor() as cur:
                await cur.execute(
                    "SELECT * FROM unnest(%s::jsonb[]) AS k",
                    [["{'oops'}"]],
                )
        msg = str(ctx.exception)
        # PG's own "invalid input syntax for type json" must reach the
        # user; without source-chain walking it would have been a bare
        # SQLSTATE prefix only.
        self.assertIn("[22P02]", msg)
        self.assertIn("invalid input syntax for type json", msg)

    async def test_jsonb_scalar_malformed_raises_data_error(self):
        """Scalar ``::jsonb`` goes through ``PgParam::Text`` not the array
        arm. Same RawJsonText fast-path; PG validates server-side."""
        if not _rust_engine_active():
            self.skipTest("rust-ENGINE only")
        with self.assertRaises(DataError) as ctx:
            async with await async_connections["default"].cursor() as cur:
                await cur.execute(
                    "SELECT %s::jsonb",
                    ["{'oops'}"],
                )
        msg = str(ctx.exception)
        self.assertIn("[22P02]", msg)
        self.assertIn("invalid input syntax for type json", msg)


# Top-level so the multiprocessing 'fork' context can pickle the target.
# (fork doesn't strictly need pickling, but a top-level function keeps
# the test resilient if a future Python version flips the default.)
def _fork_child_run_query(connect_kwargs, queue):
    try:
        from gt_rust import dbapi  # imported in child to mirror real usage

        conn = dbapi.connect(**connect_kwargs)
        try:
            cur = conn.cursor()
            cur.execute("SELECT pg_backend_pid()", ())
            backend_pid = cur.fetchone()[0]
            cur.close()
        finally:
            conn.close()
        queue.put(("ok", os.getpid(), backend_pid))
    except BaseException as e:  # noqa: BLE001 — surface anything to parent
        queue.put(("err", os.getpid(), f"{type(e).__name__}: {e}"))


class GtRustForkSafetyTests(TransactionTestCase):
    """Forking after the parent populated the driver cache must not
    deadlock or corrupt PG sessions in the child.

    Tokio-postgres ``Client`` handles spawned on the parent's runtime
    have background ``Connection`` futures driving them; ``fork()``
    only carries the calling thread, so the child inherits the
    sockets but not the workers. The child either rebuilds (cache
    eviction in ``dbapi._evict_if_forked``) or surfaces a clear
    ``OperationalError`` (Rust ``check_pid`` guard) — never hangs.
    """

    def test_dbapi_connect_survives_fork(self):
        if not _rust_engine_active():
            self.skipTest("rust-ENGINE only")
        if sys.platform != "linux":
            self.skipTest("fork() semantics tested on Linux only")

        # Parent touches the DB so the driver cache holds a live pool
        # at fork time. Without that, the child's first connect()
        # would build a fresh driver naturally (no inherited state).
        with connection.cursor() as cur:
            cur.execute("SELECT pg_backend_pid()")
            parent_backend_pid = cur.fetchone()[0]

        # Build connect kwargs that hit the same DB the parent used.
        db = connection.settings_dict
        options = db.get("OPTIONS") or {}
        connect_kwargs = dict(
            host=db.get("HOST") or "localhost",
            port=int(db.get("PORT") or 5432),
            dbname=db["NAME"],
            user=db["USER"],
            password=db.get("PASSWORD") or "",
            sslmode=options.get("sslmode", "prefer"),
        )

        ctx = multiprocessing.get_context("fork")
        queue = ctx.Queue()
        proc = ctx.Process(target=_fork_child_run_query, args=(connect_kwargs, queue))
        proc.start()
        proc.join(timeout=30)

        # Hard fail on hang — that's the regression we're guarding against.
        if proc.is_alive():
            proc.terminate()
            proc.join(5)
            self.fail(
                "child still running after 30s — fork-safety regression (deadlock)"
            )

        kind, child_os_pid, value = queue.get(timeout=5)
        self.assertEqual(proc.exitcode, 0, f"child exitcode={proc.exitcode}")
        self.assertEqual(kind, "ok", f"child raised: {value}")
        self.assertNotEqual(child_os_pid, os.getpid())
        self.assertNotEqual(
            value,
            parent_backend_pid,
            "child got the parent's PG backend — pool was inherited, not rebuilt",
        )

"""Round-trip tests for typed-array parameters via the async cursor.

Driver-agnostic: covers the patterns the ingest hot paths use to send
``unnest($1::uuid[], $2::timestamptz[], ...)`` style queries. String
elements must coerce to the destination type the same way scalar
parameters do, so callsites that pass hex digests or ISO timestamps
work under any driver behind ``django_async_backend``.
"""

import uuid
from datetime import datetime, timezone

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
        self.assertEqual(
            rows[0][0], uuid.UUID("9c895553a08c789e3a29c02a2ba0ae03")
        )

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
            [["9c895553a08c789e3a29c02a2ba0ae03", None, "87b2035414c542763dbda05eb69717af"]],
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
        rows = await self._one(
            "SELECT * FROM unnest(%s::bigint[]) AS k", [[1, 2, 3]]
        )
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
        self.assertEqual(rows[0][0], {"a": 1})
        self.assertEqual(rows[1][0], {"b": [2, 3]})
        self.assertEqual(rows[2][0], {})

    async def test_jsonb_array_with_nulls(self):
        rows = await self._one(
            "SELECT * FROM unnest(%s::jsonb[]) AS k",
            [['{"x": 1}', None, "{}"]],
        )
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][0], {"x": 1})
        self.assertIsNone(rows[1][0])
        self.assertEqual(rows[2][0], {})

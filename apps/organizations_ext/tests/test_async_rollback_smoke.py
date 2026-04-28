"""Smoke tests for the inlined ``AsyncioRollbackTestCase``.

These prove three things, against real GlitchTip models, on the default
psycopg path:

1. A row written via ``async_connections`` raw SQL is visible to the rest
   of the same test from both the async pool and the sync ORM (one
   transaction covers both).
2. The class-level ``setUpTestData`` row created via the sync ORM is
   visible to async raw SQL through the bridge.
3. A row written via ``async_connections`` in one test does NOT leak
   into the next test in the same class — the per-test savepoint rolls
   it back along with sync writes.

Test methods deliberately stay in alphabetical order so the
"a_" / "b_" prefixes pin the execution order Django's TestRunner uses.
"""

from django.db import DEFAULT_DB_ALIAS, connection
from django.test import TestCase
from django_async_backend.db import async_connections

from apps.organizations_ext.models import Organization
from glitchtip.test_utils.async_rollback import AsyncioRollbackTestCase

_CANARY_NAME = "_async_rollback_smoke_canary_"


async def _async_insert_project(name, organization_id):
    sql = (
        "INSERT INTO projects_project "
        "(slug, name, platform, first_event, organization_id, "
        " created, scrub_ip_addresses, event_throttle_rate, is_deleted) "
        "VALUES (%s, %s, NULL, NULL, %s, NOW(), TRUE, 0, FALSE)"
    )
    async with await async_connections[DEFAULT_DB_ALIAS].cursor() as c:
        await c.execute(sql, [name.lower(), name, organization_id])


async def _async_count_canary():
    async with await async_connections[DEFAULT_DB_ALIAS].cursor() as c:
        await c.execute(
            "SELECT count(*) FROM projects_project WHERE name = %s",
            [_CANARY_NAME],
        )
        return (await c.fetchone())[0]


class AsyncRollbackSmokeTest(AsyncioRollbackTestCase):
    @classmethod
    def setUpTestData(cls):
        # Sync ORM write inside the class transaction. The bridge means
        # async_connections sees the same transaction.
        cls.organization = Organization.objects.create(name="async_rollback_smoke_org")

    async def test_a_async_write_visible_to_both_pools(self):
        # No canary row at the start of the test.
        self.assertEqual(await _async_count_canary(), 0)

        await _async_insert_project(_CANARY_NAME, self.organization.id)

        # Async raw read sees the new row (proves the bridge committed it
        # to the sync conn's open transaction, not a separate one).
        self.assertEqual(await _async_count_canary(), 1)

        # Class fixture data is visible to async raw SQL through the bridge.
        async with await async_connections[DEFAULT_DB_ALIAS].cursor() as c:
            await c.execute(
                "SELECT count(*) FROM organizations_ext_organization WHERE id = %s",
                [self.organization.id],
            )
            self.assertEqual((await c.fetchone())[0], 1)

    async def test_b_canary_from_prior_test_is_rolled_back(self):
        # If rollback didn't happen, this would be 1.
        self.assertEqual(await _async_count_canary(), 0)


class SyncReadSeesAsyncWriteTest(AsyncioRollbackTestCase):
    """A second class verifies the sync ORM and async raw SQL share the
    same transaction inside one test method."""

    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(name="async_smoke_xclass_org")

    async def test_sync_read_sees_async_write(self):
        from asgiref.sync import sync_to_async

        await _async_insert_project(_CANARY_NAME, self.organization.id)

        def _sync_count():
            with connection.cursor() as c:
                c.execute(
                    "SELECT count(*) FROM projects_project WHERE name = %s",
                    [_CANARY_NAME],
                )
                return c.fetchone()[0]

        # Same transaction on the sync conn — sync read sees the async write.
        self.assertEqual(await sync_to_async(_sync_count, thread_sensitive=True)(), 1)


class PlainTestCaseStaysClean(TestCase):
    """Sanity check: the next plain Django ``TestCase`` in the suite is
    not affected by the bridge state set up above. If something leaked
    (e.g. ``async_connections._connections`` retained a proxy past
    test-class teardown), this baseline TestCase would have surprising
    behavior."""

    def test_no_canary_visible(self):
        with connection.cursor() as c:
            c.execute(
                "SELECT count(*) FROM projects_project WHERE name = %s",
                [_CANARY_NAME],
            )
            self.assertEqual(c.fetchone()[0], 0)

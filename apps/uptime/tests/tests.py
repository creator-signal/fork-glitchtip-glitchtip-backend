from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest import mock

import aiohttp
from aioresponses import aioresponses
from asgiref.sync import async_to_sync
from django.conf import settings
from django.core import mail
from django.core.management import call_command
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time
from model_bakery import baker

from apps.organizations_ext.constants import OrganizationUserRole
from apps.projects.models import ProjectAlertStatus
from glitchtip.test_utils.test_case import GlitchTipTestCaseMixin

from ..constants import MonitorType
from ..models import Monitor, MonitorCheck
from ..tasks import apply_flap_tolerance, dispatch_checks, save_monitor_checks
from ..utils import fetch_all, fetch_with_retries
from ..webhooks import send_uptime_as_webhook


class UptimeTestCase(GlitchTipTestCaseMixin, TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from glitchtip.partition_manager import PartitionManager

        # Create partitions for 2020-01-01 to 2020-01-07 to cover test data
        manager = PartitionManager()
        start_date = datetime(2020, 1, 1, tzinfo=dt_timezone.utc)
        end_date = start_date + timedelta(days=7)
        manager.create_partitions_for_date_range(
            parent_table="uptime_monitorcheck",
            start_date=start_date,
            end_date=end_date,
            partition_interval="DAY",
            key_type="uuid7",
        )

    def create_user_and_project(self):
        self.create_logged_in_user()

    @mock.patch("apps.uptime.tasks.perform_checks")
    def test_dispatch_checks(self, mocked):
        mocked.aenqueue = mock.AsyncMock()
        test_url = "https://example.com"
        with freeze_time("2020-01-01"):
            mon1 = baker.make(
                Monitor, url=test_url, monitor_type=MonitorType.GET, interval=60
            )
            baker.make(Monitor, url=test_url, monitor_type=MonitorType.GET, interval=60)
            baker.make(MonitorCheck, monitor=mon1, organization=mon1.organization)

        # Run through a full interval to ensure we hit the monitors
        async def run_loop():
            for _ in range(60):
                await dispatch_checks.func()

        async_to_sync(run_loop)()

        self.assertGreaterEqual(mocked.aenqueue.call_count, 1)

    @aioresponses()
    def test_fetch_all(self, mocked):
        test_url = "https://example.com"
        mocked.get(test_url, status=200)
        mon1 = baker.make(Monitor, url=test_url, monitor_type=MonitorType.GET)
        mocked.get(test_url, status=200)
        monitors = list(Monitor.objects.all().values())
        results = async_to_sync(fetch_all)(monitors)
        self.assertEqual(results[0]["id"], mon1.pk)

    @aioresponses()
    def test_monitor_checks_integration(self, mocked):
        test_url = "https://example.com"
        mocked.get(test_url, status=200)
        with freeze_time("2020-01-01"):
            mon = baker.make(
                Monitor, url=test_url, monitor_type=MonitorType.GET, interval=60
            )
        self.assertEqual(mon.checks.count(), 1)

        mocked.get(test_url, status=200, repeat=True)
        with freeze_time("2020-01-01"):

            async def run_loop():
                for _ in range(60):
                    await dispatch_checks.func()

            async_to_sync(run_loop)()
        self.assertEqual(mon.checks.count(), 2)

        # Ensure it runs again in the next interval
        with freeze_time("2020-01-02"):
            async_to_sync(run_loop)()
        self.assertEqual(mon.checks.count(), 3)

    def test_is_change_baseline_after_pruning(self):
        """When partition retention prunes all is_change=True records,
        the next check should be marked is_change=True to establish a baseline."""
        with freeze_time("2020-01-01"):
            mon = baker.make(
                Monitor, url="https://example.com", monitor_type=MonitorType.GET
            )
        # Simulate post-pruning: only is_change=False checks remain
        with freeze_time("2020-01-01"):
            baker.make(
                MonitorCheck,
                monitor=mon,
                organization=mon.organization,
                is_up=True,
                is_change=False,
            )

        # Run save_monitor_checks with a result that matches current state
        # (is_up=True, latest_is_up=True, last_change=None)
        result = {
            "id": mon.id,
            "organization_id": mon.organization_id,
            "is_up": True,
            "latest_is_up": True,
            "last_change": None,
            "monitor_type": MonitorType.GET,
        }
        with freeze_time("2020-01-01"):
            async_to_sync(save_monitor_checks)([result], timezone.now())

        # The new check should have is_change=True (baseline)
        latest_check = mon.checks.order_by("-start_check").first()
        self.assertTrue(latest_check.is_change)

    @aioresponses()
    def test_expected_response(self, mocked):
        test_url = "https://example.com"

        mocked.get(test_url, status=200, body="Status: OK")
        monitor = baker.make(
            Monitor,
            name=test_url,
            url=test_url,
            expected_body="OK",
            monitor_type=MonitorType.GET,
        )
        check = monitor.checks.first()
        self.assertTrue(check.is_up)

        mocked.get(test_url, status=200, body="Status: Failure")
        monitor = baker.make(
            Monitor,
            name=test_url,
            url=test_url,
            expected_body="OK",
            monitor_type=MonitorType.GET,
        )
        check = monitor.checks.first()
        self.assertFalse(check.is_up)
        self.assertEqual(check.data["payload"], "Status: Failure")

    @aioresponses()
    def test_monitor_notifications(self, mocked):
        self.create_user_and_project()
        test_url = "https://example.com"
        webhook_url = "https://webhook.example.com"
        mocked.get(test_url, status=200)
        with freeze_time("2020-01-01"):
            baker.make(
                Monitor,
                name=test_url,
                url=test_url,
                monitor_type=MonitorType.GET,
                project=self.project,
            )
            baker.make(
                "alerts.AlertRecipient",
                alert__uptime=True,
                alert__project=self.project,
                recipient_type="email",
            )
            baker.make(
                "alerts.AlertRecipient",
                alert__uptime=True,
                alert__project=self.project,
                recipient_type="webhook",
                url=webhook_url,
            )

        mocked.get(test_url, status=500)
        mocked.post(webhook_url, status=200)

        # We need to hit the tick that matches the monitor ID
        async def run_loop():
            for _ in range(60):
                await dispatch_checks.func()

        with freeze_time("2020-01-02"):
            async_to_sync(run_loop)()

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("is down", mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].extra_headers["X-Mailer"], "GlitchTip")
        self.assertEqual(
            mail.outbox[0].extra_headers["X-GlitchTip-Project"], self.project.name
        )
        self.assertEqual(
            mail.outbox[0].extra_headers["X-GlitchTip-Organization"],
            self.project.organization.name,
        )
        self.assertEqual(
            mail.outbox[0].extra_headers["List-Id"],
            f"<{self.project.slug}.{self.project.organization.slug}.{settings.GLITCHTIP_URL.hostname}>",
        )

        mocked.get(test_url, status=500)
        with freeze_time("2020-01-03"):
            async_to_sync(run_loop)()
        self.assertEqual(len(mail.outbox), 1)

        mocked.get(test_url, status=200)
        mocked.post(webhook_url, status=200)
        with freeze_time("2020-01-04"):
            async_to_sync(run_loop)()
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("is back up", mail.outbox[1].body)

    @mock.patch("aiohttp.ClientSession")
    def test_discord_webhook(self, MockSession):
        from apps.alerts.tests.test_webhooks import _mock_aiohttp_session

        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        self.create_user_and_project()
        test_url = "https://example.com"
        webhook_url = "https://discord.com/api/webhooks/test/test"
        check = baker.make(
            "uptime.MonitorCheck",
            monitor__monitor_type=MonitorType.GET,
            monitor__url=test_url,
            monitor__project=self.project,
        )
        recipient = baker.make(
            "alerts.AlertRecipient", recipient_type="discord", url=webhook_url
        )
        async_to_sync(send_uptime_as_webhook)(recipient, check.pk, True, timezone.now())
        mock_post.assert_called_once()

    @aioresponses()
    def test_notification_default_scope(self, mocked):
        """Subscribe by default should not result in alert emails for non-team members"""
        self.create_user_and_project()
        test_url = "https://example.com"

        # user2 is an org member but not in a relevant team, should not receive alerts
        user2 = baker.make("users.user")
        org_user2 = self.organization.add_user(user2, OrganizationUserRole.MEMBER)
        team2 = baker.make("teams.Team", organization=self.organization)
        team2.members.add(org_user2)

        # user3 is in team3 which should receive alerts
        user3 = baker.make("users.user")
        org_user3 = self.organization.add_user(user3, OrganizationUserRole.MEMBER)
        self.team.members.add(org_user3)
        team3 = baker.make("teams.Team", organization=self.organization)
        team3.members.add(org_user3)
        team3.projects.add(self.project)

        baker.make(
            "alerts.AlertRecipient",
            alert__uptime=True,
            alert__project=self.project,
            recipient_type="email",
        )

        mocked.get(test_url, status=200)
        with freeze_time("2020-01-01"):
            baker.make(
                Monitor,
                name=test_url,
                url=test_url,
                monitor_type=MonitorType.GET,
                project=self.project,
            )

        mocked.get(test_url, status=500, repeat=True)
        # cache.set(UPTIME_COUNTER_KEY, 59)
        with freeze_time("2020-01-02"):

            async def run_loop():
                for _ in range(60):
                    await dispatch_checks.func()

            async_to_sync(run_loop)()
        self.assertNotIn(user2.email, mail.outbox[0].to)
        self.assertIn(user3.email, mail.outbox[0].to)
        self.assertEqual(len(mail.outbox[0].to), 2)

    @aioresponses()
    def test_user_project_alert_scope(self, mocked):
        """User project alert should not result in alert emails for non-team members"""
        self.create_user_and_project()
        test_url = "https://example.com"
        baker.make(
            "alerts.AlertRecipient",
            alert__uptime=True,
            alert__project=self.project,
            recipient_type="email",
        )

        user2 = baker.make("users.user")
        self.organization.add_user(user2, OrganizationUserRole.MEMBER)

        baker.make(
            "projects.UserProjectAlert",
            user=user2,
            project=self.project,
            status=ProjectAlertStatus.ON,
        )

        mocked.get(test_url, status=200)
        with freeze_time("2020-01-01"):
            baker.make(
                Monitor,
                name=test_url,
                url=test_url,
                monitor_type=MonitorType.GET,
                project=self.project,
            )

        mocked.get(test_url, status=500, repeat=True)
        # cache.set(UPTIME_COUNTER_KEY, 59)
        with freeze_time("2020-01-02"):

            async def run_loop():
                for _ in range(60):
                    await dispatch_checks.func()

            async_to_sync(run_loop)()
        self.assertNotIn(user2.email, mail.outbox[0].to)

    def xtest_heartbeat(self):
        """
        Cannot run due to async code, it doesn't close the DB connection
        Run manually with --keepdb
        """
        self.create_user_and_project()
        with freeze_time("2020-01-01"):
            monitor = baker.make(
                Monitor,
                monitor_type=MonitorType.HEARTBEAT,
                project=self.project,
            )
            baker.make(
                "alerts.AlertRecipient",
                alert__uptime=True,
                alert__project=self.project,
                recipient_type="email",
            )
            url = reverse(
                "api:heartbeat_check",
                kwargs={
                    "organization_slug": monitor.organization.slug,
                    "endpoint_id": monitor.endpoint_id,
                },
            )
            self.assertFalse(monitor.checks.exists())
            self.client.post(url)
            self.assertTrue(monitor.checks.filter(is_up=True).exists())
            async_to_sync(dispatch_checks.func)()
        self.assertTrue(monitor.checks.filter(is_up=True).exists())
        self.assertEqual(len(mail.outbox), 0)

        # cache.set(UPTIME_COUNTER_KEY, 59)
        with freeze_time("2020-01-02"):

            async def run_loop():
                for _ in range(60):
                    await dispatch_checks.func()

            async_to_sync(run_loop)()
        self.assertEqual(len(mail.outbox), 1)

        # cache.set(UPTIME_COUNTER_KEY, 59)
        with freeze_time("2020-01-03"):
            async_to_sync(run_loop)()  # Still down
        self.assertEqual(len(mail.outbox), 1)

        # cache.set(UPTIME_COUNTER_KEY, 59)
        with freeze_time("2020-01-04"):
            self.client.post(url)  # Back up
        self.assertEqual(len(mail.outbox), 2)

    def test_heartbeat_grace_period(self):
        # Don't alert users when heartbeat check has never come in
        self.create_user_and_project()
        baker.make(Monitor, monitor_type=MonitorType.HEARTBEAT, project=self.project)
        async_to_sync(dispatch_checks.func)()
        self.assertEqual(len(mail.outbox), 0)

    def test_cached_fields_updated_on_save_monitor_checks(self):
        """cached_is_up and cached_last_change update after save_monitor_checks"""
        with freeze_time("2020-01-01"):
            mon = baker.make(
                Monitor, url="https://example.com", monitor_type=MonitorType.GET
            )
        self.assertIsNone(mon.cached_is_up)
        self.assertIsNone(mon.cached_last_change)

        now = datetime(2020, 1, 1, 12, 0, tzinfo=dt_timezone.utc)
        result = {
            "id": mon.id,
            "organization_id": mon.organization_id,
            "is_up": True,
            "latest_is_up": None,
            "last_change": None,
            "monitor_type": MonitorType.GET,
        }
        with freeze_time("2020-01-01"):
            async_to_sync(save_monitor_checks)([result], now)

        mon.refresh_from_db()
        self.assertTrue(mon.cached_is_up)
        self.assertEqual(mon.cached_last_change, now)

    def test_cached_fields_updated_on_state_change(self):
        """cached_last_change updates when is_up state changes"""
        with freeze_time("2020-01-01"):
            mon = baker.make(
                Monitor, url="https://example.com", monitor_type=MonitorType.GET
            )

        first_now = datetime(2020, 1, 1, 12, 0, tzinfo=dt_timezone.utc)
        result = {
            "id": mon.id,
            "organization_id": mon.organization_id,
            "is_up": True,
            "latest_is_up": None,
            "last_change": None,
            "monitor_type": MonitorType.GET,
        }
        with freeze_time("2020-01-01"):
            async_to_sync(save_monitor_checks)([result], first_now)

        mon.refresh_from_db()
        self.assertTrue(mon.cached_is_up)
        self.assertEqual(mon.cached_last_change, first_now)

        # Now simulate going down
        second_now = datetime(2020, 1, 2, 12, 0, tzinfo=dt_timezone.utc)
        result2 = {
            "id": mon.id,
            "organization_id": mon.organization_id,
            "is_up": False,
            "latest_is_up": True,
            "last_change": first_now,
            "monitor_type": MonitorType.GET,
        }
        with freeze_time("2020-01-02"):
            async_to_sync(save_monitor_checks)([result2], second_now)

        mon.refresh_from_db()
        self.assertFalse(mon.cached_is_up)
        self.assertEqual(mon.cached_last_change, second_now)

    def test_cached_fields_preserved_when_no_change(self):
        """cached_last_change is preserved when is_up state doesn't change"""
        with freeze_time("2020-01-01"):
            mon = baker.make(
                Monitor, url="https://example.com", monitor_type=MonitorType.GET
            )

        first_now = datetime(2020, 1, 1, 12, 0, tzinfo=dt_timezone.utc)
        result = {
            "id": mon.id,
            "organization_id": mon.organization_id,
            "is_up": True,
            "latest_is_up": None,
            "last_change": None,
            "monitor_type": MonitorType.GET,
        }
        with freeze_time("2020-01-01"):
            async_to_sync(save_monitor_checks)([result], first_now)

        # Same state, no change
        second_now = datetime(2020, 1, 2, 12, 0, tzinfo=dt_timezone.utc)
        result2 = {
            "id": mon.id,
            "organization_id": mon.organization_id,
            "is_up": True,
            "latest_is_up": True,
            "last_change": first_now,
            "monitor_type": MonitorType.GET,
        }
        with freeze_time("2020-01-02"):
            async_to_sync(save_monitor_checks)([result2], second_now)

        mon.refresh_from_db()
        self.assertTrue(mon.cached_is_up)
        # last_change should stay at first_now since no state change
        self.assertEqual(mon.cached_last_change, first_now)

    @mock.patch("apps.uptime.utils.asyncio.open_connection")
    def test_port_monitor(self, mocked):
        self.create_user_and_project()
        monitor = baker.make(
            Monitor,
            url="example.com:80",
            monitor_type=MonitorType.PORT,
            project=self.project,
        )
        mocked.assert_called_once()
        self.assertTrue(monitor.checks.filter(is_up=True).exists())

    # --- Flap tolerance (failure/recovery thresholds) ---

    @staticmethod
    def _transition_result(
        is_up,
        latest_is_up,
        *,
        last_change=None,
        monitor_type=MonitorType.GET,
        failure_threshold=1,
        recovery_threshold=1,
        consecutive_failures=0,
        consecutive_successes=0,
    ):
        return {
            "id": 1,
            "organization_id": 1,
            "is_up": is_up,
            "latest_is_up": latest_is_up,
            "last_change": last_change,
            "monitor_type": monitor_type,
            "failure_threshold": failure_threshold,
            "recovery_threshold": recovery_threshold,
            "consecutive_failures": consecutive_failures,
            "consecutive_successes": consecutive_successes,
        }

    def test_default_threshold_matches_single_check_behaviour(self):
        """With thresholds at 1, every result that differs from the cached
        status flips immediately and is treated as a transition."""
        # up -> down on a single failure
        r = self._transition_result(is_up=False, latest_is_up=True)
        apply_flap_tolerance(r)
        self.assertTrue(r["transitioned"])
        self.assertFalse(r["new_is_up"])
        # down -> up on a single success
        r = self._transition_result(is_up=True, latest_is_up=False)
        apply_flap_tolerance(r)
        self.assertTrue(r["transitioned"])
        self.assertTrue(r["new_is_up"])
        # steady state: no change, no transition
        r = self._transition_result(is_up=True, latest_is_up=True)
        apply_flap_tolerance(r)
        self.assertFalse(r["transitioned"])
        # first ever check (no cached status) is always a baseline change
        r = self._transition_result(is_up=True, latest_is_up=None)
        apply_flap_tolerance(r)
        self.assertTrue(r["transitioned"])

    def test_failure_threshold_requires_consecutive_failures(self):
        """failure_threshold=3 does not flip on 1 or 2 failures, flips on the 3rd."""
        failures = 0
        for expected_failures in (1, 2):
            r = self._transition_result(
                is_up=False,
                latest_is_up=True,
                failure_threshold=3,
                consecutive_failures=failures,
            )
            apply_flap_tolerance(r)
            self.assertFalse(r["transitioned"])
            self.assertTrue(r["new_is_up"])  # still up
            self.assertEqual(r["consecutive_failures"], expected_failures)
            failures = r["consecutive_failures"]

        r = self._transition_result(
            is_up=False,
            latest_is_up=True,
            failure_threshold=3,
            consecutive_failures=failures,
        )
        apply_flap_tolerance(r)
        self.assertTrue(r["transitioned"])
        self.assertFalse(r["new_is_up"])
        self.assertEqual(r["consecutive_failures"], 3)

    def test_recovery_threshold_requires_consecutive_successes(self):
        successes = 0
        for _ in range(2):
            r = self._transition_result(
                is_up=True,
                latest_is_up=False,
                recovery_threshold=3,
                consecutive_successes=successes,
            )
            apply_flap_tolerance(r)
            self.assertFalse(r["transitioned"])
            self.assertFalse(r["new_is_up"])  # still down
            successes = r["consecutive_successes"]

        r = self._transition_result(
            is_up=True,
            latest_is_up=False,
            recovery_threshold=3,
            consecutive_successes=successes,
        )
        apply_flap_tolerance(r)
        self.assertTrue(r["transitioned"])
        self.assertTrue(r["new_is_up"])

    def test_counters_reset_on_alternating_results(self):
        r = self._transition_result(
            is_up=True, latest_is_up=True, consecutive_failures=2
        )
        apply_flap_tolerance(r)
        self.assertEqual(r["consecutive_failures"], 0)
        self.assertEqual(r["consecutive_successes"], 1)

    def test_heartbeat_exempt_from_thresholds(self):
        """Heartbeats keep immediate transitions regardless of configured
        thresholds (their up results never reach this path)."""
        r = self._transition_result(
            is_up=False,
            latest_is_up=True,
            monitor_type=MonitorType.HEARTBEAT,
            failure_threshold=5,
        )
        apply_flap_tolerance(r)
        self.assertTrue(r["transitioned"])
        self.assertFalse(r["new_is_up"])

    def test_baseline_marks_is_change_without_transition(self):
        """When there is no prior change record (last_change is None) the check
        is marked is_change for re-baselining, but it is not a transition/alert."""
        r = self._transition_result(is_up=True, latest_is_up=True, last_change=None)
        apply_flap_tolerance(r)
        self.assertFalse(r["transitioned"])
        self.assertTrue(r["is_change"])

    def _run_check(self, mon, is_up, now):
        """Simulate one dispatch cycle: read the monitor's cached state +
        counters (as perform_checks does) and run a single check result."""
        mon.refresh_from_db()
        result = {
            "id": mon.id,
            "organization_id": mon.organization_id,
            "is_up": is_up,
            "latest_is_up": mon.cached_is_up,
            "last_change": mon.cached_last_change,
            "monitor_type": mon.monitor_type,
            "failure_threshold": mon.failure_threshold,
            "recovery_threshold": mon.recovery_threshold,
            "consecutive_failures": mon.consecutive_failures,
            "consecutive_successes": mon.consecutive_successes,
        }
        async_to_sync(save_monitor_checks)([result], now)

    @mock.patch("apps.uptime.tasks.perform_checks")
    def test_threshold_absorbs_transient_flap(self, _mocked):
        """Replay of the observed single-interval flap: with failure_threshold=3
        a lone failed check produces no status flip and no notification, while
        every check is still recorded for history."""
        self.create_user_and_project()
        with freeze_time("2020-01-01"):
            mon = baker.make(
                Monitor,
                url="https://example.com",
                monitor_type=MonitorType.GET,
                project=self.project,
                failure_threshold=3,
                recovery_threshold=1,
                consecutive_failures=0,
                consecutive_successes=0,
                cached_is_up=True,
                cached_last_change=datetime(2020, 1, 1, tzinfo=dt_timezone.utc),
            )
            baker.make(
                "alerts.AlertRecipient",
                alert__uptime=True,
                alert__project=self.project,
                recipient_type="email",
            )

            self._run_check(mon, is_up=False, now=timezone.now())  # transient blip
            self._run_check(mon, is_up=True, now=timezone.now())  # recovered

        mon.refresh_from_db()
        self.assertTrue(mon.cached_is_up)  # never flipped
        self.assertEqual(mon.consecutive_failures, 0)
        self.assertEqual(len(mail.outbox), 0)  # no false alert
        self.assertEqual(mon.checks.count(), 2)  # history intact

    @mock.patch("apps.uptime.tasks.perform_checks")
    def test_sustained_outage_alerts_once_each_way(self, _mocked):
        self.create_user_and_project()
        with freeze_time("2020-01-01"):
            mon = baker.make(
                Monitor,
                url="https://example.com",
                monitor_type=MonitorType.GET,
                project=self.project,
                failure_threshold=3,
                recovery_threshold=1,
                consecutive_failures=0,
                consecutive_successes=0,
                cached_is_up=True,
                cached_last_change=datetime(2020, 1, 1, tzinfo=dt_timezone.utc),
            )
            baker.make(
                "alerts.AlertRecipient",
                alert__uptime=True,
                alert__project=self.project,
                recipient_type="email",
            )

            for _ in range(3):  # sustained failure crosses the threshold
                self._run_check(mon, is_up=False, now=timezone.now())

        mon.refresh_from_db()
        self.assertFalse(mon.cached_is_up)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("is down", mail.outbox[0].body)

        with freeze_time("2020-01-02"):
            self._run_check(mon, is_up=True, now=timezone.now())  # recovery

        mon.refresh_from_db()
        self.assertTrue(mon.cached_is_up)
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("is back up", mail.outbox[1].body)

    @override_settings(
        GLITCHTIP_UPTIME_CHECK_RETRIES=2, GLITCHTIP_UPTIME_CHECK_RETRY_DELAY=0
    )
    @aioresponses()
    def test_in_check_retry_confirms_within_one_cycle(self, mocked):
        """A failed probe is re-tried within the same check; if a retry
        succeeds the check is recorded as up."""
        url = "https://example.com"
        mocked.get(url, status=500)  # first attempt fails
        mocked.get(url, status=200)  # retry succeeds
        monitor = {
            "id": 1,
            "organization_id": 1,
            "monitor_type": MonitorType.GET,
            "url": url,
            "timeout": 20,
            "expected_status": 200,
            "expected_body": "",
            "interval": 60,
            "latest_is_up": None,
        }

        async def run():
            async with aiohttp.ClientSession(**settings.AIOHTTP_CONFIG) as session:
                return await fetch_with_retries(session, monitor)

        result = async_to_sync(run)()
        self.assertTrue(result["is_up"])
        self.assertIsNone(result.get("reason"))  # no stale failure reason

    @override_settings(GLITCHTIP_UPTIME_CHECK_RETRIES=0)
    @aioresponses()
    def test_no_retry_when_disabled(self, mocked):
        url = "https://example.com"
        mocked.get(url, status=500)
        monitor = {
            "id": 1,
            "organization_id": 1,
            "monitor_type": MonitorType.GET,
            "url": url,
            "timeout": 20,
            "expected_status": 200,
            "expected_body": "",
            "interval": 60,
            "latest_is_up": None,
        }

        async def run():
            async with aiohttp.ClientSession(**settings.AIOHTTP_CONFIG) as session:
                return await fetch_with_retries(session, monitor)

        result = async_to_sync(run)()
        self.assertFalse(result["is_up"])

    @mock.patch("apps.uptime.tasks.perform_checks")
    def test_resync_cache_uses_confirmed_status(self, _mocked):
        """resync_monitor_cache derives cached_is_up from the last confirmed
        transition (is_change=True), not a later sub-threshold blip, and clears
        the counters."""
        with freeze_time("2020-01-01"):
            mon = baker.make(
                Monitor,
                url="https://example.com",
                monitor_type=MonitorType.GET,
                failure_threshold=3,
                consecutive_failures=2,
                cached_is_up=None,
            )
            # Confirmed "up" transition...
            baker.make(
                MonitorCheck,
                monitor=mon,
                organization=mon.organization,
                is_up=True,
                is_change=True,
                start_check=datetime(2020, 1, 1, 10, 0, tzinfo=dt_timezone.utc),
            )
            # ...followed by a transient failed check that did NOT transition.
            baker.make(
                MonitorCheck,
                monitor=mon,
                organization=mon.organization,
                is_up=False,
                is_change=False,
                start_check=datetime(2020, 1, 1, 11, 0, tzinfo=dt_timezone.utc),
            )

        call_command("resync_monitor_cache")

        mon.refresh_from_db()
        self.assertTrue(mon.cached_is_up)  # confirmed up, not the blip
        self.assertEqual(
            mon.cached_last_change,
            datetime(2020, 1, 1, 10, 0, tzinfo=dt_timezone.utc),
        )
        self.assertEqual(mon.consecutive_failures, 0)

    @mock.patch("apps.uptime.tasks.perform_checks")
    def test_resync_cache_falls_back_when_no_change_records(self, _mocked):
        """If retention pruned every is_change=True row on a long-stable
        monitor, resync must keep the last known status (not NULL, which would
        re-baseline and fire a spurious notification on the next check)."""
        with freeze_time("2020-01-01"):
            mon = baker.make(
                Monitor,
                url="https://example.com",
                monitor_type=MonitorType.GET,
                cached_is_up=None,
            )
            baker.make(
                MonitorCheck,
                monitor=mon,
                organization=mon.organization,
                is_up=True,
                is_change=False,
                start_check=datetime(2020, 1, 1, 9, 0, tzinfo=dt_timezone.utc),
            )

        call_command("resync_monitor_cache")

        mon.refresh_from_db()
        self.assertTrue(mon.cached_is_up)  # fell back to latest check
        self.assertIsNone(mon.cached_last_change)  # no confirmed transition known

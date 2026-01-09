from datetime import timedelta

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from model_bakery import baker

from apps.issue_events.models import IssueEvent
from glitchtip.tasks import downsample_old_events


class DownsampleTestCase(TransactionTestCase):
    def setUp(self):
        pass

    def tearDown(self):
        # Clean up partitions created during test
        with connection.cursor() as cursor:
            cursor.execute(
                'DROP TABLE IF EXISTS "issue_events_issueevent_p2025_11_29";'
            )  # Example date from previous run, but logic needs to be dynamic or we just drop what we created.
            # We will use dynamic names in the test, so we should clean them up there or here.
            # For simplicity, let's just drop the ones we know we create.
            # But wait, the previous run output showed `issue_events_issueevent_p2025_11_29`.
            # Let's rely on the test logic to define names.
            pass

    @override_settings(
        GLITCHTIP_EVENT_DOWNSAMPLE_DAYS=30, GLITCHTIP_MAX_EVENT_LIFE_DAYS=90
    )
    def test_downsample_old_events(self):
        now = timezone.now()
        # 40 days ago
        target_date = now - timedelta(days=40)
        # 5 days ago (should not be touched)
        recent_date = now - timedelta(days=5)

        # Prepare Partition Name for 40 days ago
        table_date_str = target_date.strftime("%Y_%m_%d")
        partition_name = f"issue_events_issueevent_p{table_date_str}"

        # Manually create the partition
        start_ts = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_ts = start_ts + timedelta(days=1)

        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass(%s)", [partition_name])
            if not cursor.fetchone()[0]:
                cursor.execute(f"""
                    CREATE TABLE \"{partition_name}\" PARTITION OF \"issue_events_issueevent\"
                    FOR VALUES FROM ('{start_ts.isoformat()}') TO ('{end_ts.isoformat()}');
                """)

        recent_table_date_str = recent_date.strftime("%Y_%m_%d")
        recent_partition_name = f"issue_events_issueevent_p{recent_table_date_str}"
        start_ts_recent = recent_date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_ts_recent = start_ts_recent + timedelta(days=1)

        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass(%s)", [recent_partition_name])
            if not cursor.fetchone()[0]:
                cursor.execute(f"""
                    CREATE TABLE \"{recent_partition_name}\" PARTITION OF \"issue_events_issueevent\"
                    FOR VALUES FROM ('{start_ts_recent.isoformat()}') TO ('{end_ts_recent.isoformat()}');
                """)

        try:
            # Create Issues
            org = baker.make(
                "organizations_ext.Organization", name="test-org", slug="test-org"
            )
            project = baker.make("projects.Project", organization=org)
            issue1 = baker.make("issue_events.Issue", project=project)
            issue2 = baker.make("issue_events.Issue", project=project)

            # Create Events in the target partition (40 days ago)
            # Event 1A: Older
            baker.make(
                "issue_events.IssueEvent",
                issue=issue1,
                received=target_date - timedelta(seconds=100),
            )
            # Event 1B: Newer (Should survive)
            baker.make("issue_events.IssueEvent", issue=issue1, received=target_date)

            # Event 2A: Only one (Should survive)
            baker.make("issue_events.IssueEvent", issue=issue2, received=target_date)

            # Create Events in recent partition
            baker.make("issue_events.IssueEvent", issue=issue1, received=recent_date)
            baker.make(
                "issue_events.IssueEvent",
                issue=issue1,
                received=recent_date - timedelta(seconds=10),
            )

            # Verify initial counts
            self.assertEqual(IssueEvent.objects.filter(issue=issue1).count(), 4)

            # Run Downsample
            downsample_old_events()

            # Check Results

            # Issue 1: 3 total
            self.assertEqual(IssueEvent.objects.filter(issue=issue1).count(), 3)

            # Issue 2: 1 total
            self.assertEqual(IssueEvent.objects.filter(issue=issue2).count(), 1)

            # Check if optimized comment is set
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT obj_description(%s::regclass, 'pg_class')", [partition_name]
                )
                comment = cursor.fetchone()[0]
                self.assertEqual(comment, "optimized")

                # Check recent partition is NOT optimized
                cursor.execute(
                    "SELECT obj_description(%s::regclass, 'pg_class')",
                    [recent_partition_name],
                )
                comment_recent = cursor.fetchone()[0]
                self.assertNotEqual(comment_recent, "optimized")

            # Run it again to ensure it skips (idempotency) and doesn't break things
            downsample_old_events()
            self.assertEqual(IssueEvent.objects.filter(issue=issue1).count(), 3)
        finally:
            with connection.cursor() as cursor:
                cursor.execute(f'DROP TABLE IF EXISTS "{partition_name}";')
                cursor.execute(f'DROP TABLE IF EXISTS "{recent_partition_name}";')

from datetime import datetime, timedelta, timezone

from django.db import connection
from django.test import TestCase, override_settings
from model_bakery import baker

from glitchtip.partition_manager import PartitionManager, UUID7Helper

from ..downsample import downsample_events
from ..models import IssueEvent

_UNSET = object()


class DownsampleTestCase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = PartitionManager()
        # Create an old partition for testing (60 days ago)
        cls.old_date = datetime.now(timezone.utc) - timedelta(days=60)
        cls.old_date = cls.old_date.replace(hour=0, minute=0, second=0, microsecond=0)
        cls.old_partition_name = (
            f"issue_events_issueevent_{cls.old_date.strftime('%Y%m%d')}"
        )

        if not cls.manager.table_exists(cls.old_partition_name):
            cls.manager.execute_partition_creation(
                parent_table="issue_events_issueevent",
                partition_name=cls.old_partition_name,
                start_date=cls.old_date,
                end_date=cls.old_date + timedelta(days=1),
                key_type="uuid7",
                hash_column="organization_id",
            )

    def _create_event(self, issue, timestamp=None, data=_UNSET):
        if timestamp is None:
            timestamp = self.old_date + timedelta(hours=1)
        if data is _UNSET:
            data = {"message": "test", "platform": "python"}
        event_id = UUID7Helper.from_datetime(timestamp)
        return IssueEvent.objects.create(
            id=event_id,
            issue=issue,
            organization=issue.project.organization,
            timestamp=timestamp,
            type=0,
            level=4,
            title="Test Event",
            transaction="",
            data=data,
            tags={},
        )

    @override_settings(GLITCHTIP_EVENT_DOWNSAMPLE_DAYS=30)
    def test_downsample_nulls_data(self):
        """Events in old partitions should have data nullified"""
        issue = baker.make("issue_events.Issue")
        issue.project.downsample_rate = 0.1
        issue.project.save()

        # Create multiple events for the same issue in the old partition
        events = []
        for i in range(10):
            ts = self.old_date + timedelta(hours=i + 1)
            events.append(self._create_event(issue, timestamp=ts))

        downsample_events()

        # At least some events should have data=NULL (statistically ~81% with rate=0.1)
        null_count = IssueEvent.objects.filter(
            id__in=[e.id for e in events], data__isnull=True
        ).count()
        self.assertGreater(null_count, 0, "Expected some events to be downsampled")

    @override_settings(GLITCHTIP_EVENT_DOWNSAMPLE_DAYS=30)
    def test_representative_keeps_data(self):
        """The newest event per issue should always retain data"""
        issue = baker.make("issue_events.Issue")
        issue.project.downsample_rate = 0.1
        issue.project.save()

        # Create events - the last one is newest (highest UUIDv7)
        for i in range(5):
            ts = self.old_date + timedelta(hours=i + 1)
            self._create_event(issue, timestamp=ts)

        newest_ts = self.old_date + timedelta(hours=10)
        newest = self._create_event(issue, timestamp=newest_ts)

        downsample_events()

        newest.refresh_from_db()
        self.assertIsNotNone(
            newest.data, "Newest event (representative) should keep data"
        )

    @override_settings(GLITCHTIP_EVENT_DOWNSAMPLE_DAYS=30)
    def test_rate_zero_skips_project(self):
        """Projects with downsample_rate=0.0 should be untouched"""
        issue = baker.make("issue_events.Issue")
        issue.project.downsample_rate = 0.0
        issue.project.save()

        events = []
        for i in range(5):
            ts = self.old_date + timedelta(hours=i + 1)
            events.append(self._create_event(issue, timestamp=ts))

        downsample_events()

        null_count = IssueEvent.objects.filter(
            id__in=[e.id for e in events], data__isnull=True
        ).count()
        self.assertEqual(null_count, 0, "No events should be nullified for rate=0.0")

    @override_settings(GLITCHTIP_EVENT_DOWNSAMPLE_DAYS=30)
    def test_partition_marked(self):
        """Partition should be marked as 'downsampled' after processing"""
        issue = baker.make("issue_events.Issue")
        self._create_event(issue)

        downsample_events()

        comment = self.manager.get_partition_comment(self.old_partition_name)
        self.assertEqual(comment, "downsampled")

    @override_settings(GLITCHTIP_EVENT_DOWNSAMPLE_DAYS=30)
    def test_already_downsampled_skipped(self):
        """Re-running should skip already-downsampled partitions"""
        issue = baker.make("issue_events.Issue")
        self._create_event(issue)

        # First run marks the partition
        downsample_events()

        # Create more events in the same partition
        new_event = self._create_event(
            issue, timestamp=self.old_date + timedelta(hours=12)
        )

        # Second run should skip the partition
        downsample_events()

        new_event.refresh_from_db()
        self.assertIsNotNone(
            new_event.data,
            "Events added after downsampling should not be touched on re-run",
        )

    @override_settings(GLITCHTIP_EVENT_DOWNSAMPLE_DAYS=0)
    def test_disabled_setting(self):
        """GLITCHTIP_EVENT_DOWNSAMPLE_DAYS=0 should do nothing"""
        issue = baker.make("issue_events.Issue")
        events = []
        for i in range(5):
            ts = self.old_date + timedelta(hours=i + 1)
            events.append(self._create_event(issue, timestamp=ts))

        downsample_events()

        null_count = IssueEvent.objects.filter(
            id__in=[e.id for e in events], data__isnull=True
        ).count()
        self.assertEqual(null_count, 0, "No events should be downsampled when disabled")

    @override_settings(GLITCHTIP_EVENT_DOWNSAMPLE_DAYS=30)
    def test_null_data_serialization(self):
        """Events with data=None should serialize without errors"""
        issue = baker.make("issue_events.Issue")
        event = self._create_event(issue, data=None)

        # Test model properties
        self.assertEqual(event.message, event.title)
        self.assertEqual(event.metadata, {"title": event.title})
        self.assertIsNone(event.platform)

        # Test schema serialization
        from ..schema import IssueEventSchema

        schema = IssueEventSchema.from_orm(event)
        self.assertEqual(schema.entries, [])
        self.assertIsNone(schema.contexts)
        self.assertIsNone(schema.context)
        self.assertIsNone(schema.user)
        self.assertIsNone(schema.sdk)

    @override_settings(GLITCHTIP_EVENT_DOWNSAMPLE_DAYS=30)
    def test_idempotent_data_null(self):
        """Events already with data=NULL should not be updated again"""
        issue = baker.make("issue_events.Issue")
        issue.project.downsample_rate = 0.1
        issue.project.save()

        # Create event with data already null
        event = self._create_event(issue, data=None)

        # Remove the downsampled comment so the partition is eligible
        with connection.cursor() as cursor:
            cursor.execute(f"COMMENT ON TABLE {self.old_partition_name} IS NULL")

        # This should not error
        downsample_events()

        event.refresh_from_db()
        self.assertIsNone(event.data)

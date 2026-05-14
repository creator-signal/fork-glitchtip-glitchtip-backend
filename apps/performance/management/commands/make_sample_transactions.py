from datetime import timedelta

from django.utils import timezone

from apps.performance.test_data import generate_fake_transaction_group
from glitchtip.base_commands import MakeSampleCommand
from glitchtip.partition_manager import PartitionManager


class Command(MakeSampleCommand):
    help = "Create sample transaction groups for dev and demonstration purposes"

    def _ensure_partitions(self, start_time: timezone.datetime, end_time: timezone.datetime):
        """Ensure partitions exist for the given time range."""
        manager = PartitionManager()

        # Weekly DateTime partitions
        start_of_week = start_time - timedelta(days=start_time.weekday())
        manager.create_partitions_for_date_range(
            parent_table="projects_transactioneventprojecthourlystatistic",
            start_date=start_of_week,
            end_date=end_time + timedelta(weeks=1),
            partition_interval="WEEK",
            hash_buckets=None,
            hash_column="organization_id",
            key_type="datetime",
        )

    def handle(self, *args, **options):
        super().handle(*args, **options)

        quantity = options["quantity"]
        total_count = 0

        now = timezone.now()
        self._ensure_partitions(now, now)

        for _ in range(quantity):
            group = generate_fake_transaction_group(self.project)
            total_count += group.count
            self.progress_tick()

        # Populate transaction stats with total count at current hour
        self.upsert_hourly_project_stats(
            "projects_transactioneventprojecthourlystatistic",
            [now] * total_count,
        )

        self.success_message('Successfully created "%s" transaction groups' % quantity)

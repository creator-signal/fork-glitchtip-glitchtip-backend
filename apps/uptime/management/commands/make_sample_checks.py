from datetime import timedelta
from random import randrange

from django.utils import timezone

from apps.uptime.models import Monitor, MonitorCheck
from glitchtip.base_commands import MakeSampleCommand
from glitchtip.partition_manager import PartitionManager


class Command(MakeSampleCommand):
    help = "Create a number of monitors each with checks for dev and demonstration purposes."

    def _ensure_partitions(self, start_time: timezone.datetime, end_time: timezone.datetime):
        """Ensure partitions exist for the given time range."""
        manager = PartitionManager()

        # Align to midnight
        start_date = start_time.replace(hour=0, minute=0, second=0, microsecond=0)
        end_date = end_time.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
            days=1
        )

        # Daily UUIDv7 partitions
        manager.create_partitions_for_date_range(
            parent_table="uptime_monitorcheck",
            start_date=start_date,
            end_date=end_date,
            partition_interval="DAY",
            hash_buckets=None,
            hash_column="organization_id",
            key_type="uuid7",
        )

        # Weekly DateTime partitions
        start_of_week = start_date - timedelta(days=start_date.weekday())
        manager.create_partitions_for_date_range(
            parent_table="uptime_uptimecheckhourlystatistic",
            start_date=start_of_week,
            end_date=end_date + timedelta(weeks=1),
            partition_interval="WEEK",
            hash_buckets=None,
            hash_column="organization_id",
            key_type="datetime",
        )

    def add_arguments(self, parser):
        self.add_org_project_arguments(parser)
        parser.add_argument("--monitor-quantity", type=int, default=10)
        parser.add_argument("--checks-quantity-per", type=int, default=100)
        parser.add_argument("--first-check-down", type=bool, default=False)

    def handle(self, *args, **options):
        super().handle(*args, **options)

        monitor_quantity = options["monitor_quantity"]
        checks_quantity_per = options["checks_quantity_per"]
        first_check_down = options["first_check_down"]

        monitors = [
            Monitor(
                project=self.project,
                name=f"Test Monitor #{i}",
                organization=self.organization,
                url="https://example.com",
                interval="60",
                monitor_type="Ping",
                expected_status="200",
            )
            for i in range(monitor_quantity)
        ]
        Monitor.objects.bulk_create(monitors)

        # Create checks sequentially based on time
        # Creates a better representation of data on disk
        now = timezone.now()
        start_time = now - timezone.timedelta(minutes=checks_quantity_per)
        self._ensure_partitions(start_time, now)

        checks = []
        for time_i in range(checks_quantity_per):
            for monitor in monitors:
                is_first = time_i == 0
                is_up = True
                if first_check_down and is_first:
                    is_up = False
                checks.append(
                    MonitorCheck(
                        monitor=monitor,
                        organization=self.organization,
                        is_up=is_up,
                        is_change=is_first,
                        start_check=start_time + timezone.timedelta(minutes=time_i),
                        response_time=randrange(1, 5000),
                    )
                )
            if len(checks) > 10000:
                MonitorCheck.objects.bulk_create(checks)
                self.progress_tick()
                checks = []
        if checks:
            MonitorCheck.objects.bulk_create(checks)

        self.success_message('Successfully created "%s" monitors' % monitor_quantity)

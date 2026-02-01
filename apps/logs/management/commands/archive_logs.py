"""
Management command to archive old log partitions to S3 cold storage.

Usage:
    # Check if pg_duckdb is available
    ./manage.py archive_logs --check

    # Archive partitions older than 7 days
    ./manage.py archive_logs --days 7

    # Archive a specific partition (for testing)
    ./manage.py archive_logs --partition logs_logevent_20260128_h0

    # Dry run - show what would be archived
    ./manage.py archive_logs --days 7 --dry-run
"""

from django.core.management.base import BaseCommand, CommandError

from apps.logs.cold_storage import (
    ColdStorageConfig,
    archive_and_swap_partition,
    archive_partition_to_s3,
    get_partitions_older_than,
    is_pg_duckdb_available,
    setup_duckdb_s3_credentials,
)


class Command(BaseCommand):
    help = "Archive old log partitions to S3 cold storage via pg_duckdb"

    def add_arguments(self, parser):
        parser.add_argument(
            "--check",
            action="store_true",
            help="Check if pg_duckdb is available",
        )
        parser.add_argument(
            "--days",
            type=int,
            default=None,
            help="Archive partitions older than this many days",
        )
        parser.add_argument(
            "--partition",
            type=str,
            default=None,
            help="Archive a specific partition by name",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be archived without doing it",
        )
        parser.add_argument(
            "--export-only",
            action="store_true",
            help="Only export to S3, don't swap partition with view",
        )

    def handle(self, *args, **options):
        if options["check"]:
            return self.check_duckdb()

        if not is_pg_duckdb_available():
            self.stderr.write(
                self.style.WARNING(
                    "pg_duckdb extension not available. "
                    "Cold storage features are disabled."
                )
            )
            return

        config = ColdStorageConfig.from_settings()
        self.stdout.write(f"Using bucket: {config.bucket}")
        if config.endpoint_url:
            self.stdout.write(f"Using endpoint: {config.endpoint_url}")

        if options["partition"]:
            return self.archive_single_partition(
                options["partition"],
                config,
                options["dry_run"],
                options["export_only"],
            )

        if options["days"]:
            return self.archive_old_partitions(
                options["days"],
                config,
                options["dry_run"],
                options["export_only"],
            )

        self.stderr.write(self.style.ERROR("Specify --check, --days, or --partition"))

    def check_duckdb(self):
        """Check if pg_duckdb is available."""
        if is_pg_duckdb_available():
            self.stdout.write(self.style.SUCCESS("pg_duckdb extension is available"))

            # Also test S3 connectivity
            config = ColdStorageConfig.from_settings()
            try:
                setup_duckdb_s3_credentials(config)
                self.stdout.write(
                    self.style.SUCCESS("S3 credentials configured successfully")
                )
            except Exception as e:
                self.stderr.write(self.style.WARNING(f"S3 configuration failed: {e}"))
        else:
            self.stdout.write(
                self.style.WARNING(
                    "pg_duckdb extension is NOT available. "
                    "Install it to enable cold storage features."
                )
            )

    def archive_single_partition(
        self,
        partition_name: str,
        config: ColdStorageConfig,
        dry_run: bool,
        export_only: bool,
    ):
        """Archive a single partition."""
        self.stdout.write(f"Archiving partition: {partition_name}")

        if dry_run:
            self.stdout.write(
                self.style.WARNING(f"[DRY RUN] Would archive {partition_name}")
            )
            return

        try:
            if export_only:
                s3_path = archive_partition_to_s3(partition_name, config=config)
                if s3_path:
                    self.stdout.write(self.style.SUCCESS(f"Exported to {s3_path}"))
            else:
                success = archive_and_swap_partition(partition_name, config=config)
                if success:
                    self.stdout.write(
                        self.style.SUCCESS(f"Archived and swapped {partition_name}")
                    )
                else:
                    self.stderr.write(
                        self.style.ERROR(f"Failed to archive {partition_name}")
                    )
        except Exception as e:
            raise CommandError(f"Archival failed: {e}")

    def archive_old_partitions(
        self,
        days: int,
        config: ColdStorageConfig,
        dry_run: bool,
        export_only: bool,
    ):
        """Archive all partitions older than specified days."""
        partitions = get_partitions_older_than("logs_logevent", days)

        if not partitions:
            self.stdout.write(f"No partitions older than {days} days found")
            return

        self.stdout.write(f"Found {len(partitions)} partitions to archive:")
        for name, date in partitions:
            self.stdout.write(f"  - {name} ({date.date()})")

        if dry_run:
            self.stdout.write(
                self.style.WARNING("[DRY RUN] No partitions were archived")
            )
            return

        archived = 0
        failed = 0

        for name, date in partitions:
            try:
                if export_only:
                    s3_path = archive_partition_to_s3(name, config=config)
                    if s3_path:
                        archived += 1
                        self.stdout.write(f"  ✓ Exported {name}")
                else:
                    if archive_and_swap_partition(name, config=config):
                        archived += 1
                        self.stdout.write(f"  ✓ Archived {name}")
                    else:
                        failed += 1
            except Exception as e:
                failed += 1
                self.stderr.write(f"  ✗ Failed {name}: {e}")

        self.stdout.write(
            self.style.SUCCESS(f"Archived {archived} partitions, {failed} failed")
        )

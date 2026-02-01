import random
from datetime import datetime, timedelta, timezone

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from apps.logs.constants import LogLevel
from apps.organizations_ext.models import Organization
from apps.projects.models import Project
from glitchtip.partition_manager import PartitionManager, UUID7Helper


class Command(BaseCommand):
    help = "Generate sample log events for testing"

    SAMPLE_MESSAGES = [
        "User login successful",
        "Database connection established",
        "Request processed in {ms}ms",
        "Cache miss for key: {key}",
        "API rate limit reached for user {user_id}",
        "Background job completed: {job_name}",
        "Email sent to {email}",
        "Payment processed: ${amount}",
        "File uploaded: {filename}",
        "Session expired for user {user_id}",
        "Configuration reloaded",
        "Health check passed",
        "Memory usage: {percent}%",
        "Queue length: {count} items",
        "Connection timeout to {service}",
        "Invalid request body received",
        "Authentication failed for user {username}",
        "Database query slow: {query_time}ms",
        "Certificate expiring in {days} days",
        "Deployment completed successfully",
    ]

    SAMPLE_SERVICES = [
        "api-gateway",
        "auth-service",
        "user-service",
        "payment-service",
        "notification-service",
        "worker",
        "scheduler",
        "cache-manager",
    ]

    def add_arguments(self, parser):
        parser.add_argument(
            "--quantity",
            "-n",
            type=int,
            default=100,
            help="Number of log events to generate (default: 100)",
        )
        parser.add_argument(
            "--organization",
            "-o",
            type=str,
            help="Organization slug (uses first organization if not specified)",
        )
        parser.add_argument(
            "--project",
            "-p",
            type=str,
            help="Project slug (uses first project in organization if not specified)",
        )
        parser.add_argument(
            "--days-ago",
            "-d",
            type=int,
            default=0,
            help="Generate logs starting this many days ago (default: 0, today)",
        )
        parser.add_argument(
            "--span-days",
            "-s",
            type=int,
            default=1,
            help="Distribute logs over this many days (default: 1)",
        )

    def handle(self, *args, **options):
        quantity = options["quantity"]
        days_ago = options["days_ago"]
        span_days = options["span_days"]

        # Get organization
        if options["organization"]:
            try:
                organization = Organization.objects.get(slug=options["organization"])
            except Organization.DoesNotExist:
                raise CommandError(
                    f"Organization '{options['organization']}' not found"
                )
        else:
            organization = Organization.objects.first()
            if not organization:
                organization, _ = Organization.objects.get_or_create(
                    slug="org",
                    defaults={"name": "Test Organization"},
                )
                self.stdout.write(f"Created default organization: {organization.slug}")

        # Get project
        if options["project"]:
            try:
                project = Project.objects.get(
                    slug=options["project"], organization=organization
                )
            except Project.DoesNotExist:
                raise CommandError(
                    f"Project '{options['project']}' not found in organization '{organization.slug}'"
                )
        else:
            project = Project.objects.filter(organization=organization).first()
            if not project:
                project = Project.objects.create(
                    name="Test Project",
                    slug="test-project",
                    organization=organization,
                )
                self.stdout.write(f"Created default project: {project.slug}")

        # Calculate time range
        now = datetime.now(timezone.utc)
        start_time = now - timedelta(days=days_ago + span_days)
        end_time = now - timedelta(days=days_ago)

        self.stdout.write(
            f"Generating {quantity} log events for {organization.slug}/{project.slug}"
        )
        self.stdout.write(f"Time range: {start_time} to {end_time}")

        # Ensure partitions exist for the date range
        self._ensure_partitions(start_time, end_time)

        # Generate logs using bulk insert for performance
        logs_created = self._bulk_create_logs(
            organization, project, quantity, start_time, end_time
        )

        self.stdout.write(
            self.style.SUCCESS(f"Successfully created {logs_created} log events")
        )

    def _ensure_partitions(self, start_time: datetime, end_time: datetime):
        """Ensure partitions exist for the given time range."""
        manager = PartitionManager()

        current = start_time.replace(hour=0, minute=0, second=0, microsecond=0)
        end = end_time.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
            days=1
        )

        while current <= end:
            partition_name = f"logs_logevent_{current.strftime('%Y%m%d')}"
            if not manager.table_exists(partition_name):
                self.stdout.write(f"Creating partition {partition_name}")
                next_day = current + timedelta(days=1)
                manager.execute_partition_creation(
                    parent_table="logs_logevent",
                    partition_name=partition_name,
                    start_date=current,
                    end_date=next_day,
                    hash_buckets=None,
                    hash_column="organization_id",
                    key_type="uuid7",
                )
            current += timedelta(days=1)

    def _bulk_create_logs(
        self,
        organization,
        project,
        quantity: int,
        start_time: datetime,
        end_time: datetime,
    ) -> int:
        """Bulk create log events using raw SQL for performance."""
        import orjson

        time_range_seconds = int((end_time - start_time).total_seconds())

        # Level weights
        level_weights = [
            (LogLevel.TRACE, 5),
            (LogLevel.DEBUG, 10),
            (LogLevel.INFO, 40),
            (LogLevel.WARN, 25),
            (LogLevel.ERROR, 15),
            (LogLevel.FATAL, 5),
        ]
        levels = [lw[0] for lw in level_weights]
        weights = [lw[1] for lw in level_weights]

        rows = []
        for i in range(quantity):
            # Random timestamp in range
            random_offset = timedelta(seconds=random.randint(0, time_range_seconds))
            log_timestamp = start_time + random_offset

            # Generate UUIDv7 from timestamp
            log_id = UUID7Helper.from_datetime(log_timestamp)

            # Random level
            level = random.choices(levels, weights=weights)[0]

            # Random message
            message_template = random.choice(self.SAMPLE_MESSAGES)
            message = message_template.format(
                ms=random.randint(10, 500),
                key=f"user:{random.randint(1000, 9999)}",
                user_id=random.randint(1, 1000),
                job_name=f"process_batch_{random.randint(1, 100)}",
                email=f"user{random.randint(1, 100)}@example.com",
                amount=random.randint(10, 1000),
                filename=f"upload_{random.randint(1000, 9999)}.pdf",
                username=f"user{random.randint(1, 100)}",
                percent=random.randint(50, 99),
                count=random.randint(1, 100),
                service=random.choice(self.SAMPLE_SERVICES),
                query_time=random.randint(100, 5000),
                days=random.randint(1, 30),
            )

            # Random service
            service = random.choice(self.SAMPLE_SERVICES)

            # Random trace_id (50% chance)
            trace_id = None
            if random.random() > 0.5:
                trace_id = str(UUID7Helper.from_datetime(log_timestamp))

            # Data
            data = orjson.dumps(
                {
                    "environment": random.choice(
                        ["production", "staging", "development"]
                    ),
                    "host": f"server-{random.randint(1, 10)}.example.com",
                }
            ).decode("utf-8")

            rows.append(
                (
                    str(log_id),
                    trace_id,
                    organization.id,
                    project.id,
                    None,  # span_id
                    level,
                    None,  # severity_number
                    message,
                    service,
                    data,
                )
            )

            if (i + 1) % 1000 == 0:
                self.stdout.write(f"  Prepared {i + 1}/{quantity} logs...")

        # Bulk insert
        insert_sql = """
            INSERT INTO logs_logevent (
                id, trace_id,
                organization_id, project_id, span_id,
                level, severity_number,
                body, service, data
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING;
        """

        self.stdout.write(f"Inserting {len(rows)} logs...")
        with connection.cursor() as cursor:
            cursor.executemany(insert_sql, rows)

        return len(rows)

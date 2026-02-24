import random
import uuid
from datetime import timedelta

from allauth.account.models import EmailAddress
from django.contrib.postgres.search import SearchVector
from django.db import connection
from django.db.models import Value
from django.utils import timezone

from apps.alerts.models import AlertRecipient, ProjectAlert
from apps.environments.models import Environment, EnvironmentProject
from apps.issue_events.models import (
    Comment,
    Issue,
    IssueAggregate,
    IssueEvent,
    IssueHash,
    IssueTag,
    TagKey,
    TagValue,
    UserReport,
)
from apps.logs.constants import LogLevel as LogsLogLevel
from apps.logs.models import LogResource
from apps.organizations_ext.models import Organization
from apps.performance.models import (
    TransactionEvent,
    TransactionGroup,
    TransactionGroupAggregate,
)
from apps.projects.models import Project
from apps.releases.models import Deploy, Release
from apps.teams.models import Team
from apps.uptime.models import Monitor, MonitorCheck
from apps.users.models import User
from glitchtip.base_commands import MakeSampleCommand
from glitchtip.partition_manager import PartitionManager, UUID7Helper

from .screenshot_data import (
    PROJECT_ISSUES,
    SCREENSHOT_ALERTS,
    SCREENSHOT_COMMENTS,
    SCREENSHOT_ENVIRONMENTS,
    SCREENSHOT_LOG_MESSAGES,
    SCREENSHOT_LOG_SERVICES,
    SCREENSHOT_MONITORS,
    SCREENSHOT_ORG_NAME,
    SCREENSHOT_ORG_SLUG,
    SCREENSHOT_PROJECTS,
    SCREENSHOT_RELEASES,
    SCREENSHOT_TEAMS,
    SCREENSHOT_TRANSACTIONS,
    SCREENSHOT_USER_REPORTS,
    SCREENSHOT_USERS,
)


class Command(MakeSampleCommand):
    help = "Create curated, realistic data for product screenshots"

    def add_arguments(self, parser):
        parser.add_argument(
            "--clean",
            action="store_true",
            help="Delete existing screenshot data before creating new data",
        )

    def handle(self, *args, **options):
        if options["clean"]:
            self._clean_previous_data()

        now = timezone.now()
        start_time = now - timedelta(days=45)

        self.stdout.write("Ensuring partitions exist...")
        self._ensure_partitions(start_time, now)

        self.stdout.write("Creating organization and users...")
        org, users_map = self._create_org_and_users()

        self.stdout.write("Creating environments...")
        environments = self._create_environments(org)

        self.stdout.write("Creating projects and teams...")
        projects = self._create_projects(org, environments)
        self._create_teams(org, projects, users_map)

        self.stdout.write("Creating releases...")
        releases = self._create_releases(org, projects)

        self.stdout.write("Creating issues and events...")
        issue_timestamps = self._create_issues(org, projects, releases, users_map)

        self.stdout.write("Creating issue aggregates...")
        self._create_issue_aggregates(org, issue_timestamps)

        self.stdout.write("Creating transactions...")
        self._create_transactions(org, projects, releases, environments)

        self.stdout.write("Creating logs...")
        self._create_logs(org, projects)

        self.stdout.write("Creating uptime monitors...")
        self._create_monitors(org, projects)

        self.stdout.write("Creating alerts...")
        self._create_alerts(projects)

        self.stdout.write(
            self.style.SUCCESS(
                f"\nScreenshot data created for org '{SCREENSHOT_ORG_SLUG}'!\n"
                f"Login: {SCREENSHOT_USERS[0]['email']} / screenshot"
            )
        )

    def _clean_previous_data(self):
        try:
            org = Organization.objects.get(slug=SCREENSHOT_ORG_SLUG)
        except Organization.DoesNotExist:
            self.stdout.write("No existing screenshot data found.")
            return

        self.stdout.write("Cleaning previous screenshot data...")

        # Delete partitioned table rows first (faster than cascade)
        IssueAggregate.objects.filter(organization=org).delete()
        IssueEvent.objects.filter(organization=org).delete()
        TransactionGroupAggregate.objects.filter(organization=org).delete()
        TransactionEvent.objects.filter(organization=org).delete()
        MonitorCheck.objects.filter(organization=org).delete()
        # LogEvent uses raw SQL since it's not a standard Django model for deletes
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM logs_logevent WHERE organization_id = %s", [org.id]
            )

        # Delete users
        user_emails = [u["email"] for u in SCREENSHOT_USERS]
        User.objects.filter(email__in=user_emails).delete()

        # Force delete org (real delete, cascades remaining FKs)
        org.force_delete()
        self.stdout.write("Previous screenshot data cleaned.")

    def _ensure_partitions(self, start_time, end_time):
        """Ensure partitions exist for the date range, skipping existing ones."""
        from django.db import transaction
        from django.db.utils import ProgrammingError

        manager = PartitionManager()

        # Daily partitions for event tables (uuid7 key)
        uuid7_tables = [
            "issue_events_issueevent",
            "performance_transactionevent",
            "uptime_monitorcheck",
            "logs_logevent",
        ]
        current = start_time.replace(hour=0, minute=0, second=0, microsecond=0)
        end = end_time.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
            days=1
        )
        while current <= end:
            for table in uuid7_tables:
                partition_name = f"{table}_{current.strftime('%Y%m%d')}"
                if not manager.table_exists(partition_name):
                    next_day = current + timedelta(days=1)
                    try:
                        with transaction.atomic():
                            manager.execute_partition_creation(
                                parent_table=table,
                                partition_name=partition_name,
                                start_date=current,
                                end_date=next_day,
                                hash_column="organization_id",
                                key_type="uuid7",
                            )
                    except ProgrammingError:
                        # Partition range covered by existing partitions
                        pass
            current += timedelta(days=1)

        # Weekly partitions for aggregate tables (datetime key)
        agg_tables = [
            "issue_events_issuetag",
            "issue_events_issueaggregate",
            "performance_transactiongroupaggregate",
        ]
        start_of_week = start_time - timedelta(days=start_time.weekday())
        current = start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)
        end = end_time + timedelta(weeks=1)
        while current < end:
            next_week = current + timedelta(weeks=1)
            for table in agg_tables:
                partition_name = f"{table}_{current.strftime('%Y%m%d')}"
                if not manager.table_exists(partition_name):
                    try:
                        with transaction.atomic():
                            manager.execute_partition_creation(
                                parent_table=table,
                                partition_name=partition_name,
                                start_date=current,
                                end_date=next_week,
                                hash_column="organization_id",
                                key_type="datetime",
                            )
                    except ProgrammingError:
                        pass
            current = next_week

    def _create_org_and_users(self):
        users_map = {}

        for i, user_data in enumerate(SCREENSHOT_USERS):
            user, created = User.objects.get_or_create(
                email=user_data["email"],
                defaults={
                    "name": user_data["name"],
                },
            )
            if created:
                user.set_password("screenshot")
                user.save()
            EmailAddress.objects.get_or_create(
                user=user,
                email=user.email,
                defaults={"primary": True, "verified": True},
            )
            users_map[user_data["email"]] = user

        org, _ = Organization.objects.get_or_create(
            slug=SCREENSHOT_ORG_SLUG,
            defaults={"name": SCREENSHOT_ORG_NAME},
        )

        # Add users with roles. First user becomes owner automatically.
        for user_data in SCREENSHOT_USERS:
            user = users_map[user_data["email"]]
            if not org.users.filter(pk=user.pk).exists():
                org.add_user(user, role=user_data["role"])

        return org, users_map

    def _create_environments(self, org):
        env_map = {}
        for env_name in SCREENSHOT_ENVIRONMENTS:
            env, _ = Environment.objects.get_or_create(organization=org, name=env_name)
            env_map[env_name] = env
        return env_map

    def _create_projects(self, org, environments):
        projects = {}
        for proj_data in SCREENSHOT_PROJECTS:
            project, _ = Project.objects.get_or_create(
                slug=proj_data["name"],
                organization=org,
                defaults={
                    "name": proj_data["name"],
                    "platform": proj_data["platform"],
                    "first_event": timezone.now() - timedelta(days=45),
                },
            )
            projects[proj_data["name"]] = project

            # Link environments to project
            for env in environments.values():
                EnvironmentProject.objects.get_or_create(
                    project=project, environment=env
                )

        return projects

    def _create_teams(self, org, projects, users_map):
        for team_data in SCREENSHOT_TEAMS:
            team, _ = Team.objects.get_or_create(
                slug=team_data["slug"], organization=org
            )
            # Add members
            for email in team_data["member_emails"]:
                user = users_map[email]
                org_user = org.organization_users.get(user=user)
                team.members.add(org_user)
            # Add projects
            for slug in team_data["project_slugs"]:
                team.projects.add(projects[slug])

    def _create_releases(self, org, projects):
        now = timezone.now()
        releases = {}
        deploy_envs = ["production", "staging"]

        for rel_data in SCREENSHOT_RELEASES:
            released_at = now - timedelta(days=rel_data["days_ago"])
            release, _ = Release.objects.get_or_create(
                organization=org,
                version=rel_data["version"],
                defaults={
                    "released": released_at,
                    "deploy_count": len(deploy_envs),
                    "commit_count": random.randint(3, 25),
                },
            )

            # Link to projects
            for proj_slug in rel_data["projects"]:
                release.projects.add(projects[proj_slug])

            # Create deploys
            for env_name in deploy_envs:
                Deploy.objects.get_or_create(
                    release=release,
                    environment=env_name,
                    defaults={
                        "date_finished": released_at,
                        "date_started": released_at - timedelta(minutes=5),
                    },
                )

            releases[rel_data["version"]] = release

        return releases

    def _generate_event_timestamps(self, now, days_span, event_count):
        """Generate timestamps with recent bias for natural-looking charts."""
        timestamps = []
        start = now - timedelta(days=days_span)

        for _ in range(event_count):
            # Weighted toward recent: use exponential distribution
            # Lower values = more recent events
            r = random.expovariate(3.0)  # lambda=3 biases toward 0
            fraction = min(r / 2.0, 1.0)  # Clamp to [0, 1], most values near 0
            # fraction=0 means now, fraction=1 means start
            ts = now - timedelta(seconds=fraction * days_span * 86400)
            # Clamp to range
            if ts < start:
                ts = start + timedelta(seconds=random.randint(0, 3600))
            timestamps.append(ts)

        timestamps.sort()
        return timestamps

    def _create_issues(self, org, projects, releases, users_map):
        now = timezone.now()
        all_tag_keys = set()
        all_tag_values = set()
        short_id_counters = {}
        issue_timestamps = []  # Collect (issue, timestamps) for aggregates

        for proj_slug, issue_defs in PROJECT_ISSUES.items():
            project = projects[proj_slug]
            short_id_counters[proj_slug] = 0

            for issue_def in issue_defs:
                short_id_counters[proj_slug] += 1
                event_count = issue_def["event_count"]
                days_span = issue_def["days_span"]

                timestamps = self._generate_event_timestamps(
                    now, days_span, event_count
                )
                first_seen = timestamps[0]
                last_seen = timestamps[-1]

                # Resolve release FK if tag has one
                release_version = issue_def["tags"].get("release")
                release = releases.get(release_version) if release_version else None

                issue = Issue.objects.create(
                    project=project,
                    title=issue_def["title"],
                    culprit=issue_def["culprit"],
                    level=issue_def["level"],
                    status=issue_def["status"],
                    count=event_count,
                    first_seen=first_seen,
                    last_seen=last_seen,
                    metadata={"title": issue_def["title"]},
                    search_vector=SearchVector(Value(issue_def["title"])),
                    short_id=short_id_counters[proj_slug],
                    first_release=release,
                    last_release=release,
                )

                issue_timestamps.append((issue, timestamps))

                # Create IssueHash
                hash_val = uuid.uuid5(uuid.NAMESPACE_OID, issue_def["title"])
                IssueHash.objects.get_or_create(
                    issue=issue, project=project, value=hash_val
                )

                # Create events
                tags = issue_def["tags"]
                all_tag_keys.update(tags.keys())
                all_tag_values.update(tags.values())

                events = []
                for ts in timestamps:
                    received = ts + timedelta(milliseconds=1)
                    events.append(
                        IssueEvent(
                            id=UUID7Helper.from_datetime(received),
                            issue=issue,
                            organization=org,
                            level=issue_def["level"],
                            title=issue_def["title"],
                            transaction=issue_def["culprit"],
                            timestamp=ts,
                            tags=tags,
                            release=release,
                            data={
                                "title": issue_def["title"],
                                "culprit": issue_def["culprit"],
                                "sdk": issue_def["sdk"],
                                "exception": issue_def["exception"],
                            },
                        )
                    )

                # Bulk create in batches
                batch_size = 5000
                for i in range(0, len(events), batch_size):
                    IssueEvent.objects.bulk_create(events[i : i + batch_size])

                # Create IssueTags (aggregate per tag key/value)
                self._create_issue_tags(issue, org, tags, last_seen)

                self.progress_tick()

        # Ensure TagKey/TagValue rows exist
        TagKey.objects.bulk_create(
            [TagKey(key=k) for k in all_tag_keys], ignore_conflicts=True
        )
        TagValue.objects.bulk_create(
            [TagValue(value=v) for v in all_tag_values], ignore_conflicts=True
        )

        # Create comments and user reports
        self._create_comments(projects, users_map)
        self._create_user_reports(projects)

        return issue_timestamps

    def _create_issue_tags(self, issue, org, tags, last_seen):
        """Create IssueTag aggregates for an issue's tags."""
        # Ensure tag keys/values exist
        for key, value in tags.items():
            tag_key, _ = TagKey.objects.get_or_create(key=key)
            tag_value, _ = TagValue.objects.get_or_create(value=value)

            tag_count = max(issue.count // 10, 1)
            issue_tags = []
            for _ in range(tag_count):
                tag_date = last_seen - timedelta(
                    minutes=random.randint(0, 60),
                    seconds=random.randint(0, 60),
                    milliseconds=random.randint(0, 1000),
                )
                issue_tags.append(
                    IssueTag(
                        issue=issue,
                        organization=org,
                        date=tag_date,
                        tag_key=tag_key,
                        tag_value=tag_value,
                        count=tag_count,
                    )
                )
            IssueTag.objects.bulk_create(issue_tags)

    def _create_issue_aggregates(self, org, issue_timestamps):
        """Create IssueAggregate rows (hourly counts) for trend charts."""
        from collections import defaultdict

        aggregates = []
        for issue, timestamps in issue_timestamps:
            # Bucket timestamps into hourly slots
            hourly_counts = defaultdict(int)
            for ts in timestamps:
                hour_slot = ts.replace(minute=0, second=0, microsecond=0)
                hourly_counts[hour_slot] += 1

            for hour_slot, count in hourly_counts.items():
                aggregates.append(
                    IssueAggregate(
                        issue=issue,
                        organization=org,
                        date=hour_slot,
                        count=count,
                    )
                )

        IssueAggregate.objects.bulk_create(aggregates, ignore_conflicts=True)

    def _create_comments(self, projects, users_map):
        for comment_data in SCREENSHOT_COMMENTS:
            project = projects[comment_data["project"]]
            user = users_map[comment_data["user_email"]]
            try:
                issue = Issue.objects.filter(
                    project=project,
                    title__startswith=comment_data["issue_title_prefix"],
                ).first()
                if issue:
                    Comment.objects.create(
                        issue=issue,
                        user=user,
                        text=comment_data["text"],
                    )
            except Issue.DoesNotExist:
                pass

    def _create_user_reports(self, projects):
        for report_data in SCREENSHOT_USER_REPORTS:
            project = projects[report_data["project"]]
            issue = Issue.objects.filter(
                project=project,
                title__startswith=report_data["issue_title_prefix"],
            ).first()
            if issue:
                # Get a real event ID from this issue
                event = (
                    IssueEvent.objects.filter(
                        issue=issue, organization=issue.project.organization
                    )
                    .values_list("id", flat=True)
                    .first()
                )
                if event:
                    UserReport.objects.get_or_create(
                        project=project,
                        event_id=event,
                        defaults={
                            "issue": issue,
                            "name": report_data["name"],
                            "email": report_data["email"],
                            "comments": report_data["comments"],
                        },
                    )

    def _create_transactions(self, org, projects, releases, environments):
        from collections import defaultdict

        now = timezone.now()
        release_versions = [r["version"] for r in SCREENSHOT_RELEASES[-3:]]
        env_names = SCREENSHOT_ENVIRONMENTS[:2]  # production, staging
        all_aggregates = []

        for txn_data in SCREENSHOT_TRANSACTIONS:
            project = projects[txn_data["project"]]
            group, _ = TransactionGroup.objects.get_or_create(
                project=project,
                transaction=txn_data["transaction"],
                op=txn_data["op"],
                method=txn_data["method"],
            )

            events = []
            # Track hourly stats for aggregates
            hourly_counts = defaultdict(int)
            hourly_durations = defaultdict(int)
            hourly_sq_durations = defaultdict(int)

            for _ in range(txn_data["count"]):
                # Time distribution: spread over 14 days with recent bias
                r = random.expovariate(2.0)
                fraction = min(r / 2.0, 1.0)
                start_ts = now - timedelta(seconds=fraction * 14 * 86400)

                # Duration: 90% base, 10% outlier
                if random.random() < 0.1:
                    duration = int(
                        txn_data["outlier_duration"] * random.uniform(0.9, 1.2)
                    )
                else:
                    duration = int(txn_data["base_duration"] * random.uniform(0.8, 1.4))

                end_ts = start_ts + timedelta(milliseconds=duration)
                received = end_ts + timedelta(milliseconds=1)

                # Bucket into hourly slot for aggregates
                hour_slot = start_ts.replace(minute=0, second=0, microsecond=0)
                hourly_counts[hour_slot] += 1
                hourly_durations[hour_slot] += duration
                hourly_sq_durations[hour_slot] += duration * duration

                tags = {}
                if random.random() < 0.8:
                    tags["release"] = random.choice(release_versions)
                if random.random() < 0.9:
                    tags["environment"] = random.choice(env_names)

                events.append(
                    TransactionEvent(
                        id=UUID7Helper.from_datetime(received),
                        event_id=uuid.uuid4(),
                        trace_id=uuid.uuid4(),
                        group=group,
                        organization=org,
                        start_timestamp=start_ts,
                        timestamp=end_ts,
                        duration=duration,
                        tags=tags,
                        data={},
                    )
                )

            # Bulk create events
            for i in range(0, len(events), 5000):
                TransactionEvent.objects.bulk_create(events[i : i + 5000])

            # Collect aggregates for this group
            for hour_slot, count in hourly_counts.items():
                all_aggregates.append(
                    TransactionGroupAggregate(
                        group=group,
                        organization=org,
                        date=hour_slot,
                        count=count,
                        total_duration=hourly_durations[hour_slot],
                        sum_of_squares_duration=hourly_sq_durations[hour_slot],
                    )
                )

            self.progress_tick()

        TransactionGroupAggregate.objects.bulk_create(
            all_aggregates, ignore_conflicts=True
        )

    def _create_logs(self, org, projects):
        import orjson

        now = timezone.now()
        quantity = 800
        time_range_seconds = int(timedelta(days=14).total_seconds())

        level_weights = [
            (LogsLogLevel.DEBUG, 5),
            (LogsLogLevel.INFO, 50),
            (LogsLogLevel.WARN, 25),
            (LogsLogLevel.ERROR, 15),
            (LogsLogLevel.FATAL, 5),
        ]
        levels = [lw[0] for lw in level_weights]
        weights = [lw[1] for lw in level_weights]

        # Build list of all services/hosts across projects
        all_services = []
        all_hosts = []
        for proj_slug, svc_data in SCREENSHOT_LOG_SERVICES.items():
            if proj_slug in projects:
                project = projects[proj_slug]
                for svc in svc_data["services"]:
                    all_services.append((svc, project))
                for host in svc_data["hosts"]:
                    all_hosts.append(host)

        rows = []
        for _ in range(quantity):
            # Recent bias
            r = random.expovariate(2.0)
            fraction = min(r / 2.0, 1.0)
            log_timestamp = now - timedelta(seconds=fraction * time_range_seconds)

            log_id = UUID7Helper.from_datetime(log_timestamp)
            level = random.choices(levels, weights=weights)[0]

            # Pick level-appropriate message
            level_name = level.label.lower()
            if level_name in SCREENSHOT_LOG_MESSAGES:
                messages = SCREENSHOT_LOG_MESSAGES[level_name]
            else:
                messages = SCREENSHOT_LOG_MESSAGES["info"]

            message_template = random.choice(messages)
            message = message_template.format(
                ms=random.randint(10, 500),
                user_id=random.randint(1000, 9999),
                order_id=random.randint(10000, 99999),
                cache_key=f"session:{random.randint(1000, 9999)}",
                host=random.choice(all_hosts),
                query_time=random.randint(100, 5000),
                percent=random.randint(70, 98),
                attempt=random.randint(1, 3),
                days=random.randint(5, 30),
                count=random.randint(50, 950),
                session_id=f"{random.randint(100000, 999999)}",
                product_id=random.randint(100, 9999),
            )

            service, project = random.choice(all_services)
            environment = random.choice(SCREENSHOT_ENVIRONMENTS[:2])
            host = random.choice(all_hosts)

            trace_id = None
            if random.random() > 0.5:
                trace_id = str(UUID7Helper.from_datetime(log_timestamp))

            data = orjson.dumps(
                {
                    "request_id": f"req-{random.randint(10000, 99999)}",
                    "user_id": random.randint(1, 1000),
                }
            ).decode("utf-8")

            rows.append(
                (
                    str(log_id),
                    trace_id,
                    org.id,
                    project.id,
                    None,  # span_id
                    level,
                    None,  # severity_number
                    message,
                    service,
                    environment,
                    host,
                    data,
                )
            )

        insert_sql = """
            INSERT INTO logs_logevent (
                id, trace_id,
                organization_id, project_id, span_id,
                level, severity_number,
                body, service, environment, host, data
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING;
        """

        with connection.cursor() as cursor:
            cursor.executemany(insert_sql, rows)

        # Create LogResource entries
        unique_services = set()
        unique_hosts = set()
        for proj_slug, svc_data in SCREENSHOT_LOG_SERVICES.items():
            unique_services.update(svc_data["services"])
            unique_hosts.update(svc_data["hosts"])

        for name in unique_services:
            LogResource.objects.update_or_create(
                organization=org,
                name=name,
                type=LogResource.ResourceType.SERVICE,
            )
        for name in SCREENSHOT_ENVIRONMENTS[:2]:
            LogResource.objects.update_or_create(
                organization=org,
                name=name,
                type=LogResource.ResourceType.ENVIRONMENT,
            )
        for name in unique_hosts:
            LogResource.objects.update_or_create(
                organization=org,
                name=name,
                type=LogResource.ResourceType.HOST,
            )

    def _create_monitors(self, org, projects):
        now = timezone.now()

        for mon_data in SCREENSHOT_MONITORS:
            project = projects[mon_data["project"]]
            # Use bulk_create to bypass Monitor.save() which enqueues real
            # uptime checks against our fake URLs via perform_checks task.
            monitor = Monitor.objects.filter(
                organization=org, name=mon_data["name"]
            ).first()
            if not monitor:
                monitor = Monitor(
                    organization=org,
                    project=project,
                    name=mon_data["name"],
                    url=mon_data["url"],
                    interval=mon_data["interval"],
                    monitor_type=mon_data["monitor_type"],
                )
                if mon_data["expected_status"] is not None:
                    monitor.expected_status = mon_data["expected_status"]
                Monitor.objects.bulk_create([monitor])
                monitor = Monitor.objects.get(
                    organization=org, name=mon_data["name"]
                )

            # Generate checks over 7 days
            check_interval = timedelta(seconds=mon_data["interval"])
            checks_start = now - timedelta(days=7)
            current_time = checks_start

            checks = []
            is_first = True

            # For unhealthy monitor, define a downtime window
            down_start = None
            down_end = None
            if not mon_data["is_healthy"]:
                down_start = now - timedelta(hours=2, minutes=15)
                down_end = now  # Currently down

            prev_is_up = True
            while current_time <= now:
                is_up = True
                if down_start and down_end:
                    if down_start <= current_time <= down_end:
                        is_up = False

                is_change = is_first or (is_up != prev_is_up)

                response_time = None
                if is_up:
                    response_time = random.randint(30, 300)
                    # Occasional spikes
                    if random.random() < 0.05:
                        response_time = random.randint(500, 2000)

                checks.append(
                    MonitorCheck(
                        id=UUID7Helper.from_datetime(current_time),
                        monitor=monitor,
                        organization=org,
                        is_up=is_up,
                        is_change=is_change,
                        start_check=current_time,
                        response_time=response_time,
                    )
                )

                prev_is_up = is_up
                is_first = False
                current_time += check_interval

                if len(checks) >= 5000:
                    MonitorCheck.objects.bulk_create(checks)
                    checks = []

            if checks:
                MonitorCheck.objects.bulk_create(checks)

            self.progress_tick()

    def _create_alerts(self, projects):
        for alert_data in SCREENSHOT_ALERTS:
            project = projects[alert_data["project"]]
            alert, _ = ProjectAlert.objects.get_or_create(
                name=alert_data["name"],
                project=project,
                defaults={
                    "timespan_minutes": alert_data["timespan_minutes"],
                    "quantity": alert_data["quantity"],
                    "uptime": alert_data["uptime"],
                },
            )

            for recipient in alert_data["recipients"]:
                AlertRecipient.objects.get_or_create(
                    alert=alert,
                    recipient_type=recipient["type"],
                    url=recipient.get("url", ""),
                )

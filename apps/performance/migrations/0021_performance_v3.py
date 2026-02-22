# Performance Monitoring V3: Span-level tracking
# - Drop TransactionEvent and TransactionGroupAggregate
# - Add new fields to TransactionGroup (organization, first_seen, last_seen, stats)
# - Remove tags, search_vector, SoftDeleteModel from TransactionGroup
# - Create SpanStaging partitioned table (UUID7 + HASH by org)

from datetime import datetime, timedelta, timezone

import apps.performance.models
from django.db import migrations, models

from apps.shared.migration_utils import get_sql_content


def create_initial_partitions(apps, schema_editor):
    """Create initial daily UUID7 partitions with HASH sub-partitioning."""
    from glitchtip.partition_manager import PartitionManager

    manager = PartitionManager(db_connection=schema_editor.connection.alias)

    start_date = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end_date = start_date + timedelta(days=7)

    manager.create_partitions_for_date_range(
        parent_table="performance_spanstaging",
        start_date=start_date,
        end_date=end_date,
        partition_interval="DAY",
        hash_buckets=None,  # Use settings.PARTITION_HASH_BUCKETS
        hash_column="organization_id",
        key_type="uuid7",
    )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("performance", "0020_alter_transactionevent_duration_and_more"),
        ("organizations_ext", "0001_squashed_0008_merge_20250210_1625"),
    ]

    operations = [
        # 1. Drop old partitioned tables
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.DeleteModel(name="TransactionEvent"),
                migrations.DeleteModel(name="TransactionGroupAggregate"),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql="""
                    DROP TABLE IF EXISTS performance_transactionevent CASCADE;
                    DROP TABLE IF EXISTS performance_transactiongroupaggregate CASCADE;
                    """,
                    reverse_sql="",
                ),
            ],
        ),
        # 2. Remove old fields from TransactionGroup
        migrations.RemoveField(
            model_name="transactiongroup",
            name="tags",
        ),
        migrations.RemoveField(
            model_name="transactiongroup",
            name="search_vector",
        ),
        migrations.RemoveField(
            model_name="transactiongroup",
            name="is_deleted",
        ),
        # 3. Add new fields to TransactionGroup
        migrations.AddField(
            model_name="transactiongroup",
            name="organization",
            field=models.ForeignKey(
                on_delete=models.deletion.CASCADE,
                to="organizations_ext.organization",
                null=True,
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="transactiongroup",
            name="first_seen",
            field=models.DateTimeField(null=True),
        ),
        migrations.AddField(
            model_name="transactiongroup",
            name="last_seen",
            field=models.DateTimeField(null=True),
        ),
        migrations.AddField(
            model_name="transactiongroup",
            name="avg_duration",
            field=models.FloatField(default=0, help_text="Average duration in ms"),
        ),
        migrations.AddField(
            model_name="transactiongroup",
            name="p50",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="transactiongroup",
            name="p95",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="transactiongroup",
            name="count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="transactiongroup",
            name="error_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="transactiongroup",
            name="duration_histogram",
            field=models.JSONField(default=dict),
        ),
        # 4. Backfill organization_id and timestamps for existing rows
        migrations.RunSQL(
            sql="""
            UPDATE performance_transactiongroup tg
            SET organization_id = p.organization_id,
                first_seen = COALESCE(tg.first_seen, tg.created),
                last_seen = COALESCE(tg.last_seen, tg.created)
            FROM projects_project p
            WHERE tg.project_id = p.id;
            """,
            reverse_sql="",
        ),
        # 5. Now make organization NOT NULL and timestamps NOT NULL
        migrations.AlterField(
            model_name="transactiongroup",
            name="organization",
            field=models.ForeignKey(
                on_delete=models.deletion.CASCADE,
                to="organizations_ext.organization",
            ),
        ),
        migrations.AlterField(
            model_name="transactiongroup",
            name="first_seen",
            field=models.DateTimeField(),
        ),
        migrations.AlterField(
            model_name="transactiongroup",
            name="last_seen",
            field=models.DateTimeField(),
        ),
        # 6. Remove old managers from TransactionGroup state
        migrations.AlterModelManagers(
            name="transactiongroup",
            managers=[],
        ),
        # 7. Add index on (organization_id, last_seen) for API queries
        migrations.AddIndex(
            model_name="transactiongroup",
            index=models.Index(
                fields=["organization", "last_seen"],
                name="perf_txgroup_org_lastseen",
            ),
        ),
        # 8. Create SpanStaging partitioned table (UUID7 range + HASH org)
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="SpanStaging",
                    fields=[
                        (
                            "id",
                            models.UUIDField(
                                default=apps.performance.models._generate_uuid7,
                                editable=False,
                            ),
                        ),
                        (
                            "pk",
                            models.CompositePrimaryKey(
                                "id",
                                "organization",
                                blank=True,
                                editable=False,
                                primary_key=True,
                                serialize=False,
                            ),
                        ),
                        ("project_id", models.IntegerField()),
                        (
                            "transaction_name",
                            models.CharField(max_length=1024),
                        ),
                        ("span_id", models.CharField(max_length=32)),
                        ("transaction_id", models.CharField(max_length=32)),
                        ("op", models.CharField(max_length=255)),
                        (
                            "description",
                            models.CharField(blank=True, max_length=500),
                        ),
                        (
                            "duration",
                            models.FloatField(
                                help_text="Duration in milliseconds"
                            ),
                        ),
                        (
                            "timestamp",
                            models.DateTimeField(help_text="Span start time"),
                        ),
                        (
                            "organization",
                            models.ForeignKey(
                                on_delete=models.deletion.DO_NOTHING,
                                to="organizations_ext.organization",
                            ),
                        ),
                    ],
                    options={
                        "db_table": "performance_spanstaging",
                        "managed": False,
                    },
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=get_sql_content(__file__, "create_span_staging.sql"),
                    reverse_sql="DROP TABLE IF EXISTS performance_spanstaging CASCADE;",
                ),
            ],
        ),
        # 9. Create initial daily partitions (UUID7 + HASH by org)
        migrations.RunPython(
            code=create_initial_partitions,
            reverse_code=noop,
        ),
    ]

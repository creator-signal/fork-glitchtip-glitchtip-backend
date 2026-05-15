"""Create IssueSearchIndex: hash-partitioned search table for issues.

Decouples full-text search from the Issue table so GIN index maintenance
doesn't impact the hot ingest write path.
"""

import django.contrib.postgres.indexes
import django.contrib.postgres.search
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

from apps.shared.migration_utils import get_sql_content


def create_search_index_hash_partitions(apps, schema_editor):
    """Create hash partitions for IssueSearchIndex based on settings."""
    hash_buckets = settings.PARTITION_HASH_BUCKETS
    with schema_editor.connection.cursor() as cursor:
        for i in range(hash_buckets):
            cursor.execute(
                f"CREATE TABLE IF NOT EXISTS issue_events_issuesearchindex_h{i} "
                f"PARTITION OF issue_events_issuesearchindex "
                f"FOR VALUES WITH (MODULUS {hash_buckets}, REMAINDER {i});"
            )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("issue_events", "0018_issue_assignment"),
        ("organizations_ext", "0011_organization_is_deleted"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="IssueSearchIndex",
                    fields=[
                        (
                            "issue",
                            models.OneToOneField(
                                db_constraint=False,
                                on_delete=django.db.models.deletion.CASCADE,
                                related_name="search_index",
                                to="issue_events.issue",
                            ),
                        ),
                        (
                            "organization",
                            models.ForeignKey(
                                db_constraint=False,
                                on_delete=django.db.models.deletion.DO_NOTHING,
                                to="organizations_ext.organization",
                            ),
                        ),
                        (
                            "pk",
                            models.CompositePrimaryKey(
                                "issue",
                                "organization",
                                primary_key=True,
                                serialize=False,
                            ),
                        ),
                        (
                            "fts_document",
                            django.contrib.postgres.search.SearchVectorField(
                                default="", editable=False
                            ),
                        ),
                    ],
                    options={
                        "indexes": [
                            django.contrib.postgres.indexes.GinIndex(
                                fields=["fts_document"],
                                name="issuesearchindex_fts_gin",
                            ),
                        ],
                    },
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=get_sql_content(__file__, "create_issue_search_index.sql"),
                    reverse_sql="DROP TABLE IF EXISTS issue_events_issuesearchindex CASCADE;",
                ),
            ],
        ),
        migrations.RunPython(
            code=create_search_index_hash_partitions,
            reverse_code=noop,
        ),
    ]

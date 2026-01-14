# Generated manually for Storage Engine V2
# Replaces Uptime tables with V2 schema

from datetime import datetime, timedelta, timezone
from django.db import migrations
from django.db.migrations import RunSQL, SeparateDatabaseAndState
from apps.shared.migration_utils import get_sql_content

def create_initial_partitions(apps, schema_editor):
    """
    Create initial partitions for MonitorCheck.
    Structure: Range (Day) -> Hash (organization_id)
    """
    from glitchtip.partition_manager import PartitionManager

    start_date = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    # Create daily partitions for next 7 days
    end_date = start_date + timedelta(days=7)

    manager = PartitionManager()
    manager.create_partitions_for_date_range(
        parent_table="uptime_monitorcheck",
        start_date=start_date,
        end_date=end_date,
        partition_interval="DAY",
        hash_buckets=None,  # Default from settings (16)
        hash_column="organization_id",
        key_type="uuid7",
    )

def drop_partitions(apps, schema_editor):
    pass

class Migration(migrations.Migration):
    dependencies = [
        ("uptime", "0001_squashed_0010_auto_20240712_1900"),
        ("issue_events", "0007_storage_v2_events"), # For uuid_generate_v7 function
    ]

    operations = [
        SeparateDatabaseAndState(
            state_operations=[], 
            database_operations=[
                RunSQL(
                    sql="DROP TABLE IF EXISTS uptime_monitorcheck CASCADE;",
                    reverse_sql="", 
                ),
                RunSQL(
                    sql=get_sql_content(__file__, "create_uptime_v2.sql"),
                    reverse_sql="DROP TABLE IF EXISTS uptime_monitorcheck CASCADE;",
                ),
                # Ensure default partition attached
                RunSQL(
                    sql="""
                    DROP TABLE IF EXISTS uptime_monitorcheck_default;
                    CREATE TABLE uptime_monitorcheck_default
                    PARTITION OF uptime_monitorcheck DEFAULT;
                    """,
                    reverse_sql="",
                )
            ],
        ),
        migrations.RunPython(
            code=create_initial_partitions,
            reverse_code=drop_partitions,
        ),
    ]

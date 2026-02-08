from django.db import migrations


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("issue_events", "0012_remove_issueevent_received"),
    ]

    operations = [
        migrations.RunSQL(
            sql="DROP INDEX CONCURRENTLY IF EXISTS issue_events_tagvalue_value_998613e9_like;",
            reverse_sql=(
                "CREATE INDEX CONCURRENTLY issue_events_tagvalue_value_998613e9_like "
                "ON issue_events_tagvalue USING btree (value varchar_pattern_ops);"
            ),
        ),
    ]

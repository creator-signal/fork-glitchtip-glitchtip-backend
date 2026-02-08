from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("issue_events", "0012_remove_issueevent_received"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="issueevent",
                    name="data",
                    field=models.JSONField(blank=True, null=True),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql="ALTER TABLE issue_events_issueevent ALTER COLUMN data DROP NOT NULL;",
                    reverse_sql="ALTER TABLE issue_events_issueevent ALTER COLUMN data SET NOT NULL;",
                ),
            ],
        ),
    ]

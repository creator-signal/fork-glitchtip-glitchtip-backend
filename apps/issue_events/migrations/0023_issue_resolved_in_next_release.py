from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("issue_events", "0022_drop_issue_hot_columns"),
    ]

    operations = [
        migrations.AddField(
            model_name="issue",
            name="resolved_in_next_release",
            field=models.BooleanField(default=False),
        ),
    ]

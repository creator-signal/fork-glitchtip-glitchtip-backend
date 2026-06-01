from django.core.validators import MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("uptime", "0016_add_org_date_index_to_hourly_stats"),
    ]

    operations = [
        migrations.AddField(
            model_name="monitor",
            name="consecutive_failures",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="monitor",
            name="consecutive_successes",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="monitor",
            name="failure_threshold",
            field=models.PositiveSmallIntegerField(
                default=1, validators=[MinValueValidator(1)]
            ),
        ),
        migrations.AddField(
            model_name="monitor",
            name="recovery_threshold",
            field=models.PositiveSmallIntegerField(
                default=1, validators=[MinValueValidator(1)]
            ),
        ),
    ]

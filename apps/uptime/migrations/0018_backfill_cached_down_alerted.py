from django.db import migrations


class Migration(migrations.Migration):
    """Backfill cached_down_alerted for monitors already down at upgrade time:
    the old code already sent their down alert, so mark them alerted to ensure
    the recovery ('is back up') email fires when they come back up."""

    dependencies = [
        ("uptime", "0017_monitor_cached_down_alerted"),
    ]

    operations = [
        migrations.RunSQL(
            sql="UPDATE uptime_monitor SET cached_down_alerted = TRUE WHERE cached_is_up = FALSE;",
            reverse_sql="UPDATE uptime_monitor SET cached_down_alerted = FALSE;",
        ),
    ]

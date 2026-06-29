from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("alerts", "0015_add_feishu_recipient_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="projectalert",
            name="environment",
            field=models.CharField(
                blank=True,
                help_text="Only alert on events from this environment. Blank matches all environments.",
                max_length=255,
            ),
        ),
    ]

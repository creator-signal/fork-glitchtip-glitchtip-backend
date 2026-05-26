from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="IssuedLicense",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "stripe_subscription_id",
                    models.CharField(max_length=255, unique=True),
                ),
                (
                    "stripe_customer_id",
                    models.CharField(db_index=True, max_length=255),
                ),
                ("email", models.EmailField(max_length=254)),
                ("plan", models.CharField(max_length=255)),
                ("status", models.CharField(max_length=32)),
                ("current_period_end", models.DateTimeField()),
                ("last_issued_at", models.DateTimeField(auto_now=True)),
                (
                    "last_stripe_event_id",
                    models.CharField(blank=True, default="", max_length=255),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
    ]

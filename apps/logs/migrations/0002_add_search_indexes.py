# Generated manually for Logs feature
# Adds indexes for text search and service filtering

from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("logs", "0001_initial"),
    ]

    operations = [
        # Ensure pg_trgm extension is available for GIN trigram indexes
        TrigramExtension(),
        # Add service filtering index
        migrations.RunSQL(
            sql="""
            CREATE INDEX IF NOT EXISTS logevent_service_idx
                ON logs_logevent (organization_id, service, id DESC);
            """,
            reverse_sql="DROP INDEX IF EXISTS logevent_service_idx;",
        ),
        # Add GIN trigram index for ILIKE text search on body
        migrations.RunSQL(
            sql="""
            CREATE INDEX IF NOT EXISTS logevent_body_trgm_idx
                ON logs_logevent USING GIN (body gin_trgm_ops);
            """,
            reverse_sql="DROP INDEX IF EXISTS logevent_body_trgm_idx;",
        ),
    ]

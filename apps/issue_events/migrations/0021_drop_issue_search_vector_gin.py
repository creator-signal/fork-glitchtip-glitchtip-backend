"""Drop the Issue.search_vector GIN index concurrently.

Full-text search now reads IssueSearchIndex exclusively, so this index is
dead weight (and the GIN maintenance on it was the whole reason for the
decoupling). Dropped first, before the column (0022), so the column drop is
pure metadata and doesn't have to cascade-drop a large GIN under an
ACCESS EXCLUSIVE lock.

CONCURRENTLY takes only SHARE UPDATE EXCLUSIVE, so it never blocks ingest
reads/writes -- but it cannot run inside a transaction, hence ``atomic = False``.
"""

from django.contrib.postgres.operations import RemoveIndexConcurrently
from django.db import migrations


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("issue_events", "0020_backfill_recent_issue_search_index"),
    ]

    operations = [
        RemoveIndexConcurrently(
            model_name="issue",
            name="issue_event_search__346c17_gin",
        ),
    ]

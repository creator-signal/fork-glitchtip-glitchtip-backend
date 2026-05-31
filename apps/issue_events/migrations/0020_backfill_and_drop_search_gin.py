"""Backfill recent issues into IssueSearchIndex, then drop the old GIN.

Two non-transactional steps share this ``atomic = False`` migration. Both
require autocommit (the backfill commits per batch; ``DROP INDEX
CONCURRENTLY`` cannot run in a transaction) and both are independently
idempotent, so a deploy killed mid-migration can simply re-run it.

Step 1 -- Best-effort backfill. Only issues whose ``last_seen`` is within
``GLITCHTIP_EVENT_HOT_DAYS`` are copied. Older issues are skipped -- with
Issue.search_vector dropped (0021) and no OR-fallback, a skipped issue is
not full-text searchable until it next receives an event, at which point
ingest inserts its index row. Accepted tradeoff: few users search, and
active issues repopulate their index almost immediately.

- keyset-paginated batches over the last_seen index never hold a long
  transaction or a long single statement, and the work scales with the
  number of *hot* issues, not total table size, so it can't blow past
  statement_timeout or block the hot Issue write path for more than one
  small batch at a time.
- ``ON CONFLICT DO NOTHING``: idempotent and safe to run alongside live
  ingest (which writes the same rows); committed batches survive a killed
  deploy and a re-run resumes.
- Copies the existing ``search_vector`` tsvector verbatim (no
  ``to_tsvector`` recomputation), so it is cheap and preserves the full
  accumulated lexeme history of each issue.

Step 2 -- Drop the now-redundant Issue.search_vector GIN. Full-text search
now reads IssueSearchIndex exclusively, so this index is dead weight (and
its maintenance was the whole reason for the decoupling). Dropped here,
before the column (0021), so the column drop is pure metadata and doesn't
have to cascade-drop a large GIN under ACCESS EXCLUSIVE. ``CONCURRENTLY``
takes only SHARE UPDATE EXCLUSIVE, so it never blocks ingest -- and emits
``DROP INDEX CONCURRENTLY IF EXISTS``, so the re-run after a killed deploy
is a no-op rather than an error.
"""

from django.conf import settings
from django.contrib.postgres.operations import RemoveIndexConcurrently
from django.db import connection, migrations

# Rows copied per batch. Each batch is a bounded index-range scan over
# last_seen (keyset paginated), so a batch never holds a long transaction
# or a long single statement regardless of table size.
_BATCH = 20000


def backfill_recent(apps, schema_editor):
    hot_days = settings.GLITCHTIP_EVENT_HOT_DAYS
    with connection.cursor() as cursor:
        # Pin the cutoff once so keyset pagination is stable as now() advances.
        cursor.execute("SELECT now() - (%s || ' days')::interval", [hot_days])
        (cutoff,) = cursor.fetchone()

        # Keyset-paginate over the last_seen index (ties broken by id) so we
        # touch only recently-active issues. An id-window scan would instead
        # walk the whole id range -- at scale that means scanning cold rows
        # to find the hot subset, which can blow the deploy/migration budget.
        after_last_seen = None
        after_id = None
        while True:
            params = [cutoff]
            keyset = ""
            if after_last_seen is not None:
                keyset = "AND (i.last_seen, i.id) > (%s, %s) "
                params += [after_last_seen, after_id]
            params.append(_BATCH)
            cursor.execute(
                "WITH batch AS ("
                "  SELECT i.id, p.organization_id AS org_id,"
                "         i.search_vector, i.last_seen"
                "  FROM issue_events_issue i"
                "  JOIN projects_project p ON p.id = i.project_id"
                "  WHERE i.last_seen >= %s AND i.is_deleted = false"
                "    AND i.search_vector <> ''::tsvector "
                + keyset
                + "  ORDER BY i.last_seen, i.id LIMIT %s"
                "), ins AS ("
                "  INSERT INTO issue_events_issuesearchindex"
                "    (issue_id, organization_id, fts_document)"
                "  SELECT id, org_id, search_vector FROM batch"
                "  ON CONFLICT (issue_id, organization_id) DO NOTHING"
                ") "
                "SELECT last_seen, id FROM batch ORDER BY last_seen DESC, id DESC "
                "LIMIT 1",
                params,
            )
            row = cursor.fetchone()
            if row is None:  # no more qualifying rows
                break
            after_last_seen, after_id = row

        # Freshly bulk-loaded partitions have no autoanalyze history; give
        # the planner accurate stats before the search path starts using
        # the new GIN index in earnest.
        cursor.execute("ANALYZE issue_events_issuesearchindex")


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("issue_events", "0019_issuesearchindex"),
    ]

    operations = [
        migrations.RunPython(code=backfill_recent, reverse_code=noop),
        RemoveIndexConcurrently(
            model_name="issue",
            name="issue_event_search__346c17_gin",
        ),
    ]

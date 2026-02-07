import logging
from datetime import timedelta

from django.conf import settings
from django.utils.timezone import now

from .models import Issue

logger = logging.getLogger(__name__)


def cleanup_old_issues():
    """
    Delete Issues whose partitioned data has been dropped and have no
    remaining related objects.

    maintain_partitions drops old partitions of IssueEvent (daily),
    IssueAggregate (weekly), and IssueTag (weekly) before this runs.
    Because those tables use different partition intervals, an issue's
    events can be dropped before its aggregates/tags.

    Uses _raw_delete() instead of .delete() to bypass Django's collector,
    which would run unindexed queries against every sub-partition.
    If a new FK referencing Issue is added, the DB will raise
    IntegrityError here — add the corresponding filter to the queryset below.
    """
    days = settings.GLITCHTIP_MAX_EVENT_LIFE_DAYS

    queryset = Issue.objects.filter(
        issueevent=None,
        issueaggregate=None,
        issuetag=None,
        hashes=None,
        comments=None,
        userreport=None,
        last_seen__lt=now() - timedelta(days=days),
    ).order_by("id")

    total_deleted = 0
    while True:
        try:
            empty_issue_delimiter = queryset.values_list("id", flat=True)[
                1000:1001
            ].get()
            count = queryset.filter(id__lte=empty_issue_delimiter)._raw_delete(
                queryset.db
            )
            total_deleted += count
        except Issue.DoesNotExist:
            break

    total_deleted += queryset._raw_delete(queryset.db)
    if total_deleted:
        logger.info("Deleted %d empty issues", total_deleted)

import logging
import re
from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.db import connection

from glitchtip.partition_manager import PartitionManager

logger = logging.getLogger(__name__)

DATE_SUFFIX_PATTERN = re.compile(r".*_(\d{8})$")


def downsample_events():
    """
    Downsample old events by nullifying the `data` JSON column.

    Preserves:
    - The newest event per issue (representative)
    - A configurable fraction of remaining events per project (downsample_rate)
    - All events for projects with downsample_rate=0.0

    Skips partitions that have already been downsampled (marked via table comment).
    Disabled when GLITCHTIP_EVENT_DOWNSAMPLE_DAYS is 0.
    """
    downsample_days = settings.GLITCHTIP_EVENT_DOWNSAMPLE_DAYS
    if downsample_days <= 0:
        logger.debug("Event downsampling disabled (GLITCHTIP_EVENT_DOWNSAMPLE_DAYS=0)")
        return

    manager = PartitionManager()
    threshold_date = datetime.now(timezone.utc).date() - timedelta(days=downsample_days)

    parent_table = "issue_events_issueevent"
    date_partitions = manager.list_partitions(parent_table)

    downsampled_count = 0

    for partition in date_partitions:
        name = partition["partition_name"]
        match = DATE_SUFFIX_PATTERN.match(name)
        if not match:
            continue

        try:
            partition_date = datetime.strptime(match.group(1), "%Y%m%d").date()
        except ValueError:
            continue

        if partition_date >= threshold_date:
            continue

        comment = manager.get_partition_comment(name)
        if comment == "downsampled":
            continue

        # Get hash children of this date partition
        hash_partitions = manager.list_partitions(name)
        for leaf in hash_partitions:
            leaf_name = leaf["partition_name"]
            _downsample_leaf_partition(leaf_name)

        manager.set_partition_comment(name, "downsampled")
        downsampled_count += 1
        logger.info("Downsampled partition %s", name)

    if downsampled_count:
        logger.info("Downsampled %d partition(s)", downsampled_count)
    else:
        logger.debug("No partitions needed downsampling")


def _downsample_leaf_partition(leaf_name: str):
    """
    Nullify the `data` column on non-representative events in a leaf partition.

    For each issue in the partition, the newest event (highest UUIDv7 id) is
    kept as the representative. Of the remaining events, a fraction equal to
    the project's `downsample_rate` retains its data; the rest are nullified.
    """
    sql = f"""
    WITH representatives AS (
        SELECT DISTINCT ON (issue_id) id
        FROM {leaf_name}
        WHERE data IS NOT NULL
        ORDER BY issue_id, id DESC
    )
    UPDATE {leaf_name} AS ev
    SET data = NULL
    FROM issue_events_issue AS i
    JOIN projects_project AS p ON i.project_id = p.id
    WHERE ev.issue_id = i.id
      AND p.downsample_rate > 0.0
      AND ev.data IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM representatives r WHERE r.id = ev.id)
      AND random() >= p.downsample_rate;
    """
    with connection.cursor() as cursor:
        cursor.execute(sql)
        if cursor.rowcount:
            logger.debug(
                "Nullified data on %d event(s) in %s", cursor.rowcount, leaf_name
            )

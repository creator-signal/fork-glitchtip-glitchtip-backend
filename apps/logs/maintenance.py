"""
Log maintenance tasks for archiving and cleanup.

Called from glitchtip.tasks.perform_maintenance nightly.
"""

import logging

from django.conf import settings

from .cold_storage import (
    ColdStorageConfig,
    archive_and_swap_partition,
    delete_cold_partition,
    get_partitions_older_than,
    is_pg_duckdb_available,
)

logger = logging.getLogger(__name__)


def cleanup_old_logs():
    """
    Archive old log partitions to cold storage and delete expired cold data.

    Settings:
        GLITCHTIP_LOGS_HOT_DAYS: Days to keep in hot storage (default 7)
        GLITCHTIP_LOGS_COLD_DAYS: Days to keep in cold storage (default 90)
    """
    if not getattr(settings, "GLITCHTIP_ENABLE_LOGS", False):
        return

    hot_days = getattr(settings, "GLITCHTIP_LOGS_HOT_DAYS", 7)
    cold_days = getattr(settings, "GLITCHTIP_LOGS_COLD_DAYS", 90)

    # Archive hot -> cold (requires pg_duckdb)
    if is_pg_duckdb_available():
        archive_old_partitions(hot_days)
        # Delete expired cold storage
        delete_expired_cold_storage(cold_days)
    else:
        # No cold storage available - just delete old partitions
        delete_old_hot_partitions(hot_days)


def archive_old_partitions(days: int):
    """Archive partitions older than `days` to S3 cold storage."""
    partitions = get_partitions_older_than("logs_logevent", days)

    if not partitions:
        logger.debug(f"No log partitions older than {days} days to archive")
        return

    logger.info(f"Archiving {len(partitions)} log partitions to cold storage")

    config = ColdStorageConfig.from_settings()
    archived = 0
    failed = 0

    for name, date in partitions:
        try:
            if archive_and_swap_partition(name, config=config):
                archived += 1
                logger.info(f"Archived log partition {name}")
            else:
                failed += 1
                logger.warning(f"Failed to archive log partition {name}")
        except Exception as e:
            failed += 1
            logger.error(f"Error archiving log partition {name}: {e}")

    logger.info(f"Log archival complete: {archived} archived, {failed} failed")


def delete_expired_cold_storage(days: int):
    """Delete cold storage files older than `days`."""
    # Get cold partitions (archive views) older than retention
    partitions = get_partitions_older_than(
        "logs_logevent", days, partition_suffix="_archive"
    )

    if not partitions:
        logger.debug(f"No cold log partitions older than {days} days to delete")
        return

    logger.info(f"Deleting {len(partitions)} expired cold log partitions")

    config = ColdStorageConfig.from_settings()
    deleted = 0

    for name, date in partitions:
        try:
            delete_cold_partition(name, config=config)
            deleted += 1
            logger.info(f"Deleted cold log partition {name}")
        except Exception as e:
            logger.error(f"Error deleting cold log partition {name}: {e}")

    logger.info(f"Cold storage cleanup complete: {deleted} deleted")


def delete_old_hot_partitions(days: int):
    """Delete hot partitions older than `days` when cold storage unavailable."""
    from django.db import connection

    partitions = get_partitions_older_than("logs_logevent", days)

    if not partitions:
        return

    logger.info(
        f"Deleting {len(partitions)} old log partitions (cold storage unavailable)"
    )

    for name, date in partitions:
        try:
            with connection.cursor() as cursor:
                # Drop the partition table
                cursor.execute(f"DROP TABLE IF EXISTS {name} CASCADE;")
            logger.info(f"Deleted log partition {name}")
        except Exception as e:
            logger.error(f"Error deleting log partition {name}: {e}")

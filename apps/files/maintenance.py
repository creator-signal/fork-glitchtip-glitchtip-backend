import logging
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db.models import Exists, OuterRef
from django.utils.timezone import now

from .models import File, FileBlob

logger = logging.getLogger(__name__)


MAX_DELETIONS_PER_RUN = 10_000


async def _delete_file_blobs(queryset, label):
    total_deleted = 0
    while total_deleted < MAX_DELETIONS_PER_RUN:
        file_blobs = [fb async for fb in queryset.only("id", "blob")[:1000].aiterator()]
        if not file_blobs:
            break
        ids = []
        for file_blob in file_blobs:
            ids.append(file_blob.id)
            try:
                await sync_to_async(file_blob.blob.delete)()
            except Exception:
                logger.warning("Failed to delete storage for FileBlob %d", file_blob.id)
        count, _ = (
            await FileBlob.objects.using(settings.MAINTENANCE_DATABASE_ALIAS)
            .filter(id__in=ids)
            .adelete()
        )
        total_deleted += count
    if total_deleted:
        logger.info("Deleted %d %s file blobs", total_deleted, label)


async def cleanup_old_files():
    """
    Delete stale and orphaned FileBlobs and their storage files.

    Two passes:
    1. Retention: delete FileBlobs created before the retention cutoff that
       have no recent File referencing them.
    2. Orphans: delete FileBlobs with zero File references that are older
       than 24 hours (grace period to protect in-flight uploads). Orphans
       arise from multi-chunk assembly, source-map re-uploads, and
       abandoned uploads.
    """
    db_alias = settings.MAINTENANCE_DATABASE_ALIAS

    days_ago = now() - timedelta(days=settings.GLITCHTIP_FILE_RETENTION_DAYS)
    retention_qs = (
        FileBlob.objects.using(db_alias)
        .filter(created__lt=days_ago)
        .exclude(
            Exists(File.objects.filter(blob_id=OuterRef("id"), created__gte=days_ago))
        )
    )
    await _delete_file_blobs(retention_qs, "old")

    cutoff = now() - timedelta(hours=24)
    orphan_qs = (
        FileBlob.objects.using(db_alias)
        .filter(created__lt=cutoff)
        .exclude(Exists(File.objects.filter(blob_id=OuterRef("id"))))
    )
    await _delete_file_blobs(orphan_qs, "orphaned")

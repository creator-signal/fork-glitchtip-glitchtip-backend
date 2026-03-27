import logging

from asgiref.sync import sync_to_async
from django_vtasks import run_in_process, task

logger = logging.getLogger(__name__)


@task
async def promote_spans():
    from apps.performance.promotion import (
        delete_promoted_rows,
        fetch_promotable_spans,
        write_org_parquet_chunks,
    )

    org_batches, truncated = await sync_to_async(fetch_promotable_spans)()
    if not org_batches:
        return

    total_promoted = 0
    for org_id, date_groups, storage_config, column_types in org_batches:
        # Parquet writes run in a child process — all arro3/Arrow memory
        # is allocated there and fully reclaimed by the OS when it exits.
        try:
            written_chunks = await run_in_process(
                write_org_parquet_chunks,
                storage_config,
                org_id,
                date_groups,
                column_types,
            )
        except Exception:
            logger.error(
                "Child process failed writing chunks for org %d",
                org_id,
                exc_info=True,
            )
            continue

        # DB deletes stay in the parent process (needs DB connection).
        promoted = await sync_to_async(delete_promoted_rows)(org_id, written_chunks)
        total_promoted += promoted

    if total_promoted:
        logger.info("Promoted %d span rows to cold storage", total_promoted)

    if truncated and total_promoted > 0:
        logger.info("Promotion batch full (%d rows), re-enqueueing", total_promoted)
        await promote_spans.aenqueue()


@task
async def compact_span_chunks():
    from apps.performance.promotion import (
        collect_compactable_chunks,
        compact_chunks_in_child,
        finalize_compaction,
    )

    compaction_jobs = await sync_to_async(collect_compactable_chunks)()
    if not compaction_jobs:
        return

    compacted = 0
    for job in compaction_jobs:
        try:
            await run_in_process(compact_chunks_in_child, job)
        except Exception:
            logger.error(
                "Failed to compact chunks in %s", job["date_path"], exc_info=True
            )
            continue

        await sync_to_async(finalize_compaction)(job)
        compacted += len(job["chunks"])

    if compacted:
        logger.info("Compacted %d span chunk files", compacted)

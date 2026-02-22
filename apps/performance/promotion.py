"""
Span promotion and compaction for Performance Monitoring V2.

Promotes span_staging rows to per-org Parquet files, then compacts
chunk files into daily files for efficient analytical queries.
"""

import logging
import os
import time

from django.utils import timezone

from glitchtip.cold_storage import (
    COLD_STORAGE_PREFIX,
    get_cold_storage_backend,
    get_duckdb_connection,
    get_duckdb_parquet_path,
    is_duckdb_available,
)

logger = logging.getLogger(__name__)

TABLE_NAME = "performance_spans"

# Minimum rows per org+date group before promoting (skip tiny batches)
MIN_BATCH_SIZE = 100


def promote_spans() -> int:
    """
    Promote span_staging rows to per-org Parquet files.

    1. Query rows where created < NOW() - 5 minutes (lag prevents racing with inserts)
    2. Group by (organization_id, date(timestamp))
    3. Write chunk Parquet files per org+date
    4. DELETE consumed rows

    Returns number of rows promoted.
    """
    if not is_duckdb_available():
        logger.debug("DuckDB not available, skipping span promotion")
        return 0

    storage = get_cold_storage_backend()
    if not storage:
        logger.debug("No storage backend, skipping span promotion")
        return 0

    from apps.performance.models import SpanStaging

    cutoff = timezone.now() - timezone.timedelta(minutes=5)

    rows = list(
        SpanStaging.objects.filter(created__lt=cutoff)
        .order_by("organization_id", "created")
        .values_list(
            "id",
            "organization_id",
            "project_id",
            "transaction_name",
            "span_id",
            "transaction_id",
            "op",
            "description",
            "duration",
            "timestamp",
        )[:50000]
    )

    if not rows:
        return 0

    # Group rows by (org_id, date)
    groups: dict[tuple[int, str], list[tuple]] = {}
    for row in rows:
        org_id = row[1]
        ts = row[9]  # timestamp
        date_str = ts.strftime("%Y%m%d") if ts else "unknown"
        key = (org_id, date_str)
        groups.setdefault(key, []).append(row)

    promoted_ids: list[int] = []

    for (org_id, date_str), group_rows in groups.items():
        if len(group_rows) < MIN_BATCH_SIZE:
            continue

        try:
            _write_chunk_parquet(storage, org_id, date_str, group_rows)
            promoted_ids.extend(r[0] for r in group_rows)
        except Exception:
            logger.error(
                "Failed to write parquet chunk for org %d date %s",
                org_id,
                date_str,
                exc_info=True,
            )

    # Delete promoted rows
    if promoted_ids:
        SpanStaging.objects.filter(id__in=promoted_ids, created__lt=cutoff).delete()

    logger.info("Promoted %d span rows to cold storage", len(promoted_ids))
    return len(promoted_ids)


def _write_chunk_parquet(storage, org_id: int, date_str: str, rows: list[tuple]) -> str:
    """Write a chunk Parquet file for a single org+date group."""
    chunk_ts = int(time.time())
    org_dir = f"{COLD_STORAGE_PREFIX}/{TABLE_NAME}/org_{org_id}/{date_str}"
    relative_path = f"{org_dir}/chunk_{chunk_ts}.parquet"
    parquet_path = get_duckdb_parquet_path(storage, relative_path)

    # Ensure directory exists for filesystem storage
    parquet_dir = os.path.dirname(parquet_path)
    if not parquet_path.startswith("s3://"):
        os.makedirs(parquet_dir, exist_ok=True)

    duck_conn = get_duckdb_connection(storage)
    try:
        # Create a table from the data
        duck_conn.execute("""
            CREATE TEMPORARY TABLE staging (
                id BIGINT,
                organization_id INTEGER,
                project_id INTEGER,
                transaction_name VARCHAR,
                span_id VARCHAR,
                transaction_id VARCHAR,
                op VARCHAR,
                description VARCHAR,
                duration DOUBLE,
                timestamp TIMESTAMP WITH TIME ZONE
            )
        """)

        # Insert rows (strip the id column from output)
        duck_conn.executemany(
            "INSERT INTO staging VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

        # Write to parquet (exclude the staging id)
        duck_conn.execute(f"""
            COPY (
                SELECT organization_id, project_id, transaction_name,
                       span_id, transaction_id, op, description,
                       duration, timestamp
                FROM staging
                ORDER BY timestamp
            ) TO '{parquet_path}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
    finally:
        duck_conn.close()

    return relative_path


def compact_span_chunks() -> int:
    """
    Compact chunk Parquet files into single daily files per org.

    For each org directory, merges chunk files for completed days
    (before today) into a single sorted Parquet file.

    Returns number of files compacted.
    """
    if not is_duckdb_available():
        return 0

    storage = get_cold_storage_backend()
    if not storage:
        return 0

    spans_prefix = f"{COLD_STORAGE_PREFIX}/{TABLE_NAME}"
    today_str = timezone.now().strftime("%Y%m%d")
    compacted = 0

    try:
        org_dirs, _ = storage.listdir(spans_prefix)
    except (NotImplementedError, OSError):
        return 0

    for org_dir in org_dirs:
        if not org_dir.startswith("org_"):
            continue

        org_path = f"{spans_prefix}/{org_dir}"
        try:
            date_dirs, flat_files = storage.listdir(org_path)
        except (NotImplementedError, OSError):
            continue

        # Process date subdirectories with chunk files
        for date_dir in date_dirs:
            if date_dir == today_str:
                continue  # Don't compact today's chunks

            date_path = f"{org_path}/{date_dir}"
            try:
                _, chunk_files = storage.listdir(date_path)
            except (NotImplementedError, OSError):
                continue

            chunks = [f for f in chunk_files if f.endswith(".parquet")]
            if len(chunks) <= 1:
                continue

            try:
                _compact_date_chunks(storage, org_path, date_dir, date_path, chunks)
                compacted += len(chunks)
            except Exception:
                logger.error("Failed to compact chunks in %s", date_path, exc_info=True)

    if compacted:
        logger.info("Compacted %d span chunk files", compacted)

    return compacted


def _compact_date_chunks(
    storage, org_path: str, date_dir: str, date_path: str, chunks: list[str]
):
    """Compact multiple chunk files into a single daily Parquet file."""
    # Build list of chunk paths for DuckDB
    chunk_paths = [
        get_duckdb_parquet_path(storage, f"{date_path}/{chunk}") for chunk in chunks
    ]

    # Output path: org_{id}/{date_str}.parquet (flat file)
    output_relative = f"{org_path}/{date_dir}.parquet"
    output_path = get_duckdb_parquet_path(storage, output_relative)

    duck_conn = get_duckdb_connection(storage)
    try:
        paths_list = ", ".join(f"'{p}'" for p in chunk_paths)
        duck_conn.execute(f"""
            COPY (
                SELECT * FROM read_parquet([{paths_list}])
                ORDER BY timestamp
            ) TO '{output_path}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
    finally:
        duck_conn.close()

    # Delete chunk files and empty directory
    for chunk in chunks:
        try:
            storage.delete(f"{date_path}/{chunk}")
        except Exception:
            logger.warning("Failed to delete chunk %s/%s", date_path, chunk)

    # Try to remove the empty date directory (filesystem only)
    if not output_path.startswith("s3://"):
        try:
            os.rmdir(storage.path(date_path))
        except OSError:
            pass

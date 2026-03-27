"""
Span promotion and compaction for Performance Monitoring V2.

Promotes span_staging rows to per-org Parquet files, then compacts
chunk files into daily files for efficient analytical queries.

Memory isolation: Parquet/DuckDB work runs in child processes via
run_in_process() so that arro3/Arrow/DuckDB memory is fully reclaimed
by the OS when the child exits — no heap fragmentation in the worker.
"""

import io
import logging
import os
import tempfile
import time
from datetime import timedelta

from django.conf import settings
from django.db import connection
from django.utils import timezone

from glitchtip.cold_storage import (
    COLD_STORAGE_PREFIX,
    _is_s3_storage,
    _parquet_encoding_opts,
    duckdb_quote_path,
    get_cold_storage_backend,
    get_duckdb_parquet_path,
    is_duckdb_available,
)
from glitchtip.partition_manager import UUID7Helper

from .cold_storage import SPAN_PARQUET_COLUMN_TYPES

logger = logging.getLogger(__name__)

TABLE_NAME = "performance_spans"

# Process up to this many rows per organization per invocation.
BATCH_LIMIT_PER_ORG = 100_000


# ---------------------------------------------------------------------------
# Storage config helpers (serialize storage object for child processes)
# ---------------------------------------------------------------------------


def _get_storage_config(storage) -> dict:
    """Extract picklable storage config from a django-storages backend."""
    if _is_s3_storage(storage):
        return {
            "type": "s3",
            "bucket_name": storage.bucket_name,
            "access_key": getattr(storage, "access_key", None),
            "secret_key": getattr(storage, "secret_key", None),
            "endpoint_url": getattr(storage, "endpoint_url", None),
        }
    return {
        "type": "filesystem",
        "location": storage.location,
    }


# ---------------------------------------------------------------------------
# Phase 1: Fetch (runs in parent process — needs Django ORM)
# ---------------------------------------------------------------------------


def fetch_promotable_spans() -> tuple[list[tuple], bool]:
    """Query all promotable span rows, grouped by org and date.

    Returns:
        (org_batches, truncated) where org_batches is a list of
        (org_id, date_groups, storage_config, column_types) tuples.
        All values are picklable for dispatch to a child process.
        truncated is True if any org hit BATCH_LIMIT_PER_ORG.
    """
    if not is_duckdb_available():
        return [], False

    storage = get_cold_storage_backend()
    if not storage:
        return [], False

    from apps.performance.models import SpanStaging

    cutoff = timezone.now() - timedelta(minutes=5)
    cutoff_uuid = UUID7Helper.from_datetime(cutoff)

    org_ids = list(
        SpanStaging.objects.filter(id__lt=cutoff_uuid)
        .values_list("organization_id", flat=True)
        .distinct()
    )
    if not org_ids:
        return [], False

    storage_config = _get_storage_config(storage)
    column_types = dict(SPAN_PARQUET_COLUMN_TYPES)
    truncated = False
    org_batches = []

    for org_id in org_ids:
        rows = list(
            SpanStaging.objects.filter(
                id__lt=cutoff_uuid,
                organization_id=org_id,
            ).values_list(
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
            )[:BATCH_LIMIT_PER_ORG]
        )

        if not rows:
            continue

        if len(rows) >= BATCH_LIMIT_PER_ORG:
            truncated = True

        # Group rows by date. Convert datetimes to float timestamps
        # so the tuples are fully picklable (datetime is picklable,
        # but explicit about it for clarity).
        date_groups: dict[str, list[tuple]] = {}
        for row in rows:
            ts = row[9]  # timestamp
            date_str = ts.strftime("%Y%m%d") if ts else "unknown"
            date_groups.setdefault(date_str, []).append(row)

        org_batches.append((org_id, date_groups, storage_config, column_types))

    return org_batches, truncated


# ---------------------------------------------------------------------------
# Phase 2: Write (runs in child process — no Django, no DB)
# ---------------------------------------------------------------------------


def write_org_parquet_chunks(
    storage_config: dict,
    org_id: int,
    date_groups: dict[str, list[tuple]],
    column_types: dict[str, str],
) -> list[tuple[str, str, list]]:
    """Write Parquet chunks for one org. Runs in a child process.

    This function must not import Django or use DB connections.
    All heavy arro3/Arrow memory is allocated here and fully reclaimed
    by the OS when this child process exits.

    Returns list of (date_str, chunk_path, row_ids) for successful writes.
    """
    results = []
    for date_str, group_rows in date_groups.items():
        try:
            chunk_path = _write_chunk_parquet_isolated(
                storage_config, org_id, date_str, group_rows, column_types
            )
            row_ids = [r[0] for r in group_rows]
            results.append((date_str, chunk_path, row_ids))
        except Exception:
            # Log in child — the parent will also see the exception if
            # the entire function fails, but partial failures are handled here.
            import logging as _logging

            _logging.getLogger(__name__).error(
                "Failed to write parquet chunk for org %d date %s",
                org_id,
                date_str,
                exc_info=True,
            )
    return results


def _write_chunk_parquet_isolated(
    storage_config: dict,
    org_id: int,
    date_str: str,
    rows: list[tuple],
    column_types: dict[str, str],
) -> str:
    """Write a single chunk Parquet file via arro3. Runs in child process."""
    import arro3.core as ac
    import arro3.io as aio

    chunk_ts = f"{time.time_ns()}_{os.getpid()}"
    org_dir = f"{COLD_STORAGE_PREFIX}/{TABLE_NAME}/org_{org_id}/{date_str}"
    relative_path = f"{org_dir}/chunk_{chunk_ts}.parquet"

    batch = ac.RecordBatch.from_arrays(
        [
            ac.Array([r[1] for r in rows], type=ac.DataType.int32()),
            ac.Array([r[2] for r in rows], type=ac.DataType.int32()),
            ac.Array([r[3] for r in rows], type=ac.DataType.utf8()),
            ac.Array([r[4] for r in rows], type=ac.DataType.utf8()),
            ac.Array([r[5] for r in rows], type=ac.DataType.utf8()),
            ac.Array([r[6] for r in rows], type=ac.DataType.utf8()),
            ac.Array([r[7] for r in rows], type=ac.DataType.utf8()),
            ac.Array([r[8] for r in rows], type=ac.DataType.float64()),
            ac.Array(
                [int(r[9].timestamp() * 1_000_000) if r[9] else 0 for r in rows],
                type=ac.DataType.int64(),
            ).cast(ac.DataType.timestamp("us")),
        ],
        names=list(column_types.keys()),
    )

    encoding_opts = _parquet_encoding_opts(column_types)
    write_kwargs = {
        "compression": "zstd(3)",
        "max_row_group_size": min(len(rows), 100_000),
        **encoding_opts,
    }

    if storage_config["type"] == "s3":
        buf = io.BytesIO()
        aio.write_parquet(batch, buf, **write_kwargs)
        buf.seek(0)
        import boto3

        s3_kwargs = {}
        if storage_config.get("endpoint_url"):
            s3_kwargs["endpoint_url"] = storage_config["endpoint_url"]
        s3 = boto3.client(
            "s3",
            aws_access_key_id=storage_config.get("access_key"),
            aws_secret_access_key=storage_config.get("secret_key"),
            **s3_kwargs,
        )
        s3.put_object(
            Bucket=storage_config["bucket_name"],
            Key=relative_path,
            Body=buf.getvalue(),
        )
    else:
        parquet_path = os.path.join(storage_config["location"], relative_path)
        os.makedirs(os.path.dirname(parquet_path), exist_ok=True)
        aio.write_parquet(batch, parquet_path, **write_kwargs)

    return relative_path


# ---------------------------------------------------------------------------
# Phase 3: Delete (runs in parent process — needs DB connection)
# ---------------------------------------------------------------------------


def delete_promoted_rows(
    org_id: int,
    written_chunks: list[tuple[str, str, list]],
) -> int:
    """Delete promoted rows from span_staging. Runs in parent process."""
    storage = get_cold_storage_backend()
    promoted = 0

    for date_str, chunk_path, row_ids in written_chunks:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM performance_spanstaging
                    WHERE id = ANY(%s)
                      AND organization_id = %s
                    """,
                    [row_ids, org_id],
                )
        except Exception:
            logger.error(
                "Failed to delete promoted rows for org %d date %s, "
                "removing chunk to prevent duplicates",
                org_id,
                date_str,
                exc_info=True,
            )
            if storage:
                try:
                    storage.delete(chunk_path)
                except Exception:
                    logger.error(
                        "Failed to remove chunk %s — duplicates may "
                        "exist on next promotion run",
                        chunk_path,
                    )
            continue
        promoted += len(row_ids)

    return promoted


# ---------------------------------------------------------------------------
# Compaction: collect (parent) → compact (child) → finalize (parent)
# ---------------------------------------------------------------------------


def collect_compactable_chunks() -> list[dict]:
    """Find chunk directories that need compaction. Runs in parent process.

    Returns a list of job dicts with all info needed by the child process
    (file paths, DuckDB config) — no Django objects.
    """
    if not is_duckdb_available():
        return []

    storage = get_cold_storage_backend()
    if not storage:
        return []

    spans_prefix = f"{COLD_STORAGE_PREFIX}/{TABLE_NAME}"
    now = timezone.now()
    skip_dates = {
        now.strftime("%Y%m%d"),
        (now - timedelta(days=1)).strftime("%Y%m%d"),
    }

    try:
        org_dirs, _ = storage.listdir(spans_prefix)
    except (NotImplementedError, OSError):
        return []

    # Extract DuckDB config once for all jobs
    s3_config = None
    if _is_s3_storage(storage):
        s3_config = {
            "access_key": getattr(storage, "access_key", None),
            "secret_key": getattr(storage, "secret_key", None),
            "endpoint_url": getattr(storage, "endpoint_url", None),
        }

    duckdb_config = {
        "memory_limit": getattr(settings, "DUCKDB_MEMORY_LIMIT", "128MB"),
        "extension_directory": getattr(settings, "DUCKDB_EXTENSION_DIRECTORY", None),
        "temp_directory": getattr(settings, "DUCKDB_TEMP_DIRECTORY", ""),
        "s3_config": s3_config,
    }

    jobs = []
    for org_dir in org_dirs:
        if not org_dir.startswith("org_"):
            continue

        org_path = f"{spans_prefix}/{org_dir}"
        try:
            date_dirs, _ = storage.listdir(org_path)
        except (NotImplementedError, OSError):
            continue

        for date_dir in date_dirs:
            if date_dir in skip_dates:
                continue

            date_path = f"{org_path}/{date_dir}"
            try:
                _, chunk_files = storage.listdir(date_path)
            except (NotImplementedError, OSError):
                continue

            chunks = [f for f in chunk_files if f.endswith(".parquet")]
            if len(chunks) <= 1:
                continue

            # Build DuckDB-accessible paths for child process
            chunk_duckdb_paths = [
                get_duckdb_parquet_path(storage, f"{date_path}/{c}") for c in chunks
            ]
            output_relative = f"{org_path}/{date_dir}.parquet"
            output_duckdb_path = get_duckdb_parquet_path(storage, output_relative)

            is_s3 = output_duckdb_path.startswith("s3://")

            jobs.append(
                {
                    "chunk_duckdb_paths": chunk_duckdb_paths,
                    "write_path": output_duckdb_path
                    if is_s3
                    else output_duckdb_path + ".tmp",
                    "output_path": output_duckdb_path,
                    "is_s3": is_s3,
                    "duckdb_config": duckdb_config,
                    # For finalize (parent-side cleanup)
                    "date_path": date_path,
                    "chunks": chunks,
                    "org_path": org_path,
                    "date_dir": date_dir,
                }
            )

    return jobs


def compact_chunks_in_child(job: dict) -> None:
    """Run DuckDB compaction in a child process. No Django imports needed."""
    import duckdb

    cfg = job["duckdb_config"]
    config = {}
    if cfg.get("extension_directory"):
        config["extension_directory"] = cfg["extension_directory"]
        config["autoinstall_known_extensions"] = "false"

    conn = duckdb.connect(config=config)
    try:
        if cfg.get("memory_limit"):
            conn.execute(f"SET memory_limit = '{cfg['memory_limit']}'")

        temp_dir = cfg.get("temp_directory") or tempfile.gettempdir()
        if os.path.isdir(temp_dir) and os.access(temp_dir, os.W_OK):
            conn.execute(f"SET temp_directory = '{temp_dir}'")

        conn.execute("SET threads = 1")
        conn.execute("SET preserve_insertion_order = false")

        s3 = cfg.get("s3_config")
        if s3:
            conn.load_extension("httpfs")
            if s3.get("access_key"):
                conn.execute(f"SET s3_access_key_id = '{s3['access_key']}'")
            if s3.get("secret_key"):
                conn.execute(f"SET s3_secret_access_key = '{s3['secret_key']}'")
            if s3.get("endpoint_url"):
                endpoint = (
                    s3["endpoint_url"].replace("http://", "").replace("https://", "")
                )
                use_ssl = "true" if s3["endpoint_url"].startswith("https") else "false"
                conn.execute(f"SET s3_endpoint = '{endpoint}'")
                conn.execute(f"SET s3_use_ssl = {use_ssl}")
                conn.execute("SET s3_url_style = 'path'")

        paths_list = ", ".join(
            f"'{duckdb_quote_path(p)}'" for p in job["chunk_duckdb_paths"]
        )
        conn.execute(f"""
            COPY (
                SELECT * FROM read_parquet([{paths_list}])
                ORDER BY timestamp
            ) TO '{duckdb_quote_path(job["write_path"])}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Sync convenience wrappers (used by tests and management commands)
# ---------------------------------------------------------------------------


def promote_spans() -> tuple[int, bool]:
    """Run the full promotion pipeline synchronously (in-process).

    Equivalent to what the async task does, but without process isolation.
    Used by tests and management commands where memory isolation isn't needed.
    """
    org_batches, truncated = fetch_promotable_spans()
    if not org_batches:
        return 0, False

    total_promoted = 0
    for org_id, date_groups, storage_config, column_types in org_batches:
        written_chunks = write_org_parquet_chunks(
            storage_config, org_id, date_groups, column_types
        )
        promoted = delete_promoted_rows(org_id, written_chunks)
        total_promoted += promoted

    if total_promoted:
        logger.info("Promoted %d span rows to cold storage", total_promoted)
    return total_promoted, truncated


def compact_span_chunks() -> int:
    """Run the full compaction pipeline synchronously (in-process).

    Used by tests and management commands.
    """
    jobs = collect_compactable_chunks()
    if not jobs:
        return 0

    compacted = 0
    for job in jobs:
        try:
            # Run compaction in-process (no child) for simplicity in tests
            compact_chunks_in_child(job)
            finalize_compaction(job)
            compacted += len(job["chunks"])
        except Exception:
            logger.error(
                "Failed to compact chunks in %s", job["date_path"], exc_info=True
            )

    if compacted:
        logger.info("Compacted %d span chunk files", compacted)
    return compacted


def _write_chunk_parquet(storage, org_id: int, date_str: str, rows: list[tuple]) -> str:
    """Write a chunk Parquet file using a storage object directly.

    Convenience wrapper for tests that pass a storage object rather than
    a serializable config dict.
    """
    storage_config = _get_storage_config(storage)
    column_types = dict(SPAN_PARQUET_COLUMN_TYPES)
    return _write_chunk_parquet_isolated(
        storage_config, org_id, date_str, rows, column_types
    )


def finalize_compaction(job: dict) -> None:
    """Post-compaction cleanup in parent process (file renames, deletes)."""
    storage = get_cold_storage_backend()

    # Atomic rename on filesystem
    if not job["is_s3"]:
        os.rename(job["write_path"], job["output_path"])

    # Delete chunk files
    for chunk in job["chunks"]:
        try:
            storage.delete(f"{job['date_path']}/{chunk}")
        except Exception:
            logger.warning("Failed to delete chunk %s/%s", job["date_path"], chunk)

    # Try to remove the empty date directory (filesystem only)
    if not job["is_s3"]:
        try:
            os.rmdir(storage.path(job["date_path"]))
        except OSError:
            pass

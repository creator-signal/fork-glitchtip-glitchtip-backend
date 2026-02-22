"""
Cold storage query layer for Performance Monitoring V2.

Queries span Parquet files produced by the promotion job.
Handles two file layouts:
- Chunk files: org_{id}/{date}/chunk_*.parquet (pre-compaction)
- Compacted files: org_{id}/{date}.parquet (post-compaction)
"""

import logging

from glitchtip.cold_storage import (
    COLD_STORAGE_PREFIX,
    get_cold_storage_backend,
    get_duckdb_connection,
    get_duckdb_parquet_path,
    is_duckdb_available,
)

logger = logging.getLogger(__name__)

TABLE_NAME = "performance_spans"

SPAN_PARQUET_COLUMN_TYPES = {
    "organization_id": "INTEGER",
    "project_id": "INTEGER",
    "transaction_name": "VARCHAR",
    "span_id": "VARCHAR",
    "transaction_id": "VARCHAR",
    "op": "VARCHAR",
    "description": "VARCHAR",
    "duration": "DOUBLE",
    "timestamp": "TIMESTAMP WITH TIME ZONE",
}


def _enumerate_parquet_files(storage, org_id: int) -> list[str]:
    """
    Enumerate all parquet files for an org, handling both layouts.

    Returns list of DuckDB-readable paths.
    """
    org_prefix = f"{COLD_STORAGE_PREFIX}/{TABLE_NAME}/org_{org_id}"

    try:
        subdirs, flat_files = storage.listdir(org_prefix)
    except (NotImplementedError, OSError):
        return []

    paths = []

    # Compacted flat files: org_{id}/{date}.parquet
    for f in flat_files:
        if f.endswith(".parquet"):
            relative = f"{org_prefix}/{f}"
            paths.append(get_duckdb_parquet_path(storage, relative))

    # Chunk files in date subdirectories: org_{id}/{date}/chunk_*.parquet
    for subdir in subdirs:
        subdir_path = f"{org_prefix}/{subdir}"
        try:
            _, chunk_files = storage.listdir(subdir_path)
        except (NotImplementedError, OSError):
            continue
        for f in chunk_files:
            if f.endswith(".parquet"):
                relative = f"{subdir_path}/{f}"
                paths.append(get_duckdb_parquet_path(storage, relative))

    return paths


def query_span_groups_for_transaction(
    org_id: int,
    transaction_group_id: int,
    start_dt,
    end_dt,
    limit: int = 50,
) -> list[dict]:
    """
    Query span groups for a specific transaction.

    Returns list of {op, description, count, avg_duration, p95_duration, total_time}
    grouped by (op, description), ordered by total_time DESC.
    """
    if not is_duckdb_available():
        return []

    storage = get_cold_storage_backend()
    if not storage:
        return []

    parquet_files = _enumerate_parquet_files(storage, org_id)
    if not parquet_files:
        return []

    # We need the transaction name from the group
    from apps.performance.models import TransactionGroup

    try:
        group = TransactionGroup.objects.get(id=transaction_group_id)
    except TransactionGroup.DoesNotExist:
        return []

    results = []
    duck_conn = get_duckdb_connection(storage)
    try:
        for path in parquet_files:
            sql = """
                SELECT
                    op,
                    description,
                    COUNT(*) as count,
                    AVG(duration) as avg_duration,
                    PERCENTILE_DISC(0.95) WITHIN GROUP (ORDER BY duration) as p95_duration,
                    SUM(duration) as total_time
                FROM read_parquet($1)
                WHERE transaction_name = $2
                  AND timestamp >= $3
                  AND timestamp < $4
                GROUP BY op, description
            """
            try:
                rows = duck_conn.execute(
                    sql, [path, group.transaction, start_dt, end_dt]
                ).fetchall()
                results.extend(rows)
            except Exception:
                logger.error("Error reading parquet file %s", path, exc_info=True)
    finally:
        duck_conn.close()

    if not results:
        return []

    # Merge results across files
    merged: dict[tuple[str, str], dict] = {}
    for op, desc, count, avg_dur, p95, total in results:
        key = (op, desc)
        if key in merged:
            existing = merged[key]
            old_count = existing["count"]
            new_count = old_count + count
            existing["avg_duration"] = (
                existing["avg_duration"] * old_count + avg_dur * count
            ) / new_count
            existing["count"] = new_count
            existing["p95_duration"] = max(existing["p95_duration"] or 0, p95 or 0)
            existing["total_time"] += total
        else:
            merged[key] = {
                "op": op,
                "description": desc or "",
                "count": count,
                "avg_duration": avg_dur or 0,
                "p95_duration": p95 or 0,
                "total_time": total or 0,
            }

    return sorted(merged.values(), key=lambda x: x["total_time"], reverse=True)[:limit]


def query_slow_queries(
    org_id: int,
    project_ids: list[int] | None,
    start_dt,
    end_dt,
    limit: int = 50,
) -> list[dict]:
    """
    Query slow database spans.

    Returns list of {op, description, count, avg_duration, total_time}
    filtered to op LIKE 'db%', grouped by (op, description),
    ordered by avg_duration DESC.
    """
    if not is_duckdb_available():
        return []

    storage = get_cold_storage_backend()
    if not storage:
        return []

    parquet_files = _enumerate_parquet_files(storage, org_id)
    if not parquet_files:
        return []

    results = []
    duck_conn = get_duckdb_connection(storage)
    try:
        for path in parquet_files:
            project_filter = ""
            params = [path, start_dt, end_dt]
            if project_ids:
                placeholders = ", ".join(f"${i + 4}" for i in range(len(project_ids)))
                project_filter = f"AND project_id IN ({placeholders})"
                params.extend(project_ids)

            sql = f"""
                SELECT
                    op,
                    description,
                    COUNT(*) as count,
                    AVG(duration) as avg_duration,
                    SUM(duration) as total_time
                FROM read_parquet($1)
                WHERE op LIKE 'db%'
                  AND timestamp >= $2
                  AND timestamp < $3
                  {project_filter}
                GROUP BY op, description
            """
            try:
                rows = duck_conn.execute(sql, params).fetchall()
                results.extend(rows)
            except Exception:
                logger.error("Error reading parquet file %s", path, exc_info=True)
    finally:
        duck_conn.close()

    if not results:
        return []

    # Merge results across files
    merged: dict[tuple[str, str], dict] = {}
    for op, desc, count, avg_dur, total in results:
        key = (op, desc)
        if key in merged:
            existing = merged[key]
            old_count = existing["count"]
            new_count = old_count + count
            existing["avg_duration"] = (
                existing["avg_duration"] * old_count + avg_dur * count
            ) / new_count
            existing["count"] = new_count
            existing["total_time"] += total
        else:
            merged[key] = {
                "op": op,
                "description": desc or "",
                "count": count,
                "avg_duration": avg_dur or 0,
                "total_time": total or 0,
            }

    return sorted(merged.values(), key=lambda x: x["avg_duration"], reverse=True)[
        :limit
    ]

"""
Cold storage query layer for Performance Monitoring V2.

Queries span Parquet files produced by the promotion job.
Handles two file layouts:
- Chunk files: org_{id}/{date}/chunk_*.parquet (pre-compaction)
- Compacted files: org_{id}/{date}.parquet (post-compaction)
"""

import logging
from datetime import datetime

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


def _date_in_range(date_str: str, start_dt: datetime, end_dt: datetime) -> bool:
    """Check if a YYYYMMDD date string falls within the query range."""
    try:
        file_date = datetime.strptime(date_str, "%Y%m%d").replace(
            tzinfo=start_dt.tzinfo
        )
    except ValueError:
        return True  # Unknown format — include to be safe
    # Include if the file's day overlaps [start_dt, end_dt)
    from datetime import timedelta

    return file_date < end_dt and file_date + timedelta(days=1) > start_dt


def _enumerate_parquet_files(
    storage, org_id: int, start_dt: datetime, end_dt: datetime
) -> list[str]:
    """
    Enumerate parquet files for an org within a date range.

    Prunes files by date from the directory/file name to avoid reading
    irrelevant data.

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
            date_str = f.removesuffix(".parquet")
            if _date_in_range(date_str, start_dt, end_dt):
                relative = f"{org_prefix}/{f}"
                paths.append(get_duckdb_parquet_path(storage, relative))

    # Chunk files in date subdirectories: org_{id}/{date}/chunk_*.parquet
    for subdir in subdirs:
        if not _date_in_range(subdir, start_dt, end_dt):
            continue
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
    start_dt: datetime,
    end_dt: datetime,
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

    parquet_files = _enumerate_parquet_files(storage, org_id, start_dt, end_dt)
    if not parquet_files:
        return []

    # We need the transaction name from the group
    from apps.performance.models import TransactionGroup

    try:
        group = TransactionGroup.objects.get(
            id=transaction_group_id, organization_id=org_id
        )
    except TransactionGroup.DoesNotExist:
        return []

    duck_conn = get_duckdb_connection(storage)
    try:
        paths_list = ", ".join(f"'{p}'" for p in parquet_files)
        sql = f"""
            SELECT
                op,
                description,
                COUNT(*) as count,
                AVG(duration) as avg_duration,
                PERCENTILE_DISC(0.95) WITHIN GROUP (ORDER BY duration) as p95_duration,
                SUM(duration) as total_time
            FROM read_parquet([{paths_list}])
            WHERE transaction_name = $1
              AND timestamp >= $2
              AND timestamp < $3
            GROUP BY op, description
            ORDER BY total_time DESC
            LIMIT $4
        """
        rows = duck_conn.execute(
            sql, [group.transaction, start_dt, end_dt, limit]
        ).fetchall()
    except Exception:
        logger.error("Error querying span groups", exc_info=True)
        return []
    finally:
        duck_conn.close()

    return [
        {
            "op": row[0],
            "description": row[1] or "",
            "count": row[2],
            "avg_duration": row[3] or 0,
            "p95_duration": row[4] or 0,
            "total_time": row[5] or 0,
        }
        for row in rows
    ]


def query_span_groups(
    org_id: int,
    project_ids: list[int] | None,
    start_dt: datetime,
    end_dt: datetime,
    op_filter: str | None = None,
    sort: str = "-total_time",
    limit: int = 50,
) -> list[dict]:
    """
    Query span groups across the organization.

    Args:
        op_filter: Optional op prefix filter (e.g. "db" for database spans).
        sort: Sort field, prefixed with - for descending.
            Supported: total_time, avg_duration, count.

    Returns list of {op, description, count, avg_duration, p95_duration, total_time}
    grouped by (op, description).
    """
    if not is_duckdb_available():
        return []

    storage = get_cold_storage_backend()
    if not storage:
        return []

    parquet_files = _enumerate_parquet_files(storage, org_id, start_dt, end_dt)
    if not parquet_files:
        return []

    # Build optional WHERE clauses
    extra_where = ""
    params: list = [start_dt, end_dt]
    param_idx = 3

    if op_filter:
        extra_where += f" AND op LIKE ${param_idx}"
        params.append(f"{op_filter}%")
        param_idx += 1

    if project_ids:
        placeholders = ", ".join(f"${param_idx + i}" for i in range(len(project_ids)))
        extra_where += f" AND project_id IN ({placeholders})"
        params.extend(project_ids)
        param_idx += len(project_ids)

    # Map sort parameter to SQL ORDER BY
    sort_field = sort.lstrip("-")
    sort_dir = "DESC" if sort.startswith("-") else "ASC"
    allowed_sorts = {"total_time", "avg_duration", "count"}
    if sort_field not in allowed_sorts:
        sort_field = "total_time"
        sort_dir = "DESC"

    params.append(limit)

    duck_conn = get_duckdb_connection(storage)
    try:
        paths_list = ", ".join(f"'{p}'" for p in parquet_files)
        sql = f"""
            SELECT
                op,
                description,
                COUNT(*) as count,
                AVG(duration) as avg_duration,
                PERCENTILE_DISC(0.95) WITHIN GROUP (ORDER BY duration) as p95_duration,
                SUM(duration) as total_time
            FROM read_parquet([{paths_list}])
            WHERE timestamp >= $1
              AND timestamp < $2
              {extra_where}
            GROUP BY op, description
            ORDER BY {sort_field} {sort_dir}
            LIMIT ${param_idx}
        """
        rows = duck_conn.execute(sql, params).fetchall()
    except Exception:
        logger.error("Error querying span groups", exc_info=True)
        return []
    finally:
        duck_conn.close()

    return [
        {
            "op": row[0],
            "description": row[1] or "",
            "count": row[2],
            "avg_duration": row[3] or 0,
            "p95_duration": row[4] or 0,
            "total_time": row[5] or 0,
        }
        for row in rows
    ]

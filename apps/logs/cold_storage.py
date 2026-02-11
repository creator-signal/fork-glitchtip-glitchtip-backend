"""
Cold storage utilities for archiving log partitions to Parquet via standalone DuckDB.

Auto-enables when a storage bucket is configured (GLITCHTIP_COLD_STORAGE_BUCKET
or AWS_STORAGE_BUCKET_NAME). Old log partitions are archived to Parquet files
and queryable via DuckDB's in-process engine.

Uses standalone DuckDB (not pg_duckdb extension) so cold storage works with
any PostgreSQL provider including RDS, Aurora, Cloud SQL, etc. No Postgres
extensions required — DuckDB runs in the Python process, completely
independent of database connection pooling.

Architecture (per-org files):
1. Export each org's data from a partition to separate Parquet files
2. Path structure: cold_storage/{table}/org_{id}/{date}.parquet
3. Query cold storage by computing paths from (org_id, date_range)
4. No cross-org data in same file - enables future sharding

File deletion uses django-storages for backend abstraction (S3, GCS, Azure, etc.).
High-scale deployments can disable manual cleanup and use S3 lifecycle policies.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from django.conf import settings
from django.core.files.storage import storages
from django.db import connection

logger = logging.getLogger(__name__)

# Prefix for all cold storage files to prevent collisions with other data
COLD_STORAGE_PREFIX = "cold_storage"


@dataclass
class ColdStorageConfig:
    """Configuration for cold storage."""

    bucket: str
    endpoint_url: str | None
    access_key_id: str | None
    secret_access_key: str | None

    @classmethod
    def from_settings(cls) -> "ColdStorageConfig":
        # Default to AWS_STORAGE_BUCKET_NAME (shared with media/sourcemaps)
        # Users can override with GLITCHTIP_COLD_STORAGE_BUCKET for separation
        default_bucket = getattr(settings, "AWS_STORAGE_BUCKET_NAME", None)
        bucket = getattr(settings, "GLITCHTIP_COLD_STORAGE_BUCKET", default_bucket)

        return cls(
            bucket=bucket,
            endpoint_url=getattr(settings, "AWS_S3_ENDPOINT_URL", None),
            access_key_id=getattr(settings, "AWS_ACCESS_KEY_ID", None),
            secret_access_key=getattr(settings, "AWS_SECRET_ACCESS_KEY", None),
        )


def get_cold_storage_backend(config: ColdStorageConfig | None = None):
    """
    Get the django-storages backend for cold storage.

    Uses GLITCHTIP_COLD_STORAGE alias if configured, otherwise creates
    an S3 storage instance with the cold storage bucket.

    Args:
        config: Cold storage configuration with bucket name

    Returns None if no suitable storage backend is available.
    """
    # Check for dedicated cold storage configuration in STORAGES
    if "cold" in storages.backends:
        return storages["cold"]

    if config is None:
        config = ColdStorageConfig.from_settings()

    # Try to create an S3 storage instance with the cold bucket
    try:
        from storages.backends.s3 import S3Storage

        return S3Storage(bucket_name=config.bucket)
    except ImportError:
        pass

    # Fall back: check if default storage is a cloud backend
    try:
        default = storages["default"]
        backend_class = default.__class__.__name__
        cloud_backends = (
            "S3Boto3Storage",
            "S3Storage",
            "GoogleCloudStorage",
            "GCloudStorage",
            "AzureStorage",
        )
        if backend_class in cloud_backends:
            logger.warning(
                f"Using default storage for cold storage deletion. "
                f"Bucket may not match cold storage bucket ({config.bucket})."
            )
            return default
    except Exception:
        pass

    return None


def is_duckdb_available() -> bool:
    """
    Check if DuckDB cold storage is enabled.

    Auto-enables when a storage bucket is configured (either
    GLITCHTIP_COLD_STORAGE_BUCKET or AWS_STORAGE_BUCKET_NAME).
    Override with GLITCHTIP_ENABLE_DUCKDB=false to disable even when
    storage exists (e.g., horizontally-scaled PaaS with S3 for media only).
    """
    override = getattr(settings, "GLITCHTIP_ENABLE_DUCKDB", None)
    if override is not None:
        return str(override).lower() == "true"

    # Auto-detect: enable if a storage bucket is available
    bucket = getattr(settings, "GLITCHTIP_COLD_STORAGE_BUCKET", None)
    if not bucket:
        bucket = getattr(settings, "AWS_STORAGE_BUCKET_NAME", None)
    return bool(bucket)


def get_duckdb_connection(config: ColdStorageConfig | None = None):
    """
    Create a standalone DuckDB connection configured for S3 access.

    Returns an in-process DuckDB connection with httpfs loaded and S3
    credentials configured. Each call creates a fresh connection —
    no session state leaks, no interaction with PostgreSQL connection pooling.
    """
    import duckdb

    if config is None:
        config = ColdStorageConfig.from_settings()

    conn = duckdb.connect()

    # Load httpfs for S3 access
    conn.install_extension("httpfs")
    conn.load_extension("httpfs")

    # Configure S3 credentials
    if config.access_key_id:
        conn.execute(f"SET s3_access_key_id = '{config.access_key_id}';")
    if config.secret_access_key:
        conn.execute(f"SET s3_secret_access_key = '{config.secret_access_key}';")

    if config.endpoint_url:
        # Strip protocol prefix for DuckDB
        endpoint = config.endpoint_url.replace("http://", "").replace("https://", "")
        use_ssl = "true" if config.endpoint_url.startswith("https") else "false"
        conn.execute(f"SET s3_endpoint = '{endpoint}';")
        conn.execute(f"SET s3_use_ssl = {use_ssl};")
        conn.execute("SET s3_url_style = 'path';")

    return conn


def get_org_cold_s3_path(
    config: ColdStorageConfig, table_name: str, org_id: int, date_str: str
) -> str:
    """
    Generate the S3 path for an org's daily Parquet file.

    Path structure: s3://bucket/cold_storage/logs/org_{id}/{date}.parquet
    This isolates each org's data for efficient single-file queries.
    """
    return f"s3://{config.bucket}/{COLD_STORAGE_PREFIX}/{table_name}/org_{org_id}/{date_str}.parquet"


def get_org_cold_storage_path(table_name: str, org_id: int, date_str: str) -> str:
    """Get the storage-relative path for an org's daily Parquet file (without bucket)."""
    return f"{COLD_STORAGE_PREFIX}/{table_name}/org_{org_id}/{date_str}.parquet"


def archive_partition_per_org(
    partition_name: str,
    date_str: str,
    table_name: str = "logs_logevent",
    config: ColdStorageConfig | None = None,
) -> list[tuple[int, str]]:
    """
    Archive a partition to S3 as per-org Parquet files.

    Each organization's data is exported to a separate file:
    cold_storage/logs/org_{id}/{date}.parquet

    Reads from PostgreSQL via Django's connection, writes Parquet via
    standalone DuckDB. No pg_duckdb extension required — works with
    RDS, Aurora, Cloud SQL, and any other PostgreSQL provider.

    Args:
        partition_name: Name of the partition to archive (e.g., "logs_logevent_20260115")
        date_str: Date string for the partition (e.g., "20260115")
        table_name: Parent table name
        config: Cold storage configuration

    Returns:
        List of (org_id, s3_path) tuples for archived files
    """
    if not is_duckdb_available():
        logger.info("duckdb not available, skipping archival")
        return []

    if config is None:
        config = ColdStorageConfig.from_settings()

    archived_files = []

    try:
        with connection.cursor() as cursor:
            # Find all orgs with data in this partition
            cursor.execute(
                f"SELECT DISTINCT organization_id FROM {partition_name} ORDER BY organization_id;"
            )
            org_ids = [row[0] for row in cursor.fetchall()]

            if not org_ids:
                logger.info(f"No data in {partition_name}, skipping")
                return []

            logger.info(
                f"Archiving {partition_name} for {len(org_ids)} orgs: {org_ids}"
            )

            # Export each org's data to a separate Parquet file
            for org_id in org_ids:
                s3_path = get_org_cold_s3_path(config, table_name, org_id, date_str)

                # Read org's data from PostgreSQL, sorted for optimal row group skipping
                cursor.execute(
                    f"""
                    SELECT id, trace_id, organization_id, project_id, span_id,
                           level, severity_number, body, service, data
                    FROM {partition_name}
                    WHERE organization_id = %s
                    ORDER BY service, level, id
                    """,
                    [org_id],
                )
                columns = [desc[0] for desc in cursor.description]
                rows = cursor.fetchall()

                if not rows:
                    continue

                # Write to S3 via standalone DuckDB
                import json

                # Normalize values for DuckDB (UUIDs to strings, dicts to JSON)
                clean_rows = []
                for row in rows:
                    clean_row = []
                    for val in row:
                        if isinstance(val, dict):
                            clean_row.append(json.dumps(val))
                        elif hasattr(val, "hex"):  # UUID
                            clean_row.append(str(val))
                        else:
                            clean_row.append(val)
                    clean_rows.append(clean_row)

                duck_conn = get_duckdb_connection(config)
                try:
                    # DuckDB can create tables from Python data via VALUES
                    # or by registering a view over Python objects
                    duck_conn.execute(
                        f"CREATE TABLE export_data({', '.join(columns)})"
                    )
                    duck_conn.executemany(
                        f"INSERT INTO export_data VALUES ({', '.join(['?'] * len(columns))})",
                        clean_rows,
                    )
                    duck_conn.execute(
                        f"COPY export_data TO '{s3_path}' (FORMAT PARQUET, COMPRESSION ZSTD);"
                    )
                    archived_files.append((org_id, s3_path))
                    logger.debug(f"Archived org {org_id} to {s3_path}")
                finally:
                    duck_conn.close()

        logger.info(
            f"Archived {partition_name}: {len(archived_files)} org files created"
        )
        return archived_files

    except Exception as e:
        logger.error(f"Failed to archive {partition_name}: {e}")
        raise


def detach_partition(partition_name: str, parent_table: str = "logs_logevent") -> None:
    """
    Detach a partition from its parent table.

    This is done before dropping the partition after archival.
    """
    with connection.cursor() as cursor:
        cursor.execute(f"ALTER TABLE {parent_table} DETACH PARTITION {partition_name};")
    logger.info(f"Detached partition {partition_name} from {parent_table}")


def drop_partition(partition_name: str) -> None:
    """Drop a partition table after it has been archived."""
    with connection.cursor() as cursor:
        cursor.execute(f"DROP TABLE IF EXISTS {partition_name};")
    logger.info(f"Dropped partition {partition_name}")


def archive_and_swap_partition(
    partition_name: str,
    table_name: str = "logs_logevent",
    config: ColdStorageConfig | None = None,
) -> bool:
    """
    Full archival workflow: Export per-org files → Detach → Drop partition.

    This is the main entry point for archiving a partition. Each org's data
    is exported to a separate Parquet file for efficient per-org queries.

    Args:
        partition_name: Name of the partition to archive (e.g., logs_logevent_20260117)
        table_name: Parent table name
        config: Cold storage configuration

    Returns:
        True if archival succeeded, False if skipped (duckdb not available)
    """
    if not is_duckdb_available():
        logger.info("duckdb not available, skipping archival workflow")
        return False

    if config is None:
        config = ColdStorageConfig.from_settings()

    # Extract date from partition name (format: tablename_YYYYMMDD)
    # e.g., "logs_logevent_20260117" -> "20260117"
    parts = partition_name.split("_")
    date_str = None
    for part in parts:
        if len(part) == 8 and part.isdigit():
            date_str = part
            break

    if not date_str:
        logger.error(f"Could not extract date from partition name: {partition_name}")
        return False

    # Step 1: Export per-org files to S3
    archived_files = archive_partition_per_org(
        partition_name, date_str, table_name, config
    )
    if not archived_files:
        logger.info(f"No data archived from {partition_name}")
        # Still proceed to drop empty partition

    # Step 2: Detach partition from parent table
    detach_partition(partition_name, table_name)

    # Step 3: Drop the original partition (and its hash sub-partitions via CASCADE)
    drop_partition(partition_name)

    logger.info(
        f"Successfully archived {partition_name}: {len(archived_files)} org files"
    )
    return True


def get_partitions_older_than(
    table_name: str, days: int, partition_suffix: str = ""
) -> list[tuple[str, datetime]]:
    """
    Get list of partitions older than the specified number of days.

    Args:
        table_name: Base table name (e.g., "logs_logevent")
        days: Number of days - partitions older than this are returned
        partition_suffix: Optional suffix to match (e.g., "_archive" for views)

    Returns list of (partition_name, partition_date) tuples.
    """
    from datetime import timedelta

    from django.utils import timezone

    cutoff_date = timezone.now() - timedelta(days=days)

    # Build regex pattern based on whether we're looking for views or tables
    if partition_suffix:
        # Looking for archive views (e.g., logs_logevent_20260128_h0_archive)
        pattern = f"^{table_name}_[0-9]{{8}}_h[0-9]+{partition_suffix}$"
        source_table = "pg_views"
        name_column = "viewname"
    else:
        # Looking for partition tables (e.g., logs_logevent_20260128)
        pattern = f"^{table_name}_[0-9]{{8}}$"
        source_table = "pg_tables"
        name_column = "tablename"

    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT {name_column} FROM {source_table}
            WHERE {name_column} LIKE %s
            AND {name_column} ~ %s
            ORDER BY {name_column};
            """,
            [f"{table_name}_%", pattern],
        )
        partitions = []
        for (name,) in cursor.fetchall():
            # Extract date from partition name (format: tablename_YYYYMMDD or tablename_YYYYMMDD_h0_archive)
            parts = name.replace(partition_suffix, "").split("_")
            # Find the date part (8 digits)
            date_str = None
            for part in parts:
                if len(part) == 8 and part.isdigit():
                    date_str = part
                    break

            if date_str:
                try:
                    partition_date = datetime.strptime(date_str, "%Y%m%d")
                    partition_date = timezone.make_aware(partition_date)
                    if partition_date < cutoff_date:
                        partitions.append((name, partition_date))
                except ValueError:
                    continue

    return partitions


def query_cold_storage(
    org_id: int,
    start_date: str,
    end_date: str,
    table_name: str = "logs_logevent",
    config: ColdStorageConfig | None = None,
    filters: dict | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """
    Query cold storage for an org's data within a date range.

    Uses standalone DuckDB to read Parquet files from S3.
    No PostgreSQL extension required.

    Args:
        org_id: Organization ID
        start_date: Start date string (YYYYMMDD) - used for filtering, not path
        end_date: End date string (YYYYMMDD) - used for filtering, not path
        table_name: Table name for path construction
        config: Cold storage configuration
        filters: Optional dict with filter conditions (level, service, body_search)
        limit: Max rows to return
        offset: Rows to skip

    Returns:
        List of row dicts from cold storage
    """
    if not is_duckdb_available():
        return []

    if config is None:
        config = ColdStorageConfig.from_settings()

    # Use glob pattern to read all files for this org
    glob_path = f"s3://{config.bucket}/{COLD_STORAGE_PREFIX}/{table_name}/org_{org_id}/*.parquet"

    # Build WHERE clause
    where_parts = [f"organization_id = {org_id}"]
    if filters:
        if "level" in filters:
            where_parts.append(f"level = {int(filters['level'])}")
        if "service" in filters:
            svc = filters["service"].replace("'", "''")
            where_parts.append(f"service = '{svc}'")
        if "body_search" in filters:
            term = filters["body_search"].replace("'", "''")
            where_parts.append(f"body ILIKE '%{term}%'")

    where_clause = " AND ".join(where_parts)

    try:
        duck_conn = get_duckdb_connection(config)
        try:
            query = f"""
                SELECT id, trace_id, organization_id, project_id, span_id,
                       level, severity_number, body, service, data
                FROM read_parquet('{glob_path}')
                WHERE {where_clause}
                ORDER BY id DESC
                LIMIT {limit} OFFSET {offset};
            """
            result = duck_conn.execute(query)
            columns = [desc[0] for desc in result.description]
            return [dict(zip(columns, row)) for row in result.fetchall()]
        finally:
            duck_conn.close()

    except Exception as e:
        error_str = str(e)
        # Handle missing files gracefully
        if "No files found" in error_str or "Could not open" in error_str:
            logger.debug(f"No cold storage files found for org {org_id} in date range")
            return []
        logger.error(f"Cold storage query failed: {e}")
        raise


def cleanup_cold_storage_for_org(
    org_id: int,
    retention_days: int,
    table_name: str = "logs_logevent",
    config: ColdStorageConfig | None = None,
) -> int:
    """
    Delete cold storage files older than retention period for an org.

    Computes paths directly from a 30-day window before the retention cutoff.
    No external state (cache/DB) needed - storage.delete() is a no-op on
    most backends if the file doesn't exist.

    Args:
        org_id: Organization ID
        retention_days: Delete files older than this many days
        table_name: Table name for path construction
        config: Cold storage configuration

    Returns:
        Number of files deleted
    """
    if config is None:
        config = ColdStorageConfig.from_settings()

    storage = get_cold_storage_backend(config)
    if not storage:
        logger.warning("No storage backend available for cleanup")
        return 0

    from datetime import timedelta

    from django.utils import timezone

    cutoff = timezone.now() - timedelta(days=retention_days)
    deleted_count = 0

    # Sweep a 30-day window before the retention cutoff.
    # Files older than cutoff-30d would have been cleaned in prior runs.
    cleanup_window_days = 30
    for day_offset in range(cleanup_window_days):
        file_date = cutoff - timedelta(days=day_offset)
        date_str = file_date.strftime("%Y%m%d")
        storage_path = get_org_cold_storage_path(table_name, org_id, date_str)
        try:
            storage.delete(storage_path)
            deleted_count += 1
            logger.debug(f"Deleted {storage_path}")
        except Exception:
            # Most backends no-op on missing files; ignore errors
            pass

    if deleted_count:
        logger.info(f"Deleted {deleted_count} cold files for org {org_id}")

    return deleted_count


def cleanup_all_cold_storage(
    retention_days: int | None = None,
    table_name: str = "logs_logevent",
    config: ColdStorageConfig | None = None,
) -> int:
    """
    Delete cold storage files older than retention period for all orgs.

    Only runs if GLITCHTIP_COLD_STORAGE_CLEANUP_ENABLED is True.
    For high-scale deployments, disable this and use S3 lifecycle policies.

    Args:
        retention_days: Override retention period (uses settings if not provided)
        table_name: Table name for path construction
        config: Cold storage configuration

    Returns:
        Total number of files deleted
    """
    cleanup_enabled = getattr(settings, "GLITCHTIP_COLD_STORAGE_CLEANUP_ENABLED", True)
    if not cleanup_enabled:
        logger.info("Cold storage cleanup disabled, skipping (use lifecycle policies)")
        return 0

    if retention_days is None:
        retention_days = getattr(settings, "GLITCHTIP_COLD_STORAGE_RETENTION_DAYS", 90)

    if config is None:
        config = ColdStorageConfig.from_settings()

    # Import here to avoid circular imports
    from apps.organizations_ext.models import Organization

    total_deleted = 0

    # Only cleanup orgs that have cold storage (paid tier)
    # For now, iterate all orgs - could filter by subscription status
    for org in Organization.objects.all().iterator():
        deleted = cleanup_cold_storage_for_org(
            org.id, retention_days, table_name, config
        )
        total_deleted += deleted

    logger.info(f"Cold storage cleanup complete: {total_deleted} files deleted")
    return total_deleted

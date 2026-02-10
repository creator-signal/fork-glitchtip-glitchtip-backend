"""
Cold storage utilities for archiving log partitions to S3 via pg_duckdb.

This module implements the "progressive enhancement" pattern:
- If pg_duckdb is installed: Archive partitions to Parquet on S3
- If not: Gracefully skip archival (partitions stay in Postgres or get dropped)

Architecture (per-org files):
1. Export each org's data from a partition to separate Parquet files
2. Path structure: cold_storage/logs/org_{id}/{date}.parquet
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


def is_pg_duckdb_available() -> bool:
    """
    Check if pg_duckdb extension is installed and usable.

    This is the "progressive enhancement" check - if False, cold storage
    features are disabled but GlitchTip continues to work normally.

    Checks GLITCHTIP_ENABLE_DUCKDB setting first:
    - "true"/"false": Returns immediately without hitting the DB
    - None (unset): Falls through to DB check
    """
    setting = getattr(settings, "GLITCHTIP_ENABLE_DUCKDB", None)
    if setting is not None:
        return setting.lower() == "true"

    try:
        with connection.cursor() as cursor:
            # Check extension exists
            cursor.execute(
                "SELECT 1 FROM pg_extension WHERE extname = 'pg_duckdb' LIMIT 1;"
            )
            if cursor.fetchone() is None:
                return False

            # Check duckdb schema exists (requires shared_preload_libraries)
            cursor.execute(
                "SELECT 1 FROM pg_namespace WHERE nspname = 'duckdb' LIMIT 1;"
            )
            return cursor.fetchone() is not None
    except Exception:
        return False


def setup_duckdb_s3_credentials(config: ColdStorageConfig) -> None:
    """
    Configure DuckDB's S3 credentials within pg_duckdb.

    Must be called once per session before any S3 operations.
    Uses duckdb.create_simple_secret() to configure S3 access.
    """
    with connection.cursor() as cursor:
        # Clean endpoint URL (remove protocol prefix)
        endpoint = ""
        use_ssl = "true"
        if config.endpoint_url:
            endpoint = config.endpoint_url.replace("http://", "").replace(
                "https://", ""
            )
            use_ssl = "true" if config.endpoint_url.startswith("https") else "false"

        # Create S3 secret for DuckDB
        # All parameters are text type
        cursor.execute(
            """
            SELECT duckdb.create_simple_secret(
                %s,  -- type
                %s,  -- key_id
                %s,  -- secret
                '',  -- session_token
                '',  -- region
                'path',  -- url_style
                '',  -- provider
                %s,  -- endpoint
                '',  -- scope
                '',  -- validation
                %s   -- use_ssl
            );
            """,
            ["S3", config.access_key_id, config.secret_access_key, endpoint, use_ssl],
        )


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


# Legacy functions for backwards compatibility during migration
def get_partition_s3_path(
    config: ColdStorageConfig, table_name: str, partition_name: str
) -> str:
    """Generate the S3 path for a partition's Parquet file (legacy, mixed-org)."""
    return f"s3://{config.bucket}/{COLD_STORAGE_PREFIX}/{table_name}/{partition_name}.parquet"


def get_partition_storage_path(table_name: str, partition_name: str) -> str:
    """Get the storage-relative path for a partition's Parquet file (legacy)."""
    return f"{COLD_STORAGE_PREFIX}/{table_name}/{partition_name}.parquet"


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

    The data is sorted by (service, level, id) to enable
    efficient row group skipping during searches.

    Args:
        partition_name: Name of the partition to archive (e.g., "logs_logevent_20260115")
        date_str: Date string for the partition (e.g., "20260115")
        table_name: Parent table name
        config: Cold storage configuration

    Returns:
        List of (org_id, s3_path) tuples for archived files
    """
    if not is_pg_duckdb_available():
        logger.info("pg_duckdb not available, skipping archival")
        return []

    if config is None:
        config = ColdStorageConfig.from_settings()

    archived_files = []

    try:
        setup_duckdb_s3_credentials(config)

        with connection.cursor() as cursor:
            # Find all orgs with data in this partition
            # This query runs on PostgreSQL (not DuckDB)
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

            # Enable DuckDB execution for exports
            cursor.execute("SET duckdb.force_execution = true;")

            # Export each org's data to a separate file
            for org_id in org_ids:
                s3_path = get_org_cold_s3_path(config, table_name, org_id, date_str)

                # Export org's data, sorted for optimal row group skipping
                export_sql = f"""
                    COPY (
                        SELECT * FROM {partition_name}
                        WHERE organization_id = %s
                        ORDER BY service, level, id
                    ) TO '{s3_path}' (FORMAT PARQUET, COMPRESSION ZSTD);
                """
                cursor.execute(export_sql, [org_id])
                archived_files.append((org_id, s3_path))
                logger.debug(f"Archived org {org_id} to {s3_path}")

        logger.info(
            f"Archived {partition_name}: {len(archived_files)} org files created"
        )
        return archived_files

    except Exception as e:
        logger.error(f"Failed to archive {partition_name}: {e}")
        raise


def archive_partition_to_s3(
    partition_name: str,
    table_name: str = "logs_logevent",
    config: ColdStorageConfig | None = None,
) -> str | None:
    """
    Archive a partition to S3 as single Parquet file (legacy, mixed-org).

    DEPRECATED: Use archive_partition_per_org for new code.
    Kept for backwards compatibility with existing archives.
    """
    if not is_pg_duckdb_available():
        logger.info("pg_duckdb not available, skipping archival")
        return None

    if config is None:
        config = ColdStorageConfig.from_settings()

    s3_path = get_partition_s3_path(config, table_name, partition_name)

    try:
        setup_duckdb_s3_credentials(config)

        with connection.cursor() as cursor:
            cursor.execute("SET duckdb.force_execution = true;")

            export_sql = f"""
                COPY (
                    SELECT * FROM {partition_name}
                    ORDER BY service, level, id
                ) TO '{s3_path}' (FORMAT PARQUET, COMPRESSION ZSTD);
            """
            cursor.execute(export_sql)

        logger.info(f"Archived {partition_name} to {s3_path}")
        return s3_path

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
        True if archival succeeded, False if skipped (pg_duckdb not available)
    """
    if not is_pg_duckdb_available():
        logger.info("pg_duckdb not available, skipping archival workflow")
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

    Uses glob pattern to read all files for the org, then filters.
    pg_duckdb doesn't support array syntax in read_parquet().

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
    if not is_pg_duckdb_available():
        return []

    if config is None:
        config = ColdStorageConfig.from_settings()

    # Use glob pattern - pg_duckdb doesn't support array syntax
    glob_path = f"s3://{config.bucket}/{COLD_STORAGE_PREFIX}/{table_name}/org_{org_id}/*.parquet"

    # Build WHERE clause using r['column'] syntax required by pg_duckdb
    where_parts = [f"r['organization_id'] = {org_id}"]
    if filters:
        if "level" in filters:
            where_parts.append(f"r['level'] = {int(filters['level'])}")
        if "service" in filters:
            svc = filters["service"].replace("'", "''")
            where_parts.append(f"r['service'] = '{svc}'")
        if "body_search" in filters:
            # Escape single quotes in search term
            term = filters["body_search"].replace("'", "''")
            where_parts.append(f"r['body'] ILIKE '%{term}%'")

    where_clause = " AND ".join(where_parts)

    try:
        setup_duckdb_s3_credentials(config)

        with connection.cursor() as cursor:
            cursor.execute("SET duckdb.force_execution = true;")

            # pg_duckdb requires r['column'] syntax for read_parquet
            query = f"""
                SELECT r['id']::uuid AS id,
                       r['trace_id']::uuid AS trace_id,
                       r['organization_id']::bigint AS organization_id,
                       r['project_id']::bigint AS project_id,
                       r['span_id']::bigint AS span_id,
                       r['level']::smallint AS level,
                       r['severity_number']::smallint AS severity_number,
                       r['body']::text AS body,
                       r['service']::varchar AS service,
                       r['data']::json AS data
                FROM read_parquet('{glob_path}') r
                WHERE {where_clause}
                ORDER BY r['id'] DESC
                LIMIT {limit} OFFSET {offset};
            """
            cursor.execute(query)
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    except Exception as e:
        # Handle missing files gracefully
        if "No files found" in str(e) or "Could not open" in str(e):
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


def delete_cold_partition(
    view_name: str,
    config: ColdStorageConfig | None = None,
) -> None:
    """
    Delete a cold storage partition (legacy view + storage file).

    DEPRECATED: This is for the old mixed-org archive format.
    New per-org files are cleaned up via cleanup_cold_storage_for_org.
    """
    if config is None:
        config = ColdStorageConfig.from_settings()

    partition_name = view_name.replace("_archive", "")

    with connection.cursor() as cursor:
        cursor.execute(f"DROP VIEW IF EXISTS {view_name};")
    logger.info(f"Dropped archive view {view_name}")

    storage_path = get_partition_storage_path("logs_logevent", partition_name)

    storage = get_cold_storage_backend(config)
    if storage:
        try:
            if storage.exists(storage_path):
                storage.delete(storage_path)
                logger.info(f"Deleted cold storage file: {storage_path}")
        except Exception as e:
            logger.warning(f"Failed to delete {storage_path}: {e}")

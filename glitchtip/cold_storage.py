"""
Shared cold storage infrastructure for archiving partitions to Parquet via standalone DuckDB.

Auto-enables when a storage bucket is configured (GLITCHTIP_COLD_STORAGE_BUCKET
or AWS_STORAGE_BUCKET_NAME). Old partitions are archived to Parquet files
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

import json
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
        bucket = getattr(settings, "GLITCHTIP_COLD_STORAGE_BUCKET", None)
        if not bucket:
            bucket = getattr(settings, "AWS_STORAGE_BUCKET_NAME", None)

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

    # Configure S3 credentials (escape single quotes for safety)
    if config.access_key_id:
        val = config.access_key_id.replace("'", "''")
        conn.execute(f"SET s3_access_key_id = '{val}';")
    if config.secret_access_key:
        val = config.secret_access_key.replace("'", "''")
        conn.execute(f"SET s3_secret_access_key = '{val}';")

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

    Path structure: s3://bucket/cold_storage/{table}/org_{id}/{date}.parquet
    This isolates each org's data for efficient single-file queries.
    """
    return f"s3://{config.bucket}/{COLD_STORAGE_PREFIX}/{table_name}/org_{org_id}/{date_str}.parquet"


def get_org_cold_storage_path(table_name: str, org_id: int, date_str: str) -> str:
    """Get the storage-relative path for an org's daily Parquet file (without bucket)."""
    return f"{COLD_STORAGE_PREFIX}/{table_name}/org_{org_id}/{date_str}.parquet"


def archive_partition_per_org(
    partition_name: str,
    date_str: str,
    table_name: str,
    column_types: dict[str, str],
    select_sql: str,
    config: ColdStorageConfig | None = None,
) -> list[tuple[int, str]]:
    """
    Archive a partition to S3 as per-org Parquet files.

    Each organization's data is exported to a separate file:
    cold_storage/{table}/org_{id}/{date}.parquet

    Reads from PostgreSQL via Django's connection, writes Parquet via
    standalone DuckDB. No pg_duckdb extension required.

    Args:
        partition_name: Name of the partition to archive
        date_str: Date string for the partition (e.g., "20260115")
        table_name: Parent table name
        column_types: Dict mapping column names to DuckDB types
        select_sql: SQL template for selecting data, with {partition_name} and {org_id} placeholders
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

                # Read org's data from PostgreSQL
                cursor.execute(
                    select_sql.format(partition_name=partition_name),
                    [org_id],
                )
                columns = [desc[0] for desc in cursor.description]
                rows = cursor.fetchall()

                if not rows:
                    continue

                # Write to S3 via standalone DuckDB
                import json

                # Normalize values for DuckDB (UUIDs to strings, dicts to JSON, lists to JSON)
                clean_rows = []
                for row in rows:
                    clean_row = []
                    for val in row:
                        if isinstance(val, dict):
                            clean_row.append(json.dumps(val))
                        elif isinstance(val, list):
                            clean_row.append(json.dumps(val))
                        elif hasattr(val, "hex"):  # UUID
                            clean_row.append(str(val))
                        else:
                            clean_row.append(val)
                    clean_rows.append(clean_row)

                duck_conn = get_duckdb_connection(config)
                try:
                    col_defs = ", ".join(f"{c} {column_types[c]}" for c in columns)
                    duck_conn.execute(f"CREATE TABLE export_data({col_defs})")
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


def detach_partition(partition_name: str, parent_table: str) -> None:
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
    table_name: str,
    column_types: dict[str, str],
    select_sql: str,
    config: ColdStorageConfig | None = None,
) -> bool:
    """
    Full archival workflow: Export per-org files -> Detach -> Drop partition.

    Args:
        partition_name: Name of the partition to archive
        table_name: Parent table name
        column_types: Dict mapping column names to DuckDB types
        select_sql: SQL template for selecting data
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
        partition_name, date_str, table_name, column_types, select_sql, config
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
        partition_suffix: Optional suffix to match

    Returns list of (partition_name, partition_date) tuples.
    """
    from datetime import timedelta

    from django.utils import timezone

    cutoff_date = timezone.now() - timedelta(days=days)

    # Build regex pattern based on whether we're looking for views or tables
    if partition_suffix:
        pattern = f"^{table_name}_[0-9]{{8}}_h[0-9]+{partition_suffix}$"
        source_table = "pg_views"
        name_column = "viewname"
    else:
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
            parts = name.replace(partition_suffix, "").split("_")
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


def cleanup_cold_storage_for_org(
    org_id: int,
    retention_days: int,
    table_name: str,
    config: ColdStorageConfig | None = None,
) -> int:
    """
    Delete cold storage files older than retention period for an org.

    Computes paths directly from a 30-day window before the retention cutoff.
    No external state (cache/DB) needed - storage.delete() is a no-op on
    most backends if the file doesn't exist.

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
            if storage.exists(storage_path):
                storage.delete(storage_path)
                deleted_count += 1
                logger.debug(f"Deleted {storage_path}")
        except Exception:
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

    for org in Organization.objects.all().iterator():
        deleted = cleanup_cold_storage_for_org(
            org.id, retention_days, table_name, config
        )
        total_deleted += deleted

    logger.info(f"Cold storage cleanup complete: {total_deleted} files deleted")
    return total_deleted


def parse_json_field(val) -> dict:
    """Parse a value that may be dict, JSON string, or None into a dict."""
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def parse_json_list_field(val) -> list:
    """Parse a value that may be list, JSON string, or None into a list."""
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return []
    return []


def archive_and_cleanup_partitions(
    table_name: str,
    hot_days: int,
    column_types: dict[str, str],
    select_sql: str,
    retention_days: int | None = None,
    config: ColdStorageConfig | None = None,
) -> tuple[int, int, int]:
    """
    Archive old hot partitions to cold storage and clean up expired cold files.

    This is the standard maintenance pattern used by all partitioned tables
    with cold storage support. Each table provides its own column_types and
    select_sql; this function handles the archive loop and cold cleanup.

    Args:
        table_name: PG table name (e.g., "issue_events_issueevent")
        hot_days: Days to keep in hot storage before archiving
        column_types: DuckDB column type mapping for Parquet export
        select_sql: SQL template with {partition_name} placeholder
        retention_days: Days to keep cold files (default from settings)
        config: Cold storage config (default from settings)

    Returns:
        Tuple of (archived_count, failed_count, deleted_cold_count)
    """
    if not is_duckdb_available():
        return (0, 0, 0)

    if config is None:
        config = ColdStorageConfig.from_settings()

    if retention_days is None:
        retention_days = getattr(settings, "GLITCHTIP_COLD_STORAGE_RETENTION_DAYS", 90)

    # Archive hot -> cold
    archived = 0
    failed = 0
    partitions = get_partitions_older_than(table_name, hot_days)

    if partitions:
        logger.info(
            f"Archiving {len(partitions)} {table_name} partitions to cold storage"
        )
        for name, date in partitions:
            try:
                if archive_and_swap_partition(
                    name, table_name, column_types, select_sql, config
                ):
                    archived += 1
                    logger.info(f"Archived partition {name}")
                else:
                    failed += 1
                    logger.warning(f"Failed to archive partition {name}")
            except Exception as e:
                failed += 1
                logger.error(f"Error archiving partition {name}: {e}")

        logger.info(
            f"{table_name} archival complete: {archived} archived, {failed} failed"
        )

    # Delete expired cold storage files
    deleted = cleanup_all_cold_storage(
        retention_days=retention_days, table_name=table_name, config=config
    )
    if deleted:
        logger.info(f"{table_name} cold cleanup: {deleted} files deleted")

    return (archived, failed, deleted)

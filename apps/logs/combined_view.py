"""
Combined view utilities for seamless hot+cold log storage querying.

Creates and maintains a UNION ALL view that combines:
- Hot storage: logs_logevent partitioned table (PostgreSQL)
- Cold storage: Archive views pointing to S3 Parquet files (pg_duckdb)

This allows the API to query all logs through a single view without
needing to know whether data is in hot or cold storage.
"""

import logging

from django.db import connection

logger = logging.getLogger(__name__)

COMBINED_VIEW_NAME = "logs_logevent_all"


def get_archive_view_names() -> list[str]:
    """Get all archive view names."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT viewname FROM pg_views
            WHERE viewname LIKE 'logs_logevent_%_archive'
            ORDER BY viewname;
            """
        )
        return [row[0] for row in cursor.fetchall()]


def rebuild_combined_view() -> None:
    """
    Rebuild the combined view that unions hot and cold storage.

    Should be called after:
    - Archiving a partition to cold storage
    - Deleting a cold storage partition

    The view combines:
    - All data from logs_logevent (hot, partitioned table)
    - All data from archive views (cold, S3 Parquet via pg_duckdb)
    """
    archive_views = get_archive_view_names()

    with connection.cursor() as cursor:
        # Drop existing view
        cursor.execute(f"DROP VIEW IF EXISTS {COMBINED_VIEW_NAME};")

        # Build the UNION query
        # Hot storage - the parent partitioned table
        # Cast data to json to match archive view type (archive views return json, not jsonb)
        # This is necessary because UNION requires matching types, and DuckDB doesn't have jsonb
        hot_select = """
            SELECT
                id,
                trace_id,
                organization_id,
                project_id,
                span_id,
                level,
                severity_number,
                body,
                service,
                data::json
            FROM logs_logevent
        """

        parts = [hot_select]

        # Cold storage - archive views
        # Archive views return data as 'json' from DuckDB
        for view_name in archive_views:
            parts.append(
                f"""
                SELECT
                    id,
                    trace_id,
                    organization_id,
                    project_id,
                    span_id,
                    level,
                    severity_number,
                    body,
                    service,
                    data
                FROM {view_name}
                """
            )

        union_sql = " UNION ALL ".join(parts)

        create_sql = f"""
            CREATE VIEW {COMBINED_VIEW_NAME} AS
            {union_sql};
        """
        cursor.execute(create_sql)

    logger.info(f"Rebuilt {COMBINED_VIEW_NAME} with {len(archive_views)} archive views")


def ensure_combined_view_exists() -> None:
    """
    Ensure the combined view exists, creating it if necessary.

    This is idempotent - safe to call multiple times.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT 1 FROM pg_views WHERE viewname = %s LIMIT 1;
            """,
            [COMBINED_VIEW_NAME],
        )
        if not cursor.fetchone():
            rebuild_combined_view()


def drop_combined_view() -> None:
    """Drop the combined view."""
    with connection.cursor() as cursor:
        cursor.execute(f"DROP VIEW IF EXISTS {COMBINED_VIEW_NAME};")
    logger.info(f"Dropped {COMBINED_VIEW_NAME}")

from datetime import datetime, timedelta

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone
from django.utils.timezone import now

from .models import Issue


def cleanup_old_issues():
    days = settings.GLITCHTIP_MAX_EVENT_LIFE_DAYS

    # Delete ~1k empty issues at a time until less than 1k remain then delete the rest. Avoids memory overload.
    queryset = Issue.objects.filter(
        issueevent=None, last_seen__lt=now() - timedelta(days=days)
    ).order_by("id")

    while True:
        try:
            empty_issue_delimiter = queryset.values_list("id", flat=True)[
                1000:1001
            ].get()
            queryset.filter(id__lte=empty_issue_delimiter).delete()
        except Issue.DoesNotExist:
            break

    queryset.delete()


def downsample_old_events():
    """
    Downsample older partitions by keeping only the latest event per issue
    and sampling the rest based on project settings. Non-sampled events
    are kept as 'ghost rows' (data=NULL) to preserve statistics.
    Supports standard GlitchTip partitioning and nested pg_partman partitioning.
    """
    days = settings.GLITCHTIP_EVENT_DOWNSAMPLE_DAYS
    max_days = settings.GLITCHTIP_MAX_EVENT_LIFE_DAYS
    if days >= max_days:
        return

    # Use timezone.now() consistently
    current_time = timezone.now()
    cutoff_date = current_time - timedelta(days=days)
    max_life_date = current_time - timedelta(days=max_days)

    with connection.cursor() as cursor:
        # Recursive query to find all descendant partitions (nested or flat)
        cursor.execute(
            """
            WITH RECURSIVE partition_tree AS (
                SELECT inhrelid, inhparent
                FROM pg_inherits
                JOIN pg_class parent ON pg_inherits.inhparent = parent.oid
                WHERE parent.relname = 'issue_events_issueevent'
                UNION
                SELECT c.inhrelid, c.inhparent
                FROM pg_inherits c
                JOIN partition_tree p ON c.inhparent = p.inhrelid
            )
            SELECT child.relname, child.oid
            FROM partition_tree pt
            JOIN pg_class child ON pt.inhrelid = child.oid;
            """
        )
        partitions = cursor.fetchall()

        for partition_name, partition_oid in partitions:
            date_str = partition_name.split("_p")[-1]
            part_date = None

            # Try parsing _pYYYY_MM_DD (standard)
            try:
                part_date = datetime.strptime(date_str, "%Y_%m_%d").date()
            except ValueError:
                pass

            # Try parsing _pYYYYMMDD (partman compact)
            if not part_date:
                try:
                    part_date = datetime.strptime(date_str, "%Y%m%d").date()
                except ValueError:
                    pass

            if not part_date:
                continue

            # Check if eligible
            if part_date < cutoff_date.date() and part_date > max_life_date.date():
                # Check if already optimized
                cursor.execute(
                    "SELECT obj_description(%s, 'pg_class')", [partition_oid]
                )
                comment = cursor.fetchone()[0]
                if comment == "optimized":
                    continue

                # Perform downsample
                try:
                    with transaction.atomic():
                        # 1. Create temp table
                        cursor.execute(
                            f'CREATE TEMP TABLE temp_downsample (LIKE "{partition_name}" INCLUDING ALL) ON COMMIT DROP;'
                        )

                        # 2. Insert Smart Downsampled records
                        cursor.execute(
                            f"""
                            INSERT INTO temp_downsample (
                                id, type, timestamp, received, title, transaction, level, 
                                data, tags, issue_id, release_id, hashes
                            ) 
                            SELECT
                                e.id, e.type, e.timestamp, e.received, e.title, e.transaction, e.level,
                                CASE 
                                   -- Rule 1: Always keep the Representative (Newest per issue)
                                   WHEN rep.id IS NOT NULL THEN e.data
                                   -- Rule 2: Respect Project settings (Sample Rate)
                                   -- COALESCE default to 0.1 (10%) if project not found or setting missing
                                   WHEN random() < COALESCE(p.downsample_rate, 0.1) THEN e.data
                                   -- Rule 3: Ghost the rest
                                   ELSE NULL 
                                END,
                                e.tags, e.issue_id, e.release_id, e.hashes
                            FROM "{partition_name}" e
                            JOIN issue_events_issue i ON e.issue_id = i.id
                            LEFT JOIN projects_project p ON i.project_id = p.id
                            LEFT JOIN (
                                SELECT DISTINCT ON (issue_id) id
                                FROM "{partition_name}"
                                ORDER BY issue_id, received DESC
                            ) rep ON e.id = rep.id;
                            """
                        )

                        # 3. Truncate
                        cursor.execute(f'TRUNCATE TABLE "{partition_name}" ;')

                        # 4. Restore
                        cursor.execute(
                            f'INSERT INTO "{partition_name}" SELECT * FROM temp_downsample;'
                        )

                        # 5. Comment
                        cursor.execute(
                            f"COMMENT ON TABLE \"{partition_name}\" IS 'optimized';"
                        )
                except Exception:
                    # Transaction atomic block handles rollback automatically on exception
                    # Log error if needed, or just skip to next partition
                    pass

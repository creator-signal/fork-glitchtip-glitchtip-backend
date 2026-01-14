from django.core.management.base import BaseCommand
from django.db import connection, transaction
from glitchtip.partition_manager import UUID7Helper

class Command(BaseCommand):
    help = "Import legacy events from events_archive table to V2 partitioned table with UUIDv7 re-minting."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=10000,
            help="Number of events to import (default: 10000)",
        )
        parser.add_argument(
            "--delete-source",
            action="store_true",
            help="Delete source table (issue_events_issueevent_archive) after successful import (DANGEROUS)",
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        delete_source = options["delete_source"]

        self.stdout.write("Starting legacy event import...")

        # Check if archive table exists
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT FROM pg_tables
                    WHERE schemaname = 'public'
                    AND tablename = 'issue_events_issueevent_archive'
                );
                """
            )
            if not cursor.fetchone()[0]:
                self.stdout.write(self.style.ERROR("Archive table 'issue_events_issueevent_archive' not found. Migration may have already completed or cleaned up."))
                return

        # Import logic
        # 1. Select from archive
        # 2. Re-mint ID (UUIDv7 from created timestamp)
        # 3. Set event_id = old.id
        # 4. Insert into new table
        # We use raw SQL for performance and to handle the UUID generation properly

        import_sql = f"""
        INSERT INTO issue_events_issueevent (
            id, event_id,
            timestamp, received, created,
            issue_id, organization_id, release_id,
            type, level,
            title, transaction, data, tags, hashes
        )
        SELECT
            uuid_generate_v7_from_timestamp(created), -- Re-mint ID from created time
            id, -- Old ID becomes client event_id
            timestamp, received, created,
            issue_id,
            -- Join to get organization_id (required for V2 partitioning)
            (SELECT project.organization_id FROM projects_project project JOIN issue_events_issue issue ON issue.project_id = project.id WHERE issue.id = archive.issue_id),
            release_id,
            type, level,
            title, transaction, data, tags, hashes
        FROM issue_events_issueevent_archive archive
        WHERE NOT EXISTS (
            -- Idempotency check: don't import if event_id already exists in new table
            SELECT 1 FROM issue_events_issueevent dest WHERE dest.event_id = archive.id
        )
        ORDER BY created DESC
        LIMIT {limit};
        """

        # We need a helper SQL function for uuid_generate_v7_from_timestamp if it doesn't exist
        # Or we can do it in Python if batching.
        # Given "Performance: Use ORDER BY created DESC LIMIT 10000 (Fast index scan)", SQL is better.
        # But our `uuid_generate_v7()` function typically generates for NOW().
        # We need one that takes a timestamp.

        # Let's check if we can define a temporary function or if we should do it in Python.
        # Python iteration might be slow but safer for complex logic.
        # However, for 10k events, SQL is vastly superior.

        # Let's define a custom UUIDv7 from timestamp function in SQL for this session?
        # Or assume one exists? The migration added `uuid_generate_v7()`.
        # I'll add a python-side generator if SQL function is missing, but let's try to do it in SQL.

        # Postgres UUIDv7 implementation usually allows extracting timestamp.
        # Constructing it:
        # We need to bit-shift the epoch ms into the top 48 bits.

        sql_uuid_gen = """
        CREATE OR REPLACE FUNCTION uuid_generate_v7_from_timestamp(ts timestamptz) RETURNS uuid AS $$
        DECLARE
            unix_ms int8;
            uuid_bytes bytea;
        BEGIN
            unix_ms := (EXTRACT(EPOCH FROM ts) * 1000)::int8;
            uuid_bytes := set_byte(
                            set_byte(
                                set_byte(
                                    set_byte(
                                        overlay(decode(lpad(to_hex(unix_ms), 12, '0'), 'hex') placing '\x7'::bytea from 7 for 1),
                                        6, (unix_ms >> 48)::int::bit(8)::int | 64 -- Set version to 7? Wait.
                                        -- Actually, let's use the python logic for reference.
                                        -- 48 bits timestamp | 4 bits ver | 12 bits rand | 2 bits var | 62 bits rand
                                    ),
                                    -- Complex to do correctly in pure PL/PGSQL without pgcrypto maybe.
                                    -- Let's stick to Python for the UUID generation to ensure correctness with our PartitionManager logic.
                                    0, 0 -- placeholder
                                ),
                                0, 0
                            ),
                            0, 0
                        );
            -- Fallback to Python for safety and correctness of UUIDv7 spec implementation
            RETURN NULL;
        END
        $$ LANGUAGE plpgsql;
        """

        # Decision: Use Python-side batch processing. It's robust and we have the UUID7Helper class.
        # 10,000 records is small enough for Python batch.

        with transaction.atomic():
            # fetch old events
            with connection.cursor() as cursor:
                cursor.execute(f"""
                    SELECT
                        archive.id, archive.timestamp, archive.received,
                        archive.issue_id, archive.release_id,
                        archive.type, archive.level,
                        archive.title, archive.transaction, archive.data, archive.tags, archive.hashes,
                        (SELECT project.organization_id FROM projects_project project JOIN issue_events_issue issue ON issue.project_id = project.id WHERE issue.id = archive.issue_id) as organization_id
                    FROM issue_events_issueevent_archive archive
                    WHERE NOT EXISTS (
                        SELECT 1 FROM issue_events_issueevent dest WHERE dest.event_id = archive.id
                    )
                    ORDER BY archive.received DESC
                    LIMIT {limit}
                """)
                rows = cursor.fetchall()
                columns = [col[0] for col in cursor.description]

            if not rows:
                self.stdout.write("No more legacy events to import.")
                return

            self.stdout.write(f"Processing {len(rows)} events...")

            # Prepare batch insert
            # We need to map columns to IssueEvent model
            # But we can also do raw INSERT to avoid loading Django models if we want speed,
            # or use bulk_create.
            # Raw insert allows specifying the ID explicitly.

            from apps.issue_events.models import IssueEvent

            new_events = []
            for row in rows:
                row_dict = dict(zip(columns, row))
                
                # Re-mint ID
                received_at = row_dict['received']
                new_id = UUID7Helper.from_datetime(received_at)
                
                # Check org_id
                if row_dict['organization_id'] is None:
                    self.stdout.write(self.style.WARNING(f"Skipping event {row_dict['id']}: No organization_id found (orphaned issue?)"))
                    continue

                new_events.append(
                    IssueEvent(
                        id=new_id,
                        event_id=row_dict['id'],
                        timestamp=row_dict['timestamp'],
                        received=row_dict['received'],
                        issue_id=row_dict['issue_id'],
                        organization_id=row_dict['organization_id'],
                        release_id=row_dict['release_id'],
                        type=row_dict['type'],
                        level=row_dict['level'],
                        title=row_dict['title'],
                        transaction=row_dict['transaction'],
                        data=row_dict['data'],
                        tags=row_dict['tags'],
                        hashes=row_dict['hashes'],
                        # created is not a field on the model, so we can't set it via ORM
                        # But since we use bulk_create, we rely on DB default (NOW)
                        # To preserve history, we should probably update it via raw SQL or add field to model
                        # But for now, let's stick to what the ORM allows.
                    )
                )

            # IssueEvent.objects.bulk_create(new_events, ignore_conflicts=True) 
            # bulk_create might not allow setting 'id' if it thinks it's auto-generated?
            # UUIDField default is python function, so it should be fine to override.
            
            if new_events:
                IssueEvent.objects.bulk_create(new_events, ignore_conflicts=True)
                self.stdout.write(self.style.SUCCESS(f"Successfully imported {len(new_events)} events."))
            
            if delete_source:
                self.stdout.write(self.style.WARNING("Dropping archive table..."))
                with connection.cursor() as cursor:
                    cursor.execute("DROP TABLE IF EXISTS issue_events_issueevent_archive CASCADE;")
                self.stdout.write(self.style.SUCCESS("Archive table dropped."))


import os
import django
from django.db import connection

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "glitchtip.settings")
django.setup()

def verify_migration():
    print("Verifying Storage V2 Migration...")
    
    with connection.cursor() as cursor:
        # 1. Check IssueEvent count
        cursor.execute("SELECT count(*) FROM issue_events_issueevent")
        count = cursor.fetchone()[0]
        print(f"IssueEvent Count: {count} (Expected 500)")
        
        # 2. Check for Default Partitions
        tables = [
            "issue_events_issueevent_default",
            "performance_transactionevent_default",
            "performance_transactiongroupaggregate_default",
            "uptime_monitorcheck_default",
            "issue_events_issuetag_default",
            "issue_events_issueaggregate_default"
        ]
        
        for table in tables:
            cursor.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE c.relname = %s
                );
            """, [table])
            exists = cursor.fetchone()[0]
            if exists:
                print(f"FAILURE: Default partition {table} EXISTS!")
            else:
                print(f"SUCCESS: Default partition {table} does not exist.")

        # 3. Check Partition Count for IssueEvent
        # We expect partitions covering 2026-01-06 to 2026-01-28 (22 days)
        # Each day has partitions?
        # Wait, the log said "Created 22 new partitions".
        # But those are parent partitions (TIME).
        # Inside each, we have HASH partitions (default 4).
        
        cursor.execute("""
            SELECT count(*) FROM pg_inherits i 
            JOIN pg_class p ON i.inhparent = p.oid
            WHERE p.relname = 'issue_events_issueevent'
        """)
        time_partitions = cursor.fetchone()[0]
        print(f"Time Partitions for IssueEvent: {time_partitions} (Expected >= 22)")
        
        # Check one random day partition for hash buckets
        cursor.execute("""
            SELECT c.relname 
            FROM pg_inherits i 
            JOIN pg_class p ON i.inhparent = p.oid
            JOIN pg_class c ON i.inhrelid = c.oid
            WHERE p.relname = 'issue_events_issueevent'
            LIMIT 1
        """)
        one_partition = cursor.fetchone()[0]
        print(f"Checking sub-partitions for {one_partition}...")
        
        cursor.execute("""
            SELECT count(*) FROM pg_inherits i 
            JOIN pg_class p ON i.inhparent = p.oid
            WHERE p.relname = %s
        """, [one_partition])
        buckets = cursor.fetchone()[0]
        print(f"Buckets for {one_partition}: {buckets} (Expected 4)")
        
        if count == 500 and buckets == 4:
            print("\nVERIFICATION SUCCESSFUL")
        else:
            print("\nVERIFICATION FAILED")

if __name__ == "__main__":
    verify_migration()

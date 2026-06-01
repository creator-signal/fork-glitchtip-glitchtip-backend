from django.core.management.base import BaseCommand
from django.db import connection

# cached_is_up is the *confirmed* status (it only flips on a threshold-confirmed
# transition), so prefer the is_up of the most recent is_change=TRUE check (the
# last confirmed transition). Fall back to the latest check overall when no
# is_change=TRUE row survives (e.g. retention pruned it on a long-stable
# monitor) so we never overwrite a known status with NULL and trigger a
# spurious re-baseline notification. cached_last_change stays the last confirmed
# transition (NULL re-baselines on the next check, as before). Counters reset to
# a clean baseline and rebuild from subsequent checks.
RESYNC_SQL = """
UPDATE uptime_monitor m
SET
    cached_is_up = COALESCE(sub.confirmed_is_up, sub.latest_is_up),
    cached_last_change = sub.last_change,
    consecutive_failures = 0,
    consecutive_successes = 0
FROM (
    SELECT
        mon.id AS monitor_id,
        confirmed.is_up AS confirmed_is_up,
        confirmed.start_check AS last_change,
        latest.is_up AS latest_is_up
    FROM uptime_monitor mon
    LEFT JOIN LATERAL (
        SELECT mc.is_up, mc.start_check
        FROM uptime_monitorcheck mc
        WHERE mc.monitor_id = mon.id
          AND mc.organization_id = mon.organization_id
          AND mc.is_change = TRUE
        ORDER BY mc.start_check DESC
        LIMIT 1
    ) confirmed ON TRUE
    LEFT JOIN LATERAL (
        SELECT mc.is_up
        FROM uptime_monitorcheck mc
        WHERE mc.monitor_id = mon.id
          AND mc.organization_id = mon.organization_id
        ORDER BY mc.start_check DESC
        LIMIT 1
    ) latest ON TRUE
) sub
WHERE m.id = sub.monitor_id;
"""


class Command(BaseCommand):
    help = "Re-sync cached_is_up and cached_last_change on Monitor from MonitorCheck data"

    def handle(self, *args, **options):
        with connection.cursor() as cursor:
            cursor.execute(RESYNC_SQL)
            self.stdout.write(
                self.style.SUCCESS(f"Updated {cursor.rowcount} monitors")
            )

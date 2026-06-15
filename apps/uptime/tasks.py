import asyncio
import logging
import time
from collections import Counter
from uuid import UUID

import aiohttp
from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError
from django.db.models import F, Q
from django.tasks import task
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.alerts.constants import RecipientType
from apps.alerts.models import AlertRecipient
from apps.shared.async_db import execute_unnest

from .email import MonitorEmail
from .models import Monitor, MonitorCheck, MonitorType
from .utils import fetch_with_retries
from .webhooks import send_uptime_as_webhook

logger = logging.getLogger(__name__)

UPTIME_COUNTER_KEY = "uptime_counter"
UPTIME_TICK_EXPIRE = 2147483647


@task
async def dispatch_checks():
    """
    Dispatch monitor checks tasks in batches.
    """
    try:
        tick = await cache.aincr(UPTIME_COUNTER_KEY)
    except ValueError:
        await cache.aset(UPTIME_COUNTER_KEY, 0, UPTIME_TICK_EXPIRE)
        tick = await cache.aincr(UPTIME_COUNTER_KEY)

    # Reset tick if it gets too large, but keep it monotonic
    if tick >= UPTIME_TICK_EXPIRE:
        await cache.aset(UPTIME_COUNTER_KEY, 0, UPTIME_TICK_EXPIRE)

    # Dispatch checks for monitors that are scheduled to run at this tick
    # We use the monitor ID to spread the load across the interval window
    monitors = (
        Monitor.objects.filter(organization__event_throttle_rate__lt=100)
        .annotate(mod=(tick + F("id")) % F("interval"))
        .filter(mod=0)
        .exclude(Q(url="") & ~Q(monitor_type=MonitorType.HEARTBEAT))
        .only("id", "interval", "timeout")
    )

    monitor_ids = [mid async for mid in monitors.values_list("id", flat=True)]
    if monitor_ids:
        await perform_checks.aenqueue(monitor_ids)


async def update_uptime_statistics(org_counts: dict[int, int], check_time):
    """
    Bulk upsert UptimeCheckHourlyStatistic for a batch of monitor checks.
    Silently skips if no partition exists for the date (e.g. old test data).
    """
    hour = check_time.replace(minute=0, second=0, microsecond=0)
    data = sorted(
        ((org_id, hour, count) for org_id, count in org_counts.items()),
        key=lambda row: row[0],
    )
    if not data:
        return

    try:
        # A single INSERT ... ON CONFLICT is atomic by itself and needs no
        # surrounding transaction. Wrapping it in async_atomic() is unsafe when
        # USE_ASYNC_BACKEND is disabled (the default): async_atomic is then a
        # sync_to_async shim over Django's thread-local connection, which this
        # async worker shares across concurrently running tasks. Holding the
        # transaction open across the await below lets a sibling task close or
        # reset that shared connection mid-block, surfacing as "Cannot open a
        # new connection in an atomic block" / TransactionManagementError.
        await execute_unnest(
            "INSERT INTO uptime_uptimecheckhourlystatistic "
            "(organization_id, date, count) "
            "SELECT * FROM unnest(%s::int[], %s::timestamptz[], %s::int[]) "
            "ON CONFLICT (organization_id, date) "
            "DO UPDATE SET count = "
            "uptime_uptimecheckhourlystatistic.count + EXCLUDED.count",
            list(data),
        )
    except IntegrityError:
        logger.warning(
            "Failed to update uptime statistics for hour %s (missing partition)",
            hour,
        )


def apply_flap_tolerance(result):
    """
    Decide whether a check result confirms a status transition, applying the
    monitor's failure/recovery thresholds.

    Mutates ``result`` in place, setting:
      - ``new_is_up``: the (possibly unchanged) confirmed status to persist
      - ``transitioned``: True only on a confirmed Up<->Down flip (drives alerts)
      - ``is_change``: marks the MonitorCheck row (transition, or re-baseline
        when there is no prior change record)
      - ``consecutive_failures`` / ``consecutive_successes``: updated counters

    With both thresholds at their default of 1 this reduces exactly to the
    historical single-check behaviour. Heartbeat monitors are exempt (forced to
    1): their "up" results never reach this path and recovery is recorded by the
    push endpoint, so thresholding them would break recovery detection.
    """
    prev_is_up = result["latest_is_up"]
    check_is_up = result["is_up"]

    if result.get("monitor_type") == MonitorType.HEARTBEAT:
        failure_threshold = recovery_threshold = 1
    else:
        failure_threshold = result.get("failure_threshold", 1)
        recovery_threshold = result.get("recovery_threshold", 1)

    failures = result.get("consecutive_failures", 0)
    successes = result.get("consecutive_successes", 0)
    if check_is_up:
        successes += 1
        failures = 0
    else:
        failures += 1
        successes = 0

    if prev_is_up is None:
        # First check (or post-pruning baseline): adopt the result immediately.
        new_is_up = check_is_up
        transitioned = True
    elif check_is_up and not prev_is_up and successes >= recovery_threshold:
        new_is_up = True
        transitioned = True
    elif not check_is_up and prev_is_up and failures >= failure_threshold:
        new_is_up = False
        transitioned = True
    else:
        new_is_up = prev_is_up
        transitioned = False

    result["new_is_up"] = new_is_up
    result["transitioned"] = transitioned
    result["is_change"] = transitioned or result["last_change"] is None
    result["consecutive_failures"] = failures
    result["consecutive_successes"] = successes


async def save_monitor_checks(results, now):
    """
    Bulk save monitor checks and trigger notifications.
    """
    for result in results:
        apply_flap_tolerance(result)

    monitor_checks = await MonitorCheck.objects.abulk_create(
        [
            MonitorCheck(
                monitor_id=result["id"],
                organization_id=result["organization_id"],
                is_up=result["is_up"],
                is_change=result["is_change"],
                start_check=now,
                reason=result.get("reason", None),
                response_time=result.get("response_time", None),
                data=result.get("data", None),
            )
            for result in results
        ]
    )

    # Bulk update cached fields and flap counters on Monitor
    monitors_to_update = [
        Monitor(
            pk=result["id"],
            cached_is_up=result["new_is_up"],
            cached_last_change=now if result["is_change"] else result["last_change"],
            consecutive_failures=result["consecutive_failures"],
            consecutive_successes=result["consecutive_successes"],
        )
        for result in results
    ]
    if monitors_to_update:
        await Monitor.objects.abulk_update(
            monitors_to_update,
            [
                "cached_is_up",
                "cached_last_change",
                "consecutive_failures",
                "consecutive_successes",
            ],
        )

    # Update hourly statistics
    org_counts = Counter(r["organization_id"] for r in results)
    await update_uptime_statistics(org_counts, now)

    for i, result in enumerate(results):
        if result["transitioned"]:
            last_change = result["last_change"]
            if last_change:
                last_change = last_change.isoformat()
            # Pass monitor_id and composite PK as a list of strings for JSON serializability
            monitor_check_pk = [str(monitor_checks[i].id), result["organization_id"]]
            await send_monitor_notification.aenqueue(
                result["id"], monitor_check_pk, not result["new_is_up"], last_change
            )


async def run_checks(monitors, now):
    async with aiohttp.ClientSession(**settings.AIOHTTP_CONFIG) as session:
        tasks = [asyncio.create_task(fetch_with_retries(session, m)) for m in monitors]
        pending = tasks
        buffer = []
        BATCH_SIZE = 100
        FLUSH_INTERVAL = 1.0
        last_flush = time.monotonic()

        while pending:
            # Wait for tasks to finish, but flush buffer if needed
            time_since_flush = time.monotonic() - last_flush
            timeout = max(0.1, FLUSH_INTERVAL - time_since_flush)

            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED, timeout=timeout
            )

            for task in done:
                try:
                    result = task.result()
                    # Filter out "up" heartbeats
                    if (
                        result["monitor_type"] != MonitorType.HEARTBEAT
                        or result["is_up"] is False
                    ):
                        buffer.append(result)
                except Exception as e:
                    logger.error("Critical monitor check failure", exc_info=e)

            # Flush if batch full or timeout reached
            if len(buffer) >= BATCH_SIZE or (
                buffer and time.monotonic() - last_flush >= FLUSH_INTERVAL
            ):
                await save_monitor_checks(buffer, now)
                buffer = []
                last_flush = time.monotonic()

        # Final flush
        if buffer:
            await save_monitor_checks(buffer, now)


@task
async def perform_checks(monitor_ids: list[int], now: str | None = None):
    """
    Performant check monitors and save results
    """
    if now is None:
        now = timezone.now()
    else:
        now = parse_datetime(now)

    # Fetch monitors asynchronously
    # Django's values() returns a QuerySet, which is async iterable in Django 4.1+
    monitors = [
        m
        async for m in Monitor.objects.with_check_annotations()
        .filter(pk__in=monitor_ids)
        .values()
    ]

    # Run async checks with smart batching
    await run_checks(monitors, now)


@task
async def send_monitor_notification(
    monitor_id: int, monitor_check_pk: list, went_down: bool, last_change: str | None
):
    if last_change:
        last_change = parse_datetime(last_change)

    # Convert list back to tuple for Django lookup
    monitor_check_id = (UUID(monitor_check_pk[0]), monitor_check_pk[1])

    recipients = AlertRecipient.objects.filter(
        alert__project__monitor__id=monitor_id, alert__uptime=True
    )
    async for recipient in recipients:
        if recipient.recipient_type == RecipientType.EMAIL:
            await sync_to_async(
                MonitorEmail(
                    pk=monitor_check_id,
                    went_down=went_down,
                    last_change=last_change if last_change else None,
                ).send_users_email
            )()
        else:
            await send_uptime_as_webhook(
                recipient, monitor_check_id, went_down, last_change
            )

import asyncio
import logging
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db.models import F, Q
from django.tasks import task
from django.utils import timezone

from apps.alerts.constants import RecipientType
from apps.alerts.models import AlertRecipient

from .email import MonitorEmail
from .models import Monitor, MonitorCheck, MonitorType
from .utils import fetch_all
from .webhooks import send_uptime_as_webhook

logger = logging.getLogger(__name__)

UPTIME_COUNTER_KEY = "uptime_counter"
UPTIME_TICK_EXPIRE = 2147483647


@task
def dispatch_checks():
    """
    Dispatch monitor checks tasks in batches.
    """
    try:
        tick = cache.incr(UPTIME_COUNTER_KEY)
    except ValueError:
        cache.set(UPTIME_COUNTER_KEY, 0, UPTIME_TICK_EXPIRE)
        tick = cache.incr(UPTIME_COUNTER_KEY)

    # Reset tick if it gets too large, but keep it monotonic
    if tick >= UPTIME_TICK_EXPIRE:
        cache.set(UPTIME_COUNTER_KEY, 0, UPTIME_TICK_EXPIRE)

    # Dispatch checks for monitors that are scheduled to run at this tick
    # We use the monitor ID to spread the load across the interval window
    monitors = (
        Monitor.objects.filter(organization__event_throttle_rate__lt=100)
        .annotate(mod=(tick + F("id")) % F("interval"))
        .filter(mod=0)
        .exclude(Q(url="") & ~Q(monitor_type=MonitorType.HEARTBEAT))
        .only("id", "interval", "timeout")
    )
    
    # Batch them up to reduce queue pressure? 
    # vtasks handles lists efficiently, but let's pass all IDs at once for now
    # or chunk them if we expect thousands.
    # perform_checks will fetch them all.
    fast_ids = []
    slow_ids = []
    for monitor in monitors:
        if (monitor.timeout or 20) < 30:
            fast_ids.append(monitor.id)
        else:
            slow_ids.append(monitor.id)

    if fast_ids:
        perform_checks.enqueue(fast_ids)
    if slow_ids:
        perform_checks.enqueue(slow_ids)


@task
def perform_checks(monitor_ids: list[int], now: str | None = None):
    """
    Performant check monitors and save results

    1. Fetch all monitor data for ids
    2. Async perform all checks
    3. Save in bulk results
    """
    if now is None:
        now = timezone.now()
    else:
        from django.utils.dateparse import parse_datetime

        now = parse_datetime(now)
    # Convert queryset to raw list[dict] for asyncio operations
    monitors = list(
        Monitor.objects.with_check_annotations().filter(pk__in=monitor_ids).values()
    )
    results = []
    for result in asyncio.run(fetch_all(monitors)):
        # Log and ignore exceptions
        if isinstance(result, Exception):
            logger.error("Critical monitor check failure", exc_info=result)
        # Filter out "up" heartbeats
        elif (
            result["monitor_type"] != MonitorType.HEARTBEAT or result["is_up"] is False
        ):
            results.append(result)

    monitor_checks = MonitorCheck.objects.bulk_create(
        [
            MonitorCheck(
                monitor_id=result["id"],
                is_up=result["is_up"],
                is_change=result["latest_is_up"] != result["is_up"],
                start_check=now,
                reason=result.get("reason", None),
                response_time=result.get("response_time", None),
                data=result.get("data", None),
            )
            for result in results
        ]
    )
    for i, result in enumerate(results):
        last_change = result["last_change"]
        if last_change:
            last_change = last_change.isoformat()
        if result["latest_is_up"] is True and result["is_up"] is False:
            send_monitor_notification.enqueue(monitor_checks[i].pk, True, last_change)
        elif result["latest_is_up"] is False and result["is_up"] is True:
            send_monitor_notification.enqueue(monitor_checks[i].pk, False, last_change)


@task
def send_monitor_notification(
    monitor_check_id: int, went_down: bool, last_change: str | None
):
    if last_change:
        from django.utils.dateparse import parse_datetime

        last_change = parse_datetime(last_change)
    recipients = AlertRecipient.objects.filter(
        alert__project__monitor__checks=monitor_check_id, alert__uptime=True
    )
    for recipient in recipients:
        if recipient.recipient_type == RecipientType.EMAIL:
            MonitorEmail(
                pk=monitor_check_id,
                went_down=went_down,
                last_change=last_change if last_change else None,
            ).send_users_email()
        elif recipient.is_webhook:
            send_uptime_as_webhook(recipient, monitor_check_id, went_down, last_change)

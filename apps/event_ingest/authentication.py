import logging
import math
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.cache import cache
from django.db import connection, connections
from django.db.utils import OperationalError
from django.http import HttpRequest
from ninja.errors import AuthenticationError, HttpError, ValidationError

from apps.organizations_ext.tasks import check_organization_throttle
from glitchtip.api.exceptions import ThrottleException
from sentry.utils.auth import parse_auth_header

from .constants import EVENT_BLOCK_CACHE_KEY

logger = logging.getLogger(__name__)


@dataclass
class OrganizationInfo:
    id: int
    is_accepting_events: bool
    event_throttle_rate: int
    scrub_ip_addresses: bool
    log_throttle_rate: int = 0


@dataclass
class ProjectAuthInfo:
    id: int
    scrub_ip_addresses: bool
    event_throttle_rate: int
    organization_id: int
    first_event: datetime | None
    organization: OrganizationInfo
    log_throttle_rate: int = 0

    @property
    def should_scrub_ip_addresses(self):
        """Organization overrides project setting"""
        return self.scrub_ip_addresses or self.organization.scrub_ip_addresses


class EventAuthHttpRequest(HttpRequest):
    """Django HttpRequest that is known to be authenticated by a project DSN"""

    auth: ProjectAuthInfo


def auth_from_request(request: HttpRequest):
    """
    Get DSN (sentry_key) from request header
    Accept both sentry or glitchtip prefix
    Do not read request body when possible. This may result in uncompression which is slow.
    """
    for k in request.GET.keys():
        if k in ["sentry_key", "glitchtip_key"]:
            return request.GET[k]

    if auth_header := request.META.get(
        "HTTP_X_SENTRY_AUTH", request.META.get("HTTP_AUTHORIZATION")
    ):
        result = parse_auth_header(auth_header)
        return result.get("sentry_key", result.get("glitchtip_key"))

    raise AuthenticationError("Unable to find authentication information")


# One letter codes to save cache memory and map to various event rejection type exceptions
REJECTION_MAP: dict[Literal["v", "t"], Exception] = {
    "v": AuthenticationError(message="Invalid DSN"),
    "t": ThrottleException(),
}
REJECTION_WAIT = 30


@dataclass
class ThrottleRates:
    """Throttle rates for events and logs at org and project levels."""

    org_event: int = 0
    proj_event: int = 0
    org_log: int = 0
    proj_log: int = 0

    @property
    def max_event_throttle(self) -> int:
        return max(self.org_event, self.proj_event)

    @property
    def max_log_throttle(self) -> int:
        return max(self.org_log, self.proj_log)

    def is_accepting_events(self) -> bool:
        """Probabilistic check if events should be accepted."""
        return _is_accepting(self.org_event) and _is_accepting(self.proj_event)

    def is_accepting_logs(self) -> bool:
        """Probabilistic check if logs should be accepted."""
        return _is_accepting(self.org_log) and _is_accepting(self.proj_log)


def _is_accepting(throttle_rate: int) -> bool:
    """Probabilistic acceptance based on throttle rate."""
    if throttle_rate == 0:
        return True
    return random.randint(0, 100) > throttle_rate


def serialize_throttle(
    org_event: int, proj_event: int, org_log: int = 0, proj_log: int = 0
) -> str:
    """
    Format: "t:org_event:proj_event:org_log:proj_log"
    Example: "t:30:0:50:0" means 30% org event throttle, 50% org log throttle
    """
    return f"t:{org_event}:{proj_event}:{org_log}:{proj_log}"


def deserialize_throttle(input: str) -> ThrottleRates | None:
    """Parse cached throttle string into ThrottleRates."""
    if input == "t":
        return ThrottleRates()
    if input.startswith("t:"):
        parts = input.split(":")
        if len(parts) >= 3:
            # Support both old format (3 parts) and new format (5 parts)
            org_event = int(parts[1])
            proj_event = int(parts[2])
            org_log = int(parts[3]) if len(parts) > 3 else 0
            proj_log = int(parts[4]) if len(parts) > 4 else 0
            return ThrottleRates(org_event, proj_event, org_log, proj_log)
    return None


# Keep for backwards compatibility
def is_accepting_events(throttle_rate: int) -> bool:
    """Consider throttle to determine if events are being accepted"""
    return _is_accepting(throttle_rate)


def calculate_retry_after(throttle: int):
    """Calculates Retry-After using a power function."""
    return math.ceil(0.02 * throttle**2.3)


def get_project_auth_info_row(project_id: int, sentry_key: UUID):
    # May someday be async https://code.djangoproject.com/ticket/35629
    if "read_only" in settings.DATABASES:
        try:
            with connections["read_only"].cursor() as cursor:
                cursor.callproc(
                    "get_project_auth_info",
                    [
                        project_id,
                        sentry_key,
                    ],
                )
                return cursor.fetchone()
        except OperationalError:
            pass
        except Exception as e:
            # Fail safe - don't let a read only db failure stop the request
            logger.warning("Failed to read from read_only database", exc_info=e)

    with connection.cursor() as cursor:
        cursor.callproc(
            "get_project_auth_info",
            [
                project_id,
                sentry_key,
            ],
        )
        return cursor.fetchone()


async def get_project(request: HttpRequest) -> ProjectAuthInfo | None:
    """
    Return the valid and accepting events project based on a request.

    Throttle unwanted requests using cache to mitigate repeat attempts
    """
    if not request.resolver_match:
        raise ValidationError([{"message": "Invalid project ID"}])
    project_id: int = request.resolver_match.captured_kwargs.get("project_id")
    try:
        sentry_key = UUID(auth_from_request(request))
    except ValueError as err:
        raise ValidationError(
            [{"message": "dsn key badly formed hexadecimal UUID string"}]
        ) from err

    # block cache check should be right before database call
    block_cache_key = EVENT_BLOCK_CACHE_KEY + str(project_id)
    if block_value := await cache.aget(block_cache_key):
        if block_value.startswith("t"):
            if throttle := deserialize_throttle(block_value):
                # If both events AND logs are 100% throttled, reject immediately
                if (
                    throttle.max_event_throttle == 100
                    and throttle.max_log_throttle == 100
                ):
                    raise ThrottleException(600)
                # If only events are throttled but not 100%, do probabilistic check
                if not throttle.is_accepting_events():
                    raise ThrottleException(
                        calculate_retry_after(throttle.max_event_throttle)
                    )
                # If events pass but logs are throttled, continue - handle per-item in envelope
        else:
            # Repeat the original message until cache expires
            raise REJECTION_MAP[block_value]

    row = await sync_to_async(get_project_auth_info_row)(project_id, sentry_key)

    if not row:
        await cache.aset(block_cache_key, "v", REJECTION_WAIT)
        raise REJECTION_MAP["v"]

    project = ProjectAuthInfo(
        id=row[0],
        scrub_ip_addresses=row[1],
        event_throttle_rate=row[2],
        organization_id=row[3],
        organization=OrganizationInfo(
            id=row[3],
            is_accepting_events=row[4],
            event_throttle_rate=row[5],
            scrub_ip_addresses=row[6],
            log_throttle_rate=row[9] if len(row) > 9 else 0,
        ),
        first_event=row[7],
        log_throttle_rate=row[8] if len(row) > 8 else 0,
    )

    # Build throttle rates
    throttle = ThrottleRates(
        org_event=project.organization.event_throttle_rate,
        proj_event=project.event_throttle_rate,
        org_log=project.organization.log_throttle_rate,
        proj_log=project.log_throttle_rate,
    )

    # If not accepting events at all, or both event and log throttles are 100%, reject immediately
    if not project.organization.is_accepting_events or (
        throttle.max_event_throttle == 100 and throttle.max_log_throttle == 100
    ):
        await cache.aset(block_cache_key, "t", REJECTION_WAIT)
        raise ThrottleException(600)

    # Cache throttle rates for both events and logs
    if (
        throttle.org_event
        or throttle.proj_event
        or throttle.org_log
        or throttle.proj_log
    ):
        await cache.aset(
            block_cache_key,
            serialize_throttle(
                throttle.org_event,
                throttle.proj_event,
                throttle.org_log,
                throttle.proj_log,
            ),
            REJECTION_WAIT,
        )

    # Check event throttling
    # 100% throttle uses fixed 600 second retry, partial throttle uses calculated retry
    if throttle.max_event_throttle == 100:
        raise ThrottleException(600)
    elif not throttle.is_accepting_events():
        raise ThrottleException(calculate_retry_after(throttle.max_event_throttle))

    # Check throttle needs every 1 out of X requests
    if (
        settings.BILLING_ENABLED
        and random.random() < 1 / settings.GLITCHTIP_THROTTLE_CHECK_INTERVAL
    ):
        await check_organization_throttle.aenqueue(project.organization_id)
    return project


async def event_auth(request: HttpRequest) -> ProjectAuthInfo | None:
    """
    Event Ingest authentication means validating the DSN (sentry_key).
    Throttling is also handled here.
    It does not include user authentication.
    """
    if settings.MAINTENANCE_EVENT_FREEZE:
        raise HttpError(
            503, "Events are not currently being accepted due to maintenance."
        )
    return await get_project(request)

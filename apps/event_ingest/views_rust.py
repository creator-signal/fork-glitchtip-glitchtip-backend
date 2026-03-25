"""
Rust-accelerated envelope ingest view.

Uses the glitchtip_ingest Rust extension for envelope parsing and validation,
bypassing Pydantic model instantiation. The parsed dicts are used directly
for cache/auth/queue operations.

Toggle via GLITCHTIP_RUST_INGEST=True environment variable.
"""

import asyncio
import logging
import uuid
from dataclasses import asdict

import glitchtip_ingest
from django.core.cache import cache
from django.core.exceptions import RequestDataTooBig
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from ninja.errors import AuthenticationError
from ninja.errors import ValidationError as NinjaValidationError
from sentry_sdk import capture_exception, set_level

from apps.event_ingest.interfaces import IngestTaskMessage, LogIngestTaskMessage
from apps.issue_events.constants import IssueEventType
from apps.logs.tasks import ingest_logs
from glitchtip.api.exceptions import ThrottleException
from glitchtip.partition_manager import UUID7Helper

from .api import get_ip_address
from .authentication import EventAuthHttpRequest, event_auth
from .tasks import ingest_event, ingest_transaction, ingest_user_report
from .utils import serialize_for_vtasks

logger = logging.getLogger(__name__)

ISSUE_TYPE_MAP = {
    "error": IssueEventType.ERROR,
    "default": IssueEventType.DEFAULT,
}


@csrf_exempt
async def event_envelope_view_rust(request: EventAuthHttpRequest, project_id: int):
    if request.method != "POST":
        return JsonResponse({"detail": "Method not allowed"}, status=405)

    try:
        project = await event_auth(request)
    except ThrottleException as e:
        response = HttpResponse("Too Many Requests", status=429)
        response["Retry-After"] = str(e.retry_after)
        return response
    except AuthenticationError:
        return JsonResponse({"detail": "Denied"}, status=403)
    except NinjaValidationError:
        return JsonResponse({"detail": "Invalid DSN"}, status=403)

    if project is None:
        return JsonResponse({"detail": "Denied"}, status=403)
    request.auth = project
    update_first_event = project.first_event is None
    client_ip = get_ip_address(request)

    try:
        from asgiref.sync import sync_to_async

        body = await sync_to_async(lambda: request.body)()
    except RequestDataTooBig as e:
        return HttpResponseForbidden(f"{e}", status=413)

    if not body:
        return JsonResponse({"detail": "Empty request body"}, status=400)

    # Parse envelope in Rust (releases GIL, runs in thread pool)
    try:
        result = await asyncio.to_thread(
            glitchtip_ingest.process_envelope,
            body,
            "",  # Body already decompressed by middleware
        )
    except ValueError as e:
        logger.warning(f"Rust envelope parse error on {request.path}: {e}")
        return JsonResponse({"detail": "Invalid envelope"}, status=400)

    envelope_header = result["header"]
    envelope_event_id = envelope_header.get("event_id")

    for item in result["items"]:
        item_type = item["type"]
        payload = item["payload"]

        try:
            if item_type == "event":
                issue_type = ISSUE_TYPE_MAP.get(
                    item.get("issue_type", "default"), IssueEventType.DEFAULT
                )

                # Set client IP on user if present
                if user := payload.get("user"):
                    if isinstance(user, dict):
                        user["ip_address"] = client_ip

                # Resolve event_id
                event_id = payload.get("event_id") or envelope_event_id
                if event_id is None:
                    event_id = uuid.uuid4().hex
                elif isinstance(event_id, str) and "-" in event_id:
                    event_id = event_id.replace("-", "")

                primary_id = UUID7Helper.from_datetime()
                interchange_event = IngestTaskMessage(
                    project_id=project_id,
                    organization_id=project.organization_id,
                    payload=payload | {"type": issue_type},
                    received=timezone.now(),
                    update_first_event=update_first_event,
                    uuid=primary_id.hex,
                )
                if await cache.aadd("uuid" + event_id, True):
                    await ingest_event.aenqueue(
                        serialize_for_vtasks(asdict(interchange_event))
                    )

            elif item_type == "transaction":
                event_id = payload.get("event_id")
                if event_id is None:
                    event_id = uuid.uuid4().hex
                elif isinstance(event_id, str) and "-" in event_id:
                    event_id = event_id.replace("-", "")

                primary_id = UUID7Helper.from_datetime()
                interchange_event = IngestTaskMessage(
                    project_id=project_id,
                    organization_id=project.organization_id,
                    payload=payload,
                    received=timezone.now(),
                    update_first_event=update_first_event,
                    uuid=primary_id.hex,
                )
                if await cache.aadd("uuid" + event_id, True):
                    await ingest_transaction.aenqueue(
                        serialize_for_vtasks(asdict(interchange_event))
                    )

            elif item_type in ("user_report", "feedback"):
                from .schema import UserReportTaskMessage

                if item_type == "feedback":
                    # Extract feedback context
                    contexts = payload.get("contexts", {})
                    fb = contexts.get("feedback", {})
                    report_data = {
                        "event_id": fb.get("associated_event_id"),
                        "name": (fb.get("name") or "")[:128],
                        "email": (fb.get("contact_email") or "")[:254],
                        "comments": fb.get("message") or "",
                    }
                else:
                    report_data = {
                        "event_id": str(payload.get("event_id") or ""),
                        "name": (payload.get("name") or "")[:128],
                        "email": (payload.get("email") or "")[:254],
                        "comments": payload.get("comments") or "",
                    }

                msg = UserReportTaskMessage(
                    project_id=project_id,
                    organization_id=project.organization_id,
                    **report_data,
                )
                await ingest_user_report.aenqueue(
                    serialize_for_vtasks(msg.model_dump())
                )

            elif item_type == "log":
                from django.conf import settings

                if not settings.GLITCHTIP_ENABLE_LOGS:
                    continue

                log_items = payload.get("items", [])
                log_message = LogIngestTaskMessage(
                    project_id=project_id,
                    organization_id=project.organization_id,
                    received=timezone.now(),
                    logs=log_items,
                )
                await ingest_logs.aenqueue(serialize_for_vtasks(asdict(log_message)))

        except Exception as e:
            set_level("error")
            capture_exception(e)
            logger.error(
                f"Error processing {item_type} on {request.path}",
                exc_info=e,
            )
            continue

    if envelope_event_id:
        eid = (
            envelope_event_id.replace("-", "")
            if "-" in envelope_event_id
            else envelope_event_id
        )
        return JsonResponse({"id": eid})
    return JsonResponse({})

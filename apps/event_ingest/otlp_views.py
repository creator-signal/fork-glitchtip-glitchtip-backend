"""Native OTLP/HTTP ingest endpoints.

These accept telemetry from any OpenTelemetry exporter (language SDKs, the
Collector, auto-instrumentation) with no sentry SDK involved. They live at the
OTLP-standard paths (``/v1/logs``; traces to follow) and resolve the project
from the DSN public key carried in an auth header.
"""

import logging
from dataclasses import asdict

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseForbidden,
    JsonResponse,
)
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from ninja.errors import AuthenticationError, HttpError

from apps.event_ingest.interfaces import LogIngestTaskMessage
from apps.logs.tasks import ingest_logs
from glitchtip.api.exceptions import ThrottleException

from .authentication import get_project_by_key
from .otlp import decode_otlp_logs
from .utils import serialize_for_vtasks

logger = logging.getLogger(__name__)


def _otlp_ok(content_type: str) -> HttpResponse:
    """Empty success response. An empty body with 200 is a valid
    ``Export<Signal>ServiceResponse`` (no partial_success) and is what OTel
    exporters expect on success."""
    if "json" in content_type:
        return HttpResponse(b"{}", content_type="application/json")
    return HttpResponse(b"", content_type="application/x-protobuf")


@csrf_exempt
async def otlp_logs_view(request: HttpRequest) -> HttpResponse:
    """OTLP/HTTP logs ingest — ``POST /v1/logs``."""
    if request.method != "POST":
        return JsonResponse({"detail": "Method not allowed"}, status=405)
    if not settings.GLITCHTIP_ENABLE_LOGS:
        return JsonResponse({"detail": "Logs are not enabled"}, status=404)

    try:
        project = await get_project_by_key(request)
    except ThrottleException as e:
        response = HttpResponse("Too Many Requests", status=429)
        response["Retry-After"] = str(e.retry_after)
        return response
    except AuthenticationError:
        return JsonResponse({"detail": "Invalid DSN"}, status=401)
    except HttpError as e:
        return JsonResponse({"detail": str(e)}, status=e.status_code)

    try:
        body = await sync_to_async(lambda: request.body)()
    except RequestDataTooBig as e:
        return HttpResponseForbidden(f"{e}", status=413)

    content_type = request.content_type or ""
    try:
        log_items = decode_otlp_logs(body, content_type)
    except Exception as e:
        logger.warning("OTLP logs decode error on %s", request.path, exc_info=e)
        return JsonResponse({"detail": "Malformed OTLP logs payload"}, status=400)

    if log_items:
        message = LogIngestTaskMessage(
            project_id=project.id,
            organization_id=project.organization_id,
            received=timezone.now(),
            logs=log_items,
        )
        await ingest_logs.aenqueue(serialize_for_vtasks(asdict(message)))

    return _otlp_ok(content_type)

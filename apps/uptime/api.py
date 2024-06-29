from uuid import UUID

from django.http import HttpRequest, HttpResponse
from django.shortcuts import aget_object_or_404
from ninja import Router

from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.pagination import paginate
from glitchtip.utils import async_call_celery_task

from .models import Monitor, MonitorCheck
from .schema import MonitorCheckSchema, MonitorSchema
from .tasks import send_monitor_notification

router = Router()


def get_monitor_queryset(user_id: int, organization_slug: str):
    return Monitor.objects.with_check_annotations().filter(
        organization__slug=organization_slug
    )


@router.post(
    "organizations/{slug:organization_slug}/heartbeat_check/{uuid:endpoint_id}/",
    response=MonitorCheckSchema,
    auth=None,
)
async def heartbeat_check(
    request: HttpRequest, organization_slug: str, endpoint_id: UUID
):
    monitor = await aget_object_or_404(
        Monitor.objects.with_check_annotations(),
        organization__slug=organization_slug,
        endpoint_id=endpoint_id,
    )
    monitor_check = await MonitorCheck.objects.acreate(monitor=monitor)
    if monitor.latest_is_up is False:
        await async_call_celery_task(
            send_monitor_notification, monitor_check.pk, False, monitor.last_change
        )

    return monitor_check


@router.get(
    "organizations/{slug:organization_slug}/monitors/",
    response=list[MonitorSchema],
    by_alias=True,
)
@paginate
async def list_monitors(
    request: AuthHttpRequest, response: HttpResponse, organization_slug: str
):
    return get_monitor_queryset(request.auth.user_id, organization_slug)

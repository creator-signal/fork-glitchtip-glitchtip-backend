from datetime import timedelta
from uuid import UUID

from django.db.models import F, Prefetch, Window
from django.db.models.functions import RowNumber
from django.http import HttpRequest, HttpResponse
from django.shortcuts import aget_object_or_404
from django.utils import timezone
from ninja import Router

from apps.organizations_ext.models import Organization
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.pagination import paginate
from glitchtip.utils import async_call_celery_task

from .models import Monitor, MonitorCheck
from .schema import MonitorCheckSchema, MonitorDetailSchema, MonitorIn, MonitorSchema
from .tasks import send_monitor_notification

router = Router()


def get_monitor_queryset(user_id: int, organization_slug: str):
    return (
        Monitor.objects.with_check_annotations()
        .filter(organization__users=user_id, organization__slug=organization_slug)
        # Fetch latest 60 checks for each monitor
        .prefetch_related(
            Prefetch(
                "checks",
                queryset=MonitorCheck.objects.filter(  # Optimization
                    start_check__gt=timezone.now() - timedelta(hours=12)
                )
                .annotate(
                    row_number=Window(
                        expression=RowNumber(),
                        order_by="-start_check",
                        partition_by=F("monitor"),
                    ),
                )
                .filter(row_number__lte=60)
                .distinct(),
            )
        )
        .select_related("project")
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


@router.get(
    "organizations/{slug:organization_slug}/monitors/{int:monitor_id}/",
    response=MonitorDetailSchema,
    by_alias=True,
)
async def get_monitor(
    request: AuthHttpRequest, organization_slug: str, monitor_id: int
):
    return await aget_object_or_404(
        get_monitor_queryset(request.auth.user_id, organization_slug),
        id=monitor_id,
    )


@router.post(
    "organizations/{slug:organization_slug}/monitors/",
    response={201: MonitorSchema},
    by_alias=True,
)
async def create_monitor(
    request: AuthHttpRequest, organization_slug: str, payload: MonitorIn
):
    user_id = request.auth.user_id
    organization = await aget_object_or_404(
        Organization, slug=organization_slug, users=user_id
    )
    data = payload.dict(exclude_defaults=True)
    if project_id := data.pop("project", None):
        data["project"] = await organization.projects.filter(id=project_id).afirst()
    monitor = await Monitor.objects.acreate(organization=organization, **data)
    return 201, await get_monitor_queryset(user_id, organization_slug).aget(
        id=monitor.id
    )


@router.put(
    "organizations/{slug:organization_slug}/monitors/{int:monitor_id}/",
    response=MonitorSchema,
    by_alias=True,
)
async def update_monitor(
    request: AuthHttpRequest,
    organization_slug: str,
    monitor_id: int,
    payload: MonitorIn,
):
    monitor = await aget_object_or_404(
        get_monitor_queryset(request.auth.user_id, organization_slug),
        id=monitor_id,
    )
    for attr, value in payload.dict(exclude_none=True).items():
        setattr(monitor, attr, value)
    await monitor.asave()
    return monitor

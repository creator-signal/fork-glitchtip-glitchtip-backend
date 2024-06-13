from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import aget_object_or_404
from ninja import Router

from apps.organizations_ext.models import OrganizationUserRole
from apps.projects.models import Project
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.pagination import paginate
from glitchtip.api.permissions import has_permission

from .models import AlertRecipient, ProjectAlert
from .schema import ProjectAlertIn, ProjectAlertSchema

router = Router()


def get_project_alert_queryset(user_id: int, organization_slug: str, project_slug: str):
    return ProjectAlert.objects.filter(
        project__organization__users=user_id,
        project__organization__slug=organization_slug,
        project__slug=project_slug,
    ).prefetch_related("alertrecipient_set")


@router.get(
    "projects/{slug:organization_slug}/{slug:project_slug}/alerts/",
    response=list[ProjectAlertSchema],
    by_alias=True,
)
@has_permission(["project:read"])
@paginate
async def list_project_alerts(
    request: AuthHttpRequest,
    response: HttpResponse,
    organization_slug: str,
    project_slug: str,
):
    return get_project_alert_queryset(
        request.auth.user_id, organization_slug, project_slug
    )


@router.post(
    "projects/{slug:organization_slug}/{slug:project_slug}/alerts/",
    response={201: ProjectAlertSchema},
    by_alias=True,
)
@has_permission(["project:write", "project:admin"])
async def create_project_alert(
    request: AuthHttpRequest,
    organization_slug: str,
    project_slug: str,
    payload: ProjectAlertIn,
):
    user_id = request.auth.user_id
    project = await aget_object_or_404(
        Project.objects.filter(
            Q(
                organization__users=user_id,
                organization__organization_users__role__gte=OrganizationUserRole.ADMIN,
            )
            | Q(team__members__user=user_id)
        ).distinct(),
        organization__slug=organization_slug,
        slug=project_slug,
    )
    data = payload.dict()
    recipients = data.pop("alert_recipients")
    project_alert = await project.projectalert_set.acreate(**data)
    await AlertRecipient.objects.abulk_create(
        [AlertRecipient(alert=project_alert, **recipient) for recipient in recipients]
    )
    return await get_project_alert_queryset(
        user_id, organization_slug, project_slug
    ).aget(id=project_alert.id)


@router.put(
    "projects/{slug:organization_slug}/{slug:project_slug}/alerts/{alert_id}",
    response=ProjectAlertSchema,
)
@has_permission(["project:write", "project:admin"])
async def update_project_alert(
    request: AuthHttpRequest,
    organization_slug: str,
    project_slug: str,
    alert_id: int,
    payload: ProjectAlertIn,
):
    user_id = request.auth.user_id
    project_alert = await aget_object_or_404(
        get_project_alert_queryset(user_id, organization_slug, project_slug),
        id=alert_id,
    )

    data = payload.dict()
    alert_recipients = data.pop("alert_recipients")
    for attr, value in data.items():
        setattr(project_alert, attr, value)
    await project_alert.asave()

    # Create/Delete recipients as needed
    delete_recipient_ids = set(
        {
            id
            async for id in project_alert.alertrecipient_set.values_list(
                "id", flat=True
            )
        }
    )
    for recipient in alert_recipients:
        new_recipient, created = await AlertRecipient.objects.aget_or_create(
            alert=project_alert, **recipient
        )
        if not created:
            delete_recipient_ids.discard(new_recipient.pk)
    if delete_recipient_ids:
        await project_alert.alertrecipient_set.filter(
            pk__in=delete_recipient_ids
        ).adelete()

    return await get_project_alert_queryset(
        user_id, organization_slug, project_slug
    ).aget(id=project_alert.id)


@router.delete(
    "projects/{slug:organization_slug}/{slug:project_slug}/alerts/{alert_id}",
    response={204: None},
)
@has_permission(["project:admin"])
async def delete_project_alert(
    request: AuthHttpRequest, organization_slug: str, project_slug: str, alert_id: int
):
    user_id = request.auth.user_id
    result, _ = (
        await get_project_alert_queryset(user_id, organization_slug, project_slug)
        .filter(id=alert_id)
        .adelete()
    )
    if result:
        return 204, None
    raise Http404

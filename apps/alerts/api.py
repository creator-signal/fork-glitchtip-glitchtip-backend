from django.http import HttpResponse
from ninja import Router

from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.pagination import paginate
from glitchtip.api.permissions import has_permission

from .models import ProjectAlert
from .schema import ProjectAlertSchema

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
)
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


@router.post("projects/{slug:organization_slug}/{slug:project_slug}/alerts/")
@has_permission(["project:write", "project:admin"])
def create_alert(request: AuthHttpRequest, organization_slug: str, project_slug: str):
    pass


@router.put("projects/{slug:organization_slug}/{slug:project_slug}/alerts/{alert_id}")
@has_permission(["project:write", "project:admin"])
def update_alert(
    request: AuthHttpRequest, organization_slug: str, project_slug: str, alert_id: int
):
    pass


@router.delete(
    "projects/{slug:organization_slug}/{slug:project_slug}/alerts/{alert_id}"
)
@has_permission(["project:write", "project:admin"])
def delete_alert(
    request: AuthHttpRequest, organization_slug: str, project_slug: str, alert_id: int
):
    pass

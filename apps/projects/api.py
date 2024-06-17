from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import aget_object_or_404
from ninja import Router

from apps.organizations_ext.models import Organization, OrganizationUserRole
from apps.teams.models import Team
from glitchtip.api.pagination import paginate
from glitchtip.api.permissions import AuthHttpRequest, has_permission

from .models import Project
from .schema import ProjectSchema

router = Router()


"""
GET /api/0/projects/
GET /api/0/teams/{organization_slug}/{team_slug}/projects/
POST /api/0/teams/{organization_slug}/{team_slug}/projects/
POST /api/0/projects/{organization_slug}/{project_slug}/teams/{team_slug}/ (See teams)
DELETE /api/0/projects/{organization_slug}/{project_slug}/teams/{team_slug}/ (See teams)
"""


def get_projects_queryset(
    user_id: int, organization_slug: str = None, team_slug: str = None
):
    qs = Project.objects.filter(organization__users=user_id).annotate(
        is_member=Count("team__members", filter=Q(team__members__id=user_id))
    )
    if organization_slug:
        qs = qs.filter(organization__slug=organization_slug)
    if team_slug:
        qs = qs.filter(team__slug=team_slug)
    return qs


@router.get("projects/", response=list[ProjectSchema])
@paginate
@has_permission(["project:read"])
async def list_projects(request: AuthHttpRequest, response: HttpResponse):
    return get_projects_queryset(request.auth.user_id).order_by("name")


@router.get(
    "teams/{slug:organization_slug}/{slug:team_slug}/projects/",
    response=list[ProjectSchema],
)
@paginate
@has_permission(["project:read"])
async def list_team_projects(
    request: AuthHttpRequest,
    response: HttpResponse,
    organization_slug: str,
    team_slug: str,
):
    return get_projects_queryset(
        request.auth.user_id, organization_slug=organization_slug, team_slug=team_slug
    ).order_by("name")


@router.get(
    "teams/{slug:organization_slug}/{slug:team_slug}/projects/",
    response=ProjectSchema,
)
@paginate
@has_permission(["project:write", "project:admin"])
async def create_project(
    request: AuthHttpRequest, organization_slug: str, team_slug: str, payload
):
    user_id = request.auth.user_id
    team = await aget_object_or_404(
        Team,
        slug=team_slug,
        organization__slug=organization_slug,
        organization__users=user_id,
        organization__organization_users__role__gte=OrganizationUserRole.ADMIN,
    )
    organization = await aget_object_or_404(
        Organization,
        slug=organization_slug,
        users=user_id,
        organization_users__role__gte=OrganizationUserRole.ADMIN,
    )
    project = await Project.objects.acreate(organization=organization, **payload.dict())
    await project.team_set.aadd(team)
    return project

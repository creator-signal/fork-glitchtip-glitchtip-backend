from typing import Optional

from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.shortcuts import aget_object_or_404
from ninja import Router

from apps.organizations_ext.models import Organization, OrganizationUserRole
from apps.projects.models import Project
from apps.projects.schema import ProjectSchema
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.pagination import paginate
from glitchtip.api.permissions import has_permission

from .models import Team
from .schema import TeamIn, TeamSchema

router = Router()

TEAMS = "organizations/{slug:organization_slug}/teams"
TEAM_DETAIL = TEAMS + "/{slug:team_slug}"
PROJECT_TEAMS = "projects/{slug:organization_slug}/{slug:project_slug}/teams"
PROJECT_TEAM_DETAIL = PROJECT_TEAMS + "/{slug:team_slug}"


def get_team_queryset(
    organization_slug: str,
    team_slug: Optional[str] = None,
    project_slug: Optional[str] = None,
    user_id: Optional[int] = None,
    id: Optional[int] = None,
    add_details=False,
):
    qs = Team.objects.filter(organization__slug=organization_slug)
    if team_slug:
        qs = qs.filter(slug=team_slug)
    if project_slug:
        qs = qs.filter(projects__slug=project_slug)
    if user_id:
        qs = qs.filter(organization__users=user_id)
    if id:
        qs = qs.filter(id=id)
    if user_id and add_details:
        qs = qs.annotate(
            is_member=Count("members", filter=Q(members__user_id=user_id)),
            member_count=Count("members"),
        ).prefetch_related("projects")
    return qs


@router.get(TEAMS, response=list[TeamSchema], by_alias=True)
@paginate
@has_permission(["team:read", "team:write", "team:admin"])
async def list_teams(
    request: AuthHttpRequest, response: HttpResponse, organization_slug: str
):
    user_id = request.auth.user_id
    return get_team_queryset(organization_slug, user_id=user_id, add_details=True)


@router.post(TEAMS, response={201: TeamSchema}, by_alias=True)
@has_permission(["team:write", "team:admin"])
async def create_team(
    request: AuthHttpRequest, organization_slug: str, payload: TeamIn
):
    user_id = request.auth.user_id
    organization = await aget_object_or_404(
        Organization,
        slug=organization_slug,
        users=request.auth.user_id,
        organization_users__role__gte=OrganizationUserRole.ADMIN,
    )
    team = await Team.objects.acreate(organization=organization, slug=payload.slug)
    org_user = await organization.organization_users.filter(user=user_id).afirst()
    await team.members.aadd(org_user)
    return await get_team_queryset(
        organization_slug, user_id=user_id, id=team.id, add_details=True
    ).aget()


@router.delete(TEAM_DETAIL, response={204: None})
@has_permission(["team:admin"])
async def delete_team(request: AuthHttpRequest, organization_slug: str, team_slug: str):
    user_id = request.auth.user_id
    result, _ = (
        await get_team_queryset(organization_slug, team_slug=team_slug, user_id=user_id)
        .filter(
            organization__organization_users__role__gte=OrganizationUserRole.ADMIN,
        )
        .adelete()
    )
    if not result:
        raise Http404
    return 204, None


@router.get(PROJECT_TEAMS, response=list[TeamSchema], by_alias=True)
@paginate
@has_permission(["team:read", "team:write", "team:admin"])
async def list_project_teams(
    request: AuthHttpRequest,
    response: HttpResponse,
    organization_slug: str,
    project_slug: str,
):
    return get_team_queryset(
        organization_slug, project_slug=project_slug, user_id=request.auth.user_id
    ).distinct()


@router.post(PROJECT_TEAM_DETAIL, response={201: ProjectSchema}, by_alias=True)
@has_permission(["team:write", "team:admin"])
async def create_project_team(
    request: AuthHttpRequest, organization_slug: str, project_slug: str, team_slug: str
):
    """Add team to project"""
    user_id = request.auth.user_id
    team = await aget_object_or_404(
        get_team_queryset(organization_slug, team_slug=team_slug)
    )
    project = await aget_object_or_404(
        Project.objects.annotate(
            is_member=Count("team__members", filter=Q(team__members__id=user_id))
        ),
        slug=project_slug,
        organization__slug=organization_slug,
        organization__users=request.user,
        organization__organization_users__role__gte=OrganizationUserRole.MANAGER,
    )
    await project.team_set.aadd(team)
    return 201, project


@router.delete(PROJECT_TEAM_DETAIL, response=ProjectSchema)
@has_permission(["team:admin"])
async def delete_project_team(
    request: AuthHttpRequest, organization_slug: str, project_slug: str, team_slug: str
):
    """Remove team from project"""
    user_id = request.auth.user_id
    team = await aget_object_or_404(
        get_team_queryset(
            organization_slug, project_slug=project_slug, team_slug=team_slug
        )
    )
    qs = Project.objects.annotate(
        is_member=Count("team__members", filter=Q(team__members__id=user_id))
    )
    project = await aget_object_or_404(
        qs,
        slug=project_slug,
        organization__slug=organization_slug,
        organization__users=request.user,
        organization__organization_users__role__gte=OrganizationUserRole.MANAGER,
    )
    await project.team_set.aremove(team)
    return project

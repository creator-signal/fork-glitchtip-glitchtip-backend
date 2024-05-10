from typing import Optional

from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import aget_object_or_404
from ninja import Router

from apps.organizations_ext.models import OrganizationUserRole
from apps.projects.models import Project
from apps.projects.schema import ProjectSchema
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.pagination import paginate

from .models import Team
from .schema import TeamSchema

router = Router()


PROJECT_TEAMS = "projects/{slug:organization_slug}/{slug:project_slug}/teams"
PROJECT_TEAM_DETAIL = PROJECT_TEAMS + "/{slug:team_slug}"


def get_team_queryset(
    organization_slug: str,
    team_slug: Optional[str] = None,
    project_slug: Optional[str] = None,
    user_id: Optional[int] = None,
):
    qs = Team.objects.filter(organization__slug=organization_slug)
    if team_slug:
        qs = qs.filter(slug=team_slug)
    if project_slug:
        qs = qs.filter(projects__slug=project_slug)
    if user_id:
        qs = qs.filter(organization__users=user_id)
    return qs


@router.get(PROJECT_TEAMS, response=list[TeamSchema], by_alias=True)
@paginate
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

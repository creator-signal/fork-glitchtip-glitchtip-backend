from django.shortcuts import aget_object_or_404
from ninja import Router

from apps.organizations_ext.models import OrganizationUserRole
from apps.projects.models import Project
from apps.projects.schema import ProjectSchema
from glitchtip.api.authentication import AuthHttpRequest

from .models import Team

router = Router()


PROJECT_TEAMS = "projects/{slug:organization_slug}/{slug:project_slug}/teams"
PROJECT_TEAM_DETAIL = PROJECT_TEAMS + "/{slug:team_slug}"


def get_team_queryset(organization_slug: str, team_slug: str):
    return Team.objects.filter(organization__slug=organization_slug, slug=team_slug)


@router.get(PROJECT_TEAMS)
async def list_project_teams(request: AuthHttpRequest):
    pass


@router.post(PROJECT_TEAM_DETAIL, response={201: ProjectSchema})
async def create_project_team(
    request: AuthHttpRequest, organization_slug: str, project_slug: str, team_slug: str
):
    """Add team to project"""
    team = await aget_object_or_404(get_team_queryset(organization_slug, team_slug))
    project = await aget_object_or_404(
        Project,
        slug=project_slug,
        organization__slug=organization_slug,
        organization__users=request.user,
        organization__organization_users__role__gte=OrganizationUserRole.MANAGER,
    )
    await project.team_set.aadd(team)
    return 201, project
    # serializer = ProjectSerializer(instance=project, context={"request": request})
    # if request.method == "POST":
    #     project.team_set.add(team)
    #     return Response(serializer.data, status=status.HTTP_201_CREATED)


@router.delete(PROJECT_TEAM_DETAIL)
def delete_project_team(
    request: AuthHttpRequest, organization_slug: str, project_slug: str, team_slug: str
):
    pass

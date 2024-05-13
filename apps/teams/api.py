from typing import Optional

from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.shortcuts import aget_object_or_404
from ninja import Router

from apps.organizations_ext.models import (
    Organization,
    OrganizationUser,
    OrganizationUserRole,
)
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.pagination import paginate
from glitchtip.api.permissions import has_permission

from .models import Team
from .schema import TeamIn, TeamSchema

router = Router()


"""
OSS Sentry supported
- GET /teams/{org}/{team}/
- PUT /teams/{org}/{team}/
- DELETE /teams/{org}/{team}/
- GET /teams/{org}/{team}/members/ (See organizations)
- GET /teams/{org}/{team}/projects/ (See projects)
- GET /teams/{org}/{team}/stats/ (Not implemented)
- GET /organizations/{org}/teams/
- POST /organizations/{org}/teams/
- POST /organizations/{org}/members/{me|member_id}/teams/{team}/ (join)
- DELETE /organizations/{org}/members/{me|member_id}/teams/{team}/ (leave)
"""


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


@router.get(
    "teams/{slug:organization_slug}/{slug:team_slug}/",
    response=TeamSchema,
    by_alias=True,
)
@has_permission(["team:read", "team:write", "team:admin"])
async def get_team(request: AuthHttpRequest, organization_slug: str, team_slug: str):
    user_id = request.auth.user_id
    return await aget_object_or_404(
        get_team_queryset(organization_slug, user_id=user_id, add_details=True)
    )


@router.put(
    "teams/{slug:organization_slug}/teams/{slug:team_slug}/",
    response=TeamSchema,
    by_alias=True,
)
@has_permission(["team:write", "team:admin"])
async def update_team(
    request: AuthHttpRequest, organization_slug: str, team_slug: str, payload: TeamIn
):
    user_id = request.auth.user_id
    team = await aget_object_or_404(
        get_team_queryset(
            organization_slug, user_id=user_id, team_slug=team_slug, add_details=True
        )
    )
    team.slug = payload.slug
    await team.asave()
    return team


@router.delete(
    "teams/{slug:organization_slug}/teams/{slug:team_slug}/", response={204: None}
)
@has_permission(["team:admin"])
async def delete_team(request: AuthHttpRequest, organization_slug: str, team_slug: str):
    result, _ = (
        await get_team_queryset(
            organization_slug, team_slug=team_slug, user_id=request.auth.user_id
        )
        .filter(
            organization__organization_users__role__gte=OrganizationUserRole.ADMIN,
        )
        .adelete()
    )
    if not result:
        raise Http404
    return 204, None


@router.get(
    "/organizations/{slug:organization_slug}/teams/",
    response=list[TeamSchema],
    by_alias=True,
)
@paginate
@has_permission(
    ["team:read", "team:write", "team:admin", "org:read", "org:write", "org:admin"]
)
async def list_teams(
    request: AuthHttpRequest, response: HttpResponse, organization_slug: str
):
    return get_team_queryset(
        organization_slug, user_id=request.auth.user_id, add_details=True
    )


@router.post(
    "/organizations/{slug:organization_slug}/teams/",
    response={201: TeamSchema},
    by_alias=True,
)
@has_permission(["team:write", "team:admin", "org:admin", "org:write"])
async def create_team(
    request: AuthHttpRequest, organization_slug: str, payload: TeamIn
):
    user_id = request.auth.user_id
    organization = await aget_object_or_404(
        Organization,
        slug=organization_slug,
        users=user_id,
        organization_users__role__gte=OrganizationUserRole.ADMIN,
    )
    team = await Team.objects.acreate(organization=organization, slug=payload.slug)
    org_user = await organization.organization_users.filter(user=user_id).afirst()
    await team.members.aadd(org_user)
    return await get_team_queryset(
        organization_slug, user_id=user_id, id=team.id, add_details=True
    ).aget()


@router.post(
    "/organizations/{slug:organization_slug}/members/{slug:member_id}/teams/{slug:team_slug}/",
    response={201: TeamSchema},
    by_alias=True,
)
@has_permission(["team:write", "team:admin"])
async def add_member_to_team(
    request: AuthHttpRequest, organization_slug: str, member_id: str, team_slug: str
):
    user_id = request.auth.user_id
    team = await aget_object_or_404(
        get_team_queryset(
            organization_slug, user_id=user_id, team_slug=team_slug, add_details=True
        )
    )
    if member_id == "me":
        org_member = await OrganizationUser.objects.aget(user_id=user_id)
    else:
        org_member = await aget_object_or_404(OrganizationUser, id=member_id)
    await team.members.aadd(org_member)
    return 201, team


@router.delete(
    "/organizations/{slug:organization_slug}/members/{slug:member_id}/teams/{slug:team_slug}/",
    response=TeamSchema,
)
@has_permission(["team:write", "team:admin"])
async def delete_member_from_team(
    request: AuthHttpRequest, organization_slug: str, member_id: str, team_slug: str
):
    user_id = request.auth.user_id
    team = await aget_object_or_404(
        get_team_queryset(
            organization_slug, user_id=user_id, team_slug=team_slug, add_details=True
        )
    )
    if member_id == "me":
        org_member = await OrganizationUser.objects.aget(user_id=user_id)
    else:
        org_member = await aget_object_or_404(OrganizationUser, id=member_id)
    await team.members.aremove(org_member)
    return team


# async def
#     """Add team to project"""
#     user_id = request.auth.user_id
#     team = await aget_object_or_404(
#         get_team_queryset(organization_slug, team_slug=team_slug)
#     )
#     project = await aget_object_or_404(
#         Project.objects.annotate(
#             is_member=Count("team__members", filter=Q(team__members__id=user_id))
#         ),
#         slug=project_slug,
#         organization__slug=organization_slug,
#         organization__users=request.user,
#         organization__organization_users__role__gte=OrganizationUserRole.MANAGER,
#     )
#     await project.team_set.aadd(team)
#     return 201, project


# @router.delete("", response=ProjectSchema)
# @has_permission(["team.write", "team:admin"])
# async def delete_member_from_team(
#     request: AuthHttpRequest, organization_slug: str, project_slug: str, team_slug: str
# ):
#     """Remove team from project"""
#     user_id = request.auth.user_id
#     team = await aget_object_or_404(
#         get_team_queryset(
#             organization_slug, project_slug=project_slug, team_slug=team_slug
#         )
#     )
#     qs = Project.objects.annotate(
#         is_member=Count("team__members", filter=Q(team__members__id=user_id))
#     )
#     project = await aget_object_or_404(
#         qs,
#         slug=project_slug,
#         organization__slug=organization_slug,
#         organization__users=request.user,
#         organization__organization_users__role__gte=OrganizationUserRole.MANAGER,
#     )
#     await project.team_set.aremove(team)
#     return project

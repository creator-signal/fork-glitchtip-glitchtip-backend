from ninja import Router

from glitchtip.api.authentication import AuthHttpRequest

router = Router()


@router.post(
    "projects/{slug:organization_slug}/{slug:project_slug}/teams/{slug:team_slug}",
)
def create_project_team(
    request: AuthHttpRequest, organization_slug: str, project_slug: str, team_slug: str
):
    pass
    # team = get_object_or_404(self.get_queryset(), slug=team_slug)
    # project = get_object_or_404(
    #     Project,
    #     slug=project_slug,
    #     organization__slug=organization_slug,
    #     organization__users=self.request.user,
    #     organization__organization_users__role__gte=OrganizationUserRole.MANAGER,
    # )
    # serializer = ProjectSerializer(instance=project, context={"request": request})
    # if request.method == "POST":
    #     project.team_set.add(team)
    #     return Response(serializer.data, status=status.HTTP_201_CREATED)


@router.delete(
    "projects/{slug:organization_slug}/{slug:project_slug}/teams/{slug:team_slug}",
)
def delete_project_team(
    request: AuthHttpRequest, organization_slug: str, project_slug: str, team_slug: str
):
    pass

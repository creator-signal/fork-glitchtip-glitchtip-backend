from django.shortcuts import get_object_or_404
from rest_framework import mixins, viewsets

from .models import Project
from .permissions import ProjectPermission
from .serializers.serializers import BaseProjectSerializer, ProjectSerializer


class BaseProjectViewSet(mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = BaseProjectSerializer
    queryset = Project.undeleted_objects.all()
    lookup_field = "slug"
    permission_classes = [ProjectPermission]

    def get_object(self):
        queryset = self.filter_queryset(self.get_queryset())
        slug = self.kwargs.get("project_slug", self.kwargs.get("slug"))
        obj = get_object_or_404(
            queryset,
            slug=slug,
            organization__slug=self.kwargs["organization_slug"],
        )

        self.check_object_permissions(self.request, obj)

        return obj

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return self.queryset.none()
        queryset = self.queryset.filter(
            organization__users=self.request.user
        ).prefetch_related("teams")
        organization_slug = self.kwargs.get("organization_slug")
        if organization_slug:
            queryset = queryset.filter(organization__slug=organization_slug)
        team_slug = self.kwargs.get("team_slug")
        if team_slug:
            queryset = queryset.filter(teams__slug=team_slug)
        return queryset


class ProjectViewSet(BaseProjectViewSet):
    """
    /api/0/projects/

    Includes organization
    Detail view includes teams
    """

    serializer_class = ProjectSerializer
    lookup_field = "pk"
    lookup_value_regex = r"(?P<organization_slug>[^/.]+)/(?P<project_slug>[-\w]+)"

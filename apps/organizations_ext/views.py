from rest_framework import viewsets
from rest_framework.filters import OrderingFilter

from .models import Organization, OrganizationUser
from .permissions import OrganizationMemberPermission
from .serializers.serializers import (
    OrganizationSerializer,
    OrganizationUserProjectsSerializer,
    OrganizationUserSerializer,
)


class OrganizationViewSet(viewsets.GenericViewSet):
    filter_backends = [OrderingFilter]
    ordering = ["name"]
    ordering_fields = ["name"]
    queryset = Organization.objects.none()
    serializer_class = OrganizationSerializer
    lookup_field = "slug"


class OrganizationMemberViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API compatible with undocumented Sentry endpoint `/api/organizations/<slug>/members/`
    """

    queryset = OrganizationUser.objects.all()
    serializer_class = OrganizationUserSerializer
    permission_classes = [OrganizationMemberPermission]

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return self.queryset.none()
        queryset = self.queryset.filter(organization__users=self.request.user)
        organization_slug = self.kwargs.get("organization_slug")
        if organization_slug:
            queryset = queryset.filter(organization__slug=organization_slug)
        team_slug = self.kwargs.get("team_slug")
        if team_slug:
            queryset = queryset.filter(teams__slug=team_slug)
        return queryset.select_related("organization", "user").prefetch_related(
            "user__socialaccount_set", "organization__owner"
        )


class OrganizationUserViewSet(OrganizationMemberViewSet):
    """
    Extension of OrganizationMemberViewSet that adds projects the user has access to

    API compatible with [get-organization-users](https://docs.sentry.io/api/organizations/get-organization-users/)
    """

    serializer_class = OrganizationUserProjectsSerializer

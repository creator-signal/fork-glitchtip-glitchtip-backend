from django.core.exceptions import ObjectDoesNotExist
from django.shortcuts import get_object_or_404
from rest_framework import exceptions, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.filters import OrderingFilter

from .models import Organization, OrganizationUser, OrganizationUserRole
from .permissions import OrganizationMemberPermission
from .serializers.serializers import (
    OrganizationSerializer,
    OrganizationUserDetailSerializer,
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

    def get_serializer_class(self):
        if self.action in ["retrieve"]:
            return OrganizationUserDetailSerializer
        return super().get_serializer_class()

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

    def get_object(self):
        pk = self.kwargs.get("pk")
        if pk == "me":
            obj = get_object_or_404(self.get_queryset(), user=self.request.user)
            self.check_object_permissions(self.request, obj)
            return obj
        return super().get_object()

    def check_permissions(self, request):
        if self.request.user.is_authenticated and self.action in [
            "create",
            "update",
            "partial_update",
            "destroy",
        ]:
            org_slug = self.kwargs.get("organization_slug")
            try:
                user_org_user = (
                    self.request.user.organizations_ext_organizationuser.get(
                        organization__slug=org_slug
                    )
                )
            except ObjectDoesNotExist:
                raise PermissionDenied("Not a member of this organization")
            if user_org_user.role < OrganizationUserRole.MANAGER:
                raise PermissionDenied(
                    "Must be manager or higher to add/remove organization members"
                )
        return super().check_permissions(request)


class OrganizationUserViewSet(OrganizationMemberViewSet):
    """
    Extension of OrganizationMemberViewSet that adds projects the user has access to

    API compatible with [get-organization-users](https://docs.sentry.io/api/organizations/get-organization-users/)
    """

    serializer_class = OrganizationUserProjectsSerializer

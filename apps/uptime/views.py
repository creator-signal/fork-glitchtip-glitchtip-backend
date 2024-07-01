from django.db.models import Q
from django.views.generic import DetailView
from rest_framework import exceptions, viewsets

from apps.organizations_ext.models import Organization

from .models import Monitor, StatusPage
from .serializers import StatusPageSerializer


class MonitorViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Monitor.objects.none()


class StatusPageViewSet(viewsets.ModelViewSet):
    queryset = StatusPage.objects.all()
    serializer_class = StatusPageSerializer
    lookup_field = "slug"

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return self.queryset.none()

        queryset = self.queryset.filter(organization__users=self.request.user)
        organization_slug = self.kwargs.get("organization_slug")
        if organization_slug:
            queryset = queryset.filter(organization__slug=organization_slug)
        return queryset

    def perform_create(self, serializer):
        try:
            organization = Organization.objects.get(
                slug=self.kwargs.get("organization_slug"), users=self.request.user
            )
        except Organization.DoesNotExist as exc:
            raise exceptions.ValidationError("Organization not found") from exc
        serializer.save(organization=organization)


class StatusPageDetailView(DetailView):
    model = StatusPage

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.request.user.is_authenticated:
            queryset = queryset.filter(
                Q(is_public=True) | Q(organization__users=self.request.user)
            )
        else:
            queryset = queryset.filter(is_public=True)

        return queryset.filter(
            organization__slug=self.kwargs.get("organization")
        ).distinct()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["monitors"] = Monitor.objects.with_check_annotations().filter(
            statuspage=self.object
        )
        return context

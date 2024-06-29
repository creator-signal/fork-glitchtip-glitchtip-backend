from django.db.models import F, Prefetch, Q, Window
from django.db.models.functions import RowNumber
from django.utils import timezone
from django.views.generic import DetailView
from rest_framework import exceptions, viewsets

from apps.organizations_ext.models import Organization
from glitchtip.pagination import LinkHeaderPagination

from .models import Monitor, MonitorCheck, StatusPage
from .serializers import (
    MonitorCheckSerializer,
    MonitorDetailSerializer,
    MonitorSerializer,
    MonitorUpdateSerializer,
    StatusPageSerializer,
)


class MonitorViewSet(viewsets.ModelViewSet):
    queryset = Monitor.objects.with_check_annotations()
    serializer_class = MonitorSerializer

    def get_serializer_class(self):
        if self.action in ["retrieve"]:
            return MonitorDetailSerializer
        elif self.action in ["update"]:
            return MonitorUpdateSerializer
        return super().get_serializer_class()

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return self.queryset.none()

        queryset = self.queryset.filter(organization__users=self.request.user)
        organization_slug = self.kwargs.get("organization_slug")
        if organization_slug:
            queryset = queryset.filter(organization__slug=organization_slug)

        # Fetch latest 60 checks for each monitor
        queryset = queryset.prefetch_related(
            Prefetch(
                "checks",
                queryset=MonitorCheck.objects.filter(  # Optimization
                    start_check__gt=timezone.now() - timezone.timedelta(hours=12)
                )
                .annotate(
                    row_number=Window(
                        expression=RowNumber(),
                        order_by="-start_check",
                        partition_by=F("monitor"),
                    ),
                )
                .filter(row_number__lte=60)
                .distinct(),
            )
        ).select_related("project")
        return queryset

    def perform_create(self, serializer):
        try:
            organization = Organization.objects.get(
                slug=self.kwargs.get("organization_slug"), users=self.request.user
            )
        except Organization.DoesNotExist as exc:
            raise exceptions.ValidationError("Organization not found") from exc
        serializer.save(organization=organization)


class MonitorCheckPagination(LinkHeaderPagination):
    ordering = "-start_check"


class MonitorCheckViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = MonitorCheck.objects.all()
    serializer_class = MonitorCheckSerializer
    pagination_class = MonitorCheckPagination
    filterset_fields = ["is_change"]

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return self.queryset.none()

        queryset = self.queryset.filter(monitor__organization__users=self.request.user)
        organization_slug = self.kwargs.get("organization_slug")
        if organization_slug:
            queryset = queryset.filter(monitor__organization__slug=organization_slug)
        monitor_pk = self.kwargs.get("monitor_pk")
        if monitor_pk:
            queryset = queryset.filter(monitor__pk=monitor_pk)
        return queryset.only("is_up", "start_check", "reason", "response_time")


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

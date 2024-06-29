from datetime import datetime

from django.conf import settings
from django.urls import reverse
from ninja import Field, ModelSchema

from glitchtip.schema import CamelSchema

from .models import Monitor, MonitorCheck


class MonitorCheckSchema(CamelSchema, ModelSchema):
    class Meta:
        model = MonitorCheck
        fields = ["is_up", "start_check", "reason"]


class MonitorSchema(CamelSchema, ModelSchema):
    is_up: bool = Field(validation_alias="latest_is_up")
    last_change: datetime
    heartbeat_endpoint: str | None
    project_name: str | None = None
    env_name: str | None = None

    class Meta:
        model = Monitor
        fields = [
            "id",
            "monitor_type",
            "endpoint_id",
            "created",
            # "checks",
            "name",
            "url",
            "expected_status",
            "expected_body",
            # "environment",
            # "project",
            # "organization",
            "interval",
            "timeout",
        ]

    @staticmethod
    def resolve_heartbeat_endpoint(obj):
        if obj.endpoint_id:
            return settings.GLITCHTIP_URL.geturl() + reverse(
                "heartbeat-check",
                kwargs={
                    "organization_slug": obj.organization.slug,
                    "endpoint_id": obj.endpoint_id,
                },
            )

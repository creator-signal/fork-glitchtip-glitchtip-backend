from urllib.parse import urlparse

from django.conf import settings
from django.core.validators import URLValidator
from django.urls import reverse
from ninja import Field, ModelSchema
from ninja.errors import ValidationError
from pydantic import model_validator

from glitchtip.schema import CamelSchema

from .constants import HTTP_MONITOR_TYPES, MonitorType
from .models import Monitor, MonitorCheck


class MonitorCheckSchema(CamelSchema, ModelSchema):
    class Meta:
        model = MonitorCheck
        fields = ["is_up", "start_check", "reason"]


class MonitorCheckResponseTimeSchema(MonitorCheckSchema, ModelSchema):
    class Meta(MonitorCheckSchema.Meta):
        fields = MonitorCheckSchema.Meta.fields + ["response_time"]


class MonitorIn(CamelSchema, ModelSchema):
    # project: int | None

    @model_validator(mode="after")
    def validate(self):
        monitor_type = self.monitor_type
        if self.url == "" and monitor_type in HTTP_MONITOR_TYPES + (MonitorType.SSL,):
            raise ValidationError("URL is required for " + monitor_type)

        if monitor_type in HTTP_MONITOR_TYPES:
            URLValidator()(self.url)

        if self.expected_status is None and monitor_type in [
            MonitorType.GET,
            MonitorType.POST,
        ]:
            raise ValidationError("Expected status is required for " + monitor_type)

        if monitor_type == MonitorType.PORT:
            url = self.url.replace("http://", "//", 1)
            if not url.startswith("//"):
                url = "//" + url
            parsed_url = urlparse(url)
            message = "Invalid Port URL, expected hostname and port"
            try:
                if not all([parsed_url.hostname, parsed_url.port]):
                    raise ValidationError(message)
            except ValueError as err:
                raise ValidationError(message) from err
            self.url = f"{parsed_url.hostname}:{parsed_url.port}"

        return self

    class Meta:
        model = Monitor
        fields = [
            "monitor_type",
            "name",
            "url",
            "expected_status",
            "expected_body",
            "project",
            "interval",
            "timeout",
        ]


class MonitorSchema(MonitorIn):
    project: int | None = Field(validation_alias="project_id")
    environment: int | None = Field(validation_alias="environment_id")
    is_up: bool | None = Field(validation_alias="latest_is_up")
    last_change: str | None
    heartbeat_endpoint: str | None
    project_name: str | None = None
    env_name: str | None = None
    checks: list[MonitorCheckSchema]
    organization: int = Field(validation_alias="organization_id")

    class Meta(MonitorIn.Meta):
        fields = [
            "id",
            "monitor_type",
            "endpoint_id",
            "created",
            "name",
            "url",
            "expected_status",
            "expected_body",
            "interval",
            "timeout",
        ]

    @staticmethod
    def resolve_last_change(obj):
        if obj.last_change:
            return obj.last_change.isoformat().replace("+00:00", "Z")

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


class MonitorDetailSchema(MonitorSchema):
    checks: list[MonitorCheckResponseTimeSchema]

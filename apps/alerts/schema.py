from typing import Optional

from ninja import Field, ModelSchema
from pydantic import HttpUrl

from glitchtip.schema import CamelSchema

from .constants import RecipientType
from .models import AlertRecipient, ProjectAlert


class AlertRecipientIn(CamelSchema):
    recipient_type: RecipientType
    url: HttpUrl


class AlertRecipientSchema(CamelSchema, ModelSchema):
    class Meta:
        model = AlertRecipient
        fields = ["id", "recipient_type", "url"]


class ProjectAlertIn(CamelSchema, ModelSchema):
    name: str = Field(default="")
    alert_recipients: Optional[list[AlertRecipientIn]] = Field(default_factory=list)

    class Meta:
        model = ProjectAlert
        fields = [
            "name",
            "timespan_minutes",
            "quantity",
            "uptime",
        ]


class ProjectAlertSchema(CamelSchema, ModelSchema):
    alert_recipients: list[AlertRecipientSchema] = Field(
        validation_alias="alertrecipient_set"
    )

    class Meta(ProjectAlertIn.Meta):
        fields = ["id"] + ProjectAlertIn.Meta.fields

from typing import Annotated, Literal

from ninja import Field, ModelSchema
from pydantic import HttpUrl

from glitchtip.schema import CamelSchema

from .constants import RecipientType
from .models import AlertRecipient, ProjectAlert


class EmailAlertRecipientIn(CamelSchema):
    recipient_type: Literal[RecipientType.EMAIL]
    url: HttpUrl | Literal[""] | None = Field(default="")
    tags_to_add: list[str] | None = Field(default_factory=list)


class WebhookAlertRecipientIn(CamelSchema):
    recipient_type: Literal[
        RecipientType.DISCORD,
        RecipientType.GENERAL_WEBHOOK,
        RecipientType.GOOGLE_CHAT,
        RecipientType.MICROSOFT_TEAMS,
        RecipientType.NTFY,
    ]
    url: HttpUrl
    tags_to_add: list[str] | None = Field(default_factory=list)


AlertRecipientIn = Annotated[
    EmailAlertRecipientIn | WebhookAlertRecipientIn,
    Field(discriminator="recipient_type"),
]


class AlertRecipientSchema(CamelSchema, ModelSchema):
    class Meta:
        model = AlertRecipient
        fields = ["id", "recipient_type", "url"]


class TestAlertResultSchema(CamelSchema):
    recipient_type: str
    status: str  # "sent", "error", "skipped"
    message: str | None = None


class ProjectAlertIn(CamelSchema, ModelSchema):
    name: str | None = None
    alert_recipients: list[AlertRecipientIn] | None = Field(default_factory=list)

    class Meta:
        model = ProjectAlert
        fields = ["name", "timespan_minutes", "quantity", "uptime"]


class ProjectAlertSchema(CamelSchema, ModelSchema):
    alert_recipients: list[AlertRecipientSchema]

    class Meta(ProjectAlertIn.Meta):
        fields = ["id"] + ProjectAlertIn.Meta.fields

    @staticmethod
    def resolve_alert_recipients(obj):
        return obj.alertrecipient_set

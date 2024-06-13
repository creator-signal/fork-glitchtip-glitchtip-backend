from ninja import Field, ModelSchema

from glitchtip.schema import CamelSchema

from .models import AlertRecipient, ProjectAlert


class AlertRecipientSchema(CamelSchema, ModelSchema):
    class Meta:
        model = AlertRecipient
        fields = ["id", "recipient_type", "url"]


class ProjectAlertSchema(CamelSchema, ModelSchema):
    alert_recipients: list[AlertRecipientSchema] = Field(
        validation_alias="alertrecipient_set"
    )

    class Meta:
        model = ProjectAlert
        fields = [
            "id",
            "name",
            "timespan_minutes",
            "quantity",
            "uptime",
        ]

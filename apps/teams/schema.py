from ninja import ModelSchema

from glitchtip.schema import CamelSchema

from .models import Team


class TeamSchema(CamelSchema, ModelSchema):
    class Meta:
        model = Team
        fields = ["id", "slug"]

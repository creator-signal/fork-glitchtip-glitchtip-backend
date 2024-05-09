from typing import Optional

from ninja import ModelSchema

from glitchtip.schema import CamelSchema

from .models import Project


class ProjectSchema(CamelSchema, ModelSchema):
    avatar: dict[str, Optional[str]] = {"avatarType": "", "avatarUuid": None}
    color: str = ""
    features: list = []
    has_access: bool = True
    is_bookmarked: bool = False
    is_internal: bool = False
    # is_member: bool

    class Meta:
        model = Project
        fields = [
            "first_event",
            "id",
            # "isPublic",
            # "name",
            # "scrubIPAddresses",
            "slug",
            # "dateCreated",
            # "platform",
            # "eventThrottleRate",
        ]

    class Config(CamelSchema.Config):
        pass

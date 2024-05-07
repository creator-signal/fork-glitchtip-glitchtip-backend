from typing import Optional

from ninja import ModelSchema

from glitchtip.schema import CamelSchema, to_camel

from .models import Project


class ProjectSchema(CamelSchema, ModelSchema):
    avatar: dict[str, Optional[str]] = {"avatarType": "", "avatarUuid": None}
    color: str = ""
    features: list = []

    class Config(CamelSchema.Config):
        model = Project
        model_fields = [
            "first_event",
            # "hasAccess",
            "id",
            # "isBookmarked",
            # "isInternal",
            # "isMember",
            # "isPublic",
            # "name",
            # "scrubIPAddresses",
            "slug",
            # "dateCreated",
            # "platform",
            # "eventThrottleRate",
        ]
        alias_generator = to_camel
        populate_by_name = True

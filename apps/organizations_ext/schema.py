from datetime import datetime
from typing import Literal, Optional

from ninja import Field, ModelSchema
from pydantic import EmailStr

from apps.users.schema import UserSchema
from glitchtip.schema import CamelSchema

from .models import (
    Organization,
    OrganizationUser,
)


class OrganizationInSchema(CamelSchema, ModelSchema):
    class Meta:
        model = Organization
        fields = [
            "name",
        ]


class OrganizationSchema(OrganizationInSchema, ModelSchema):
    date_created: datetime = Field(validation_alias="created")
    status: dict[str, str] = {"id": "active", "name": "active"}
    avatar: dict[str, Optional[str]] = {"avatarType": "", "avatarUuid": None}
    is_early_adopter: bool = False
    require2fa: bool = False

    class Meta(OrganizationInSchema.Meta):
        fields = [
            "id",
            "name",
            "slug",
            "is_accepting_events",
        ]


OrgRole = Literal["member", "admin", "manager", "owner"]


class TeamRole(CamelSchema):
    team_slug: str
    role: str


class OrganizationUserUpdateSchema(CamelSchema):
    org_role: OrgRole
    team_roles: list[TeamRole] = Field(default_factory=list)


class OrganizationUserIn(OrganizationUserUpdateSchema):
    email: EmailStr
    send_invite: bool = True
    reinvite: bool = True


class OrganizationUserSchema(CamelSchema, ModelSchema):
    id: str
    role: str = Field(validation_alias="get_role")
    role_name: str = Field(validation_alias="get_role_display")
    date_created: datetime = Field(validation_alias="created")
    email: str = Field(validation_alias="get_email")
    user: UserSchema
    pending: bool

    class Meta:
        model = OrganizationUser
        fields = ["id"]

    class Config:
        coerce_numbers_to_str = True


class OrganizationUserDetailSchema(OrganizationUserSchema):
    teams: list[str]

    @staticmethod
    def resolve_teams(obj):
        return [team.slug for team in obj.teams.all()]

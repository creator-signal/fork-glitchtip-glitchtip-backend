from typing import Any

from ninja import ModelSchema
from ninja.errors import ValidationError
from pydantic import ValidationInfo, field_validator

from bitfield.types import BitHandler

from .models import APIToken


def bitfield_to_internal_value(model, v: Any, info: ValidationInfo) -> int:
    if not info.field_name:
        raise Exception("Improperly used bitfield_to_internal_value")

    if isinstance(v, list):
        model_field = getattr(model, info.field_name)
        result = BitHandler(0, model_field.keys())
        for k in v:
            try:
                setattr(result, str(k), True)
            except AttributeError:
                raise ValidationError([{"scopes": "Invalid scope"}])
        v = result

    if isinstance(v, BitHandler):
        return v.mask

    return v


class APITokenIn(ModelSchema):
    scopes: int

    class Meta:
        model = APIToken
        fields = ("label",)
        fields_optional = ["label"]

    @field_validator("scopes", mode="before")
    @classmethod
    def scopes_to_bitfield(cls, v, info: ValidationInfo):
        return bitfield_to_internal_value(cls.Meta.model, v, info)


def _resolve_scopes(obj) -> list[str]:
    """Example: ['member:read']"""
    scopes: BitHandler
    # Must accept both kwarg and model object
    if isinstance(obj, APIToken):
        scopes = obj.scopes
    if isinstance(obj, dict):
        scopes = obj.get("scopes")
    return [i[0] for i in scopes.items() if i[1] is True]


class APITokenSchema(ModelSchema):
    """Full token schema — used only for POST create response."""

    id: int
    label: str
    scopes: list[str]
    token: str

    class Meta:
        model = APIToken
        fields = ("created",)

    resolve_scopes = staticmethod(_resolve_scopes)


class APITokenListSchema(ModelSchema):
    """Masked token schema — used for GET list response."""

    id: int
    label: str
    scopes: list[str]
    token: str

    class Meta:
        model = APIToken
        fields = ("created",)

    resolve_scopes = staticmethod(_resolve_scopes)

    @staticmethod
    def resolve_token(obj) -> str:
        token = obj.token if isinstance(obj, APIToken) else obj["token"]
        return f"…{token[-8:]}"

from ninja import ModelSchema

from glitchtip.schema import CamelSchema

from .models import User


class UserIn(CamelSchema, ModelSchema):
    class Meta:
        model = User
        fields = [
            # "username",
            # "emails",
            # "identities",
            "name",
            # "email",
            "options",
        ]


class UserSchema(CamelSchema, ModelSchema):
    class Meta(UserIn.Meta):
        fields = [
            # "username",
            "last_login",
            "is_superuser",
            # "emails",
            # "identities",
            "id",
            "is_active",
            "name",
            # "dateJoined",
            # "hasPasswordAuth",
            "email",
            "options",
        ]

from django.db import models
from django.utils.translation import ugettext_lazy as _
from organizations.base import (
    OrganizationBase,
    OrganizationUserBase,
    OrganizationOwnerBase,
)
from organizations.fields import SlugField
from organizations.abstract import SharedBaseModel


class Organization(SharedBaseModel, OrganizationBase):
    slug = SlugField(
        max_length=200,
        blank=False,
        editable=True,
        populate_from="name",
        unique=True,
        help_text=_("The name in all lowercase, suitable for URL identification"),
    )


class OrganizationUserRole(models.IntegerChoices):
    MEMBER = 0, "Member"
    ADMIN = 1, "Admin"
    MANAGER = 2, "Manager"
    OWNER = 3, "Owner"


class OrganizationUser(SharedBaseModel, OrganizationUserBase):
    role = models.PositiveSmallIntegerField(choices=OrganizationUserRole.choices)


class OrganizationOwner(OrganizationOwnerBase):
    pass

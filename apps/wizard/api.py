import string

from django.core.cache import cache
from django.db.models import Count, Q
from django.http import Http404
from django.utils.crypto import get_random_string
from ninja import Field, Router, Schema
from ninja.errors import HttpError

from apps.api_tokens.models import APIToken
from apps.api_tokens.schema import APITokenSchema
from apps.projects.models import Project
from apps.projects.schema import ProjectWithKeysSchema
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.schema import CamelSchema

from .constants import (
    SETUP_WIZARD_CACHE_EMPTY,
    SETUP_WIZARD_CACHE_KEY,
    SETUP_WIZARD_CACHE_TIMEOUT,
)


class SetupWizardSchema(Schema):
    hash: str = Field(min_length=64, max_length=64)


class SetupWizardResultSchema(CamelSchema):
    api_keys: APITokenSchema
    projects: list[ProjectWithKeysSchema]


router = Router()


@router.get("wizard/", response=SetupWizardSchema, auth=None)
def setup_wizard(request):
    wizard_hash = get_random_string(
        64, allowed_chars=string.ascii_lowercase + string.digits
    )
    key = SETUP_WIZARD_CACHE_KEY + wizard_hash
    cache.set(key, SETUP_WIZARD_CACHE_EMPTY, SETUP_WIZARD_CACHE_TIMEOUT)
    return {"hash": wizard_hash}


@router.get("wizard/{wizard_hash}/")
async def setup_wizard_hash(request, wizard_hash: str, auth=None):
    key = SETUP_WIZARD_CACHE_KEY + wizard_hash
    wizard_data = cache.get(key)

    if wizard_data is None:
        raise Http404
    elif wizard_data == SETUP_WIZARD_CACHE_EMPTY:
        raise HttpError(400)

    return wizard_data


@router.delete("wizard/{wizard_hash}/")
def setup_wizard_delete(request, wizard_hash: str, auth=None):
    cache.delete(SETUP_WIZARD_CACHE_KEY + wizard_hash)


@router.post("wizard-set-token/")
async def setup_wizard_set_token(request: AuthHttpRequest, payload: SetupWizardSchema):
    wizard_hash = payload.hash
    key = SETUP_WIZARD_CACHE_KEY + wizard_hash
    wizard_data = cache.get(key)
    if wizard_data is None:
        raise HttpError(400)

    user_id = request.auth.user_id
    projects = [
        project
        async for project in Project.objects.filter(organization__users=user_id)
        .annotate(is_member=Count("team__members", filter=Q(team__members__id=user_id)))
        .select_related("organization")
        .prefetch_related("projectkey_set")[:50]
    ]

    scope = getattr(APIToken.scopes, "project:releases")
    token = await APIToken.objects.filter(user=user_id, scopes=scope).afirst()
    if not token:
        token = await APIToken.objects.acreate(user_id=user_id, scopes=scope)

    result = SetupWizardResultSchema(api_keys=token, projects=projects)
    cache.set(key, result.dict(by_alias=True), SETUP_WIZARD_CACHE_TIMEOUT)

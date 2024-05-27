from django.http import HttpResponse
from django.shortcuts import aget_object_or_404
from ninja import Router

from apps.shared.types import MeID
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.pagination import paginate

from .models import User
from .schema import UserSchema

router = Router()


"""
GET /users/
GET /users/<me_id>/
GET /organizations/burke-software/users/
"""


def get_user_queryset(user_id: int):
    return User.objects.filter(id=user_id)


@router.get("/users/", response=list[UserSchema])
@paginate
async def list_users(request: AuthHttpRequest, response: HttpResponse):
    return get_user_queryset(user_id=request.auth.user_id)


@router.get("/users/{slug:user_id}/", response=UserSchema)
async def get_user(request: AuthHttpRequest, user_id: MeID):
    if user_id == "me":
        user_id = request.auth.user_id
    return aget_object_or_404(get_user_queryset(user_id))


@router.get("/organization/{slug:organization_slug}/users/", response=list[UserSchema])
@paginate
async def list_organization_users(
    request: AuthHttpRequest, response: HttpResponse, organization_slug: str
):
    return []

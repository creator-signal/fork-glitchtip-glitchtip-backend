from django.http import HttpResponse
from ninja import Router
from ninja.pagination import paginate

from glitchtip.api.authentication import AuthHttpRequest

from .models import TransactionEvent

router = Router()


@router.get("organizations/{slug:organization_slug}/transactions/", response=list)
@paginate
async def list_transactions(
    request: AuthHttpRequest, response: HttpResponse, organization_slug: str
):
    return TransactionEvent.objects.filter(
        group__project__organization__slug=organization_slug
    ).order_by("start_timestamp")

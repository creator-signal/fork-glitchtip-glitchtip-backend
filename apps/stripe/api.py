from ninja import ModelSchema, Router

from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.schema import CamelSchema

from .models import StripeProduct

router = Router()


class StripeProductSchema(CamelSchema, ModelSchema):
    class Meta:
        model = StripeProduct
        fields = ["stripe_id", "name", "description", "price", "events"]


@router.get("products/", response=list[StripeProductSchema])
async def list_products(request: AuthHttpRequest):
    return [
        product
        async for product in StripeProduct.objects.filter(is_public=True, events__gt=0)
    ]

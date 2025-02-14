from django.shortcuts import aget_object_or_404
from ninja import ModelSchema, Router

from apps.organizations_ext.constants import OrganizationUserRole
from apps.organizations_ext.models import Organization
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.schema import CamelSchema

from .models import StripeProduct, StripeSubscription

router = Router()


class StripeProductSchema(CamelSchema, ModelSchema):
    class Meta:
        model = StripeProduct
        fields = ["stripe_id", "name", "description", "events"]


class StripeSubscriptionSchema(CamelSchema, ModelSchema):
    class Meta:
        model = StripeSubscription
        fields = ["stripe_id", "created", "current_period_start", "current_period_end"]


class PriceIDSchema(CamelSchema):
    price: str


@router.get("products/", response=list[StripeProductSchema])
async def list_stripe_products(request: AuthHttpRequest):
    return [
        product
        async for product in StripeProduct.objects.filter(is_public=True, events__gt=0)
    ]


@router.get(
    "subscriptions/{slug:organization_slug}/", response=StripeSubscriptionSchema | None
)
async def get_stripe_subscription(request: AuthHttpRequest, organization_slug: str):
    return await (
        StripeSubscription.objects.filter(
            organization__users=request.auth.user_id,
            organization__slug=organization_slug,
            is_active=True,
        )
        .select_related("product")
        .order_by("-created")
        .afirst()
    )


@router.post(
    "organizations/{slug:organization_slug}/create-stripe-subscription-checkout/"
)
async def create_stripe_subscription_checkout(
    request: AuthHttpRequest, organization_slug: str, payload: PriceIDSchema
):
    """
    Create Stripe Checkout, send to client for redirecting to Stripe
    See https://stripe.com/docs/api/checkout/sessions/create
    """
    organization = await aget_object_or_404(
        Organization,
        slug=organization_slug,
        organization_users__role=OrganizationUserRole.OWNER,
        organization_users__user=request.auth.user_id,
    )
    # price = await aget_object_or_404(Price, id=payload.price)
    # customer, _ = await sync_to_async(Customer.get_or_create)(subscriber=organization)
    # domain = settings.GLITCHTIP_URL.geturl()
    # async with get_stripe_client() as client:
    #     session = await client.checkout.sessions.create_async(
    #         params={
    #             "payment_method_types": ["card"],
    #             "line_items": [
    #                 {
    #                     "price": price.id,
    #                     "quantity": 1,
    #                 }
    #             ],
    #             "mode": "subscription",
    #             "customer": customer.id,
    #             "automatic_tax": {
    #                 "enabled": settings.STRIPE_AUTOMATIC_TAX,
    #             },
    #             "customer_update": {"address": "auto", "name": "auto"},
    #             "tax_id_collection": {
    #                 "enabled": True,
    #             },
    #             "success_url": domain
    #             + "/"
    #             + organization.slug
    #             + "/settings/subscription?session_id={CHECKOUT_SESSION_ID}",
    #             "cancel_url": domain + "",
    #         }
    #     )

    # return session

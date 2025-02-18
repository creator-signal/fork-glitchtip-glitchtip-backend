from django.conf import settings
from django.shortcuts import aget_object_or_404
from ninja import ModelSchema, Router

from apps.organizations_ext.constants import OrganizationUserRole
from apps.organizations_ext.models import Organization
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.schema import CamelSchema

from .client import stripe_post
from .models import StripePrice, StripeProduct, StripeSubscription

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
    if organization.stripe_customer_id:
        customer_id = organization.stripe_customer_id
    else:
        customer_id = ""
        pass  # customer_id = Create it
    # Ensure price exists
    price_id = payload.price
    await aget_object_or_404(StripePrice, stripe_id=price_id)
    domain = settings.GLITCHTIP_URL.geturl()
    params = {
        "payment_method_types": ["card"],
        "line_items": [
            {
                "price": price_id,
                "quantity": 1,
            }
        ],
        "mode": "subscription",
        "customer": customer_id,
        "automatic_tax": {
            "enabled": True,
        },
        "customer_update": {"address": "auto", "name": "auto"},
        "tax_id_collection": {
            "enabled": True,
        },
        "success_url": domain
        + "/"
        + organization.slug
        + "/settings/subscription?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": domain + "",
    }
    session = await stripe_post("/checkout/sessions", params)
    return session

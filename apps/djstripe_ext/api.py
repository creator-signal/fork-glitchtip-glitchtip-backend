import stripe
from asgiref.sync import sync_to_async
from django.conf import settings
from django.db.models import Prefetch
from django.http import Http404
from django.shortcuts import aget_object_or_404
from djstripe.models import Customer, Price, Subscription, SubscriptionItem
from djstripe.settings import djstripe_settings
from ninja import Router

from apps.organizations_ext.models import Organization, OrganizationUserRole
from glitchtip.api.authentication import AuthHttpRequest

from .schema import PriceIDSchema, SubscriptionSchema

router = Router()


@router.get("subscriptions/{slug:organization_slug}/", response=SubscriptionSchema)
async def get_subscription(request: AuthHttpRequest, organization_slug: str):
    subscription = await (
        Subscription.objects.filter(
            livemode=settings.STRIPE_LIVE_MODE,
            customer__subscriber__users=request.auth.user_id,
        )
        .exclude(status="canceled")
        .select_related("customer")
        .prefetch_related(
            Prefetch(
                "items",
                queryset=SubscriptionItem.objects.select_related("price__product"),
            )
        )
        .order_by("-created")
        .afirst()
    )
    if not subscription:
        raise Http404()

    # Check organization throttle, in case it changed recently
    await Organization.objects.filter(
        id=subscription.customer.subscriber_id,
        is_accepting_events=False,
        is_active=True,
        djstripe_customers__subscriptions__plan__amount__gt=0,
        djstripe_customers__subscriptions__status="active",
    ).aupdate(is_accepting_events=True)

    return subscription


@router.post("subscriptions/", response=SubscriptionSchema)
async def create_subscription(request: AuthHttpRequest):
    pass


@router.get("subscriptions/{slug:organization_slug}/events_count/")
async def get_subscription_events_count(
    request: AuthHttpRequest, organization_slug: str
):
    org = await aget_object_or_404(
        Organization.objects.with_event_counts(),
        slug=organization_slug,
        users=request.auth.user_id,
    )
    return {
        "eventCount": org.issue_event_count,
        "transactionEventCount": org.transaction_count,
        "uptimeCheckEventCount": org.uptime_check_event_count,
        "fileSizeMB": org.file_size,
    }


@router.post("organizations/{slug:organization_slug}/create-billing-portal/")
async def stripe_billing_portal(request: AuthHttpRequest, organization_slug: str):
    """See https://stripe.com/docs/billing/subscriptions/integrating-self-serve-portal"""
    organization = await aget_object_or_404(
        Organization,
        slug=organization_slug,
        organization_users__role=OrganizationUserRole.OWNER,
        organization_users__user=request.auth.user_id,
    )
    customer, _ = await sync_to_async(Customer.get_or_create)(subscriber=organization)
    domain = settings.GLITCHTIP_URL.geturl()
    session = await sync_to_async(stripe.billing_portal.Session.create)(
        api_key=djstripe_settings.STRIPE_SECRET_KEY,
        customer=customer.id,
        return_url=domain + "/" + organization.slug + "/settings/subscription",
    )
    # Once we can update stripe-python
    # session = await stripe.billing_portal.Session.create_async(
    return session


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
    price = await aget_object_or_404(Price, id=payload.price)
    customer, _ = await sync_to_async(Customer.get_or_create)(subscriber=organization)
    domain = settings.GLITCHTIP_URL.geturl()
    session = await sync_to_async(stripe.checkout.Session.create)(
        api_key=djstripe_settings.STRIPE_SECRET_KEY,
        payment_method_types=["card"],
        line_items=[
            {
                "price": price.id,
                "quantity": 1,
            }
        ],
        mode="subscription",
        customer=customer.id,
        automatic_tax={
            "enabled": settings.STRIPE_AUTOMATIC_TAX,
        },
        customer_update={"address": "auto", "name": "auto"},
        tax_id_collection={
            "enabled": True,
        },
        success_url=domain
        + "/"
        + organization.slug
        + "/settings/subscription?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=domain + "",
    )

    return session

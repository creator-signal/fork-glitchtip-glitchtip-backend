import stripe
from asgiref.sync import sync_to_async
from django.conf import settings
from django.shortcuts import aget_object_or_404
from djstripe.models import Customer
from djstripe.settings import djstripe_settings
from ninja import Router

from apps.organizations_ext.models import Organization, OrganizationUserRole
from glitchtip.api.authentication import AuthHttpRequest

router = Router()


@router.post("{slug:organization_slug}/create-billing-portal/")
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
    session = await stripe.billing_portal.Session.create_async(
        api_key=djstripe_settings.STRIPE_SECRET_KEY,
        customer=customer.id,
        return_url=domain + "/" + organization.slug + "/settings/subscription",
    )
    return session

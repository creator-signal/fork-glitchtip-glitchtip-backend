"""
License minting — issuer side only, runs on app.glitchtip.com.

Given a Stripe subscription id, fetch the subscription + customer from Stripe,
mint a signed license blob, upsert the IssuedLicense record, and email the
blob to the customer.
"""

import logging
import time
from datetime import datetime, timezone

from asgiref.sync import sync_to_async
from django.conf import settings

from apps.stripe.client import stripe_get
from apps.stripe.schema import Customer, Subscription

from .emails import send_license_email
from .keys import load_signing_key
from .models import IssuedLicense
from .signing import encode
from .verifier import GRACE_PERIOD_SECONDS

logger = logging.getLogger(__name__)

# Subscription statuses that entitle the customer to a usable license.
ACTIVE_STATUSES = {"active", "trialing", "past_due"}


class IssuerNotConfigured(Exception):
    pass


async def mint_for_subscription_id(
    subscription_id: str, stripe_event_id: str = ""
) -> IssuedLicense | None:
    """
    Fetch a subscription from Stripe and issue (or re-issue) its license.

    Returns None if the subscription is not in an active state — in that case
    we still update our record so support can see the latest status.
    """
    signing = load_signing_key()
    if signing is None:
        raise IssuerNotConfigured("SELF_HOST_LICENSE_SIGNING_KEY / KID not configured")
    kid, private_key = signing

    subscription = Subscription.model_validate_json(
        await stripe_get(f"subscriptions/{subscription_id}")
    )
    customer = Customer.model_validate_json(
        await stripe_get(f"customers/{subscription.customer}")
    )
    email = customer.email or ""
    if not email:
        logger.warning(
            "self-host license: subscription %s has no customer email; skipping",
            subscription.id,
        )
        return None

    item = subscription.items.data[0]
    price = item.price
    plan = price.lookup_key or price.nickname or price.id
    seats = item.quantity
    current_period_end_unix = item.current_period_end
    current_period_end = datetime.fromtimestamp(
        current_period_end_unix, tz=timezone.utc
    )

    is_active = subscription.status in ACTIVE_STATUSES
    blob: str | None = None
    if is_active:
        claims = {
            "iss": getattr(settings, "SELF_HOST_LICENSE_ISSUER", "app.glitchtip.com"),
            "sub": subscription.id,
            "cus": subscription.customer,
            "eml": email,
            "pln": plan,
            "sts": seats,
            "iat": int(time.time()),
            "exp": current_period_end_unix + GRACE_PERIOD_SECONDS,
        }
        blob = encode(claims, private_key, kid)

    license_obj, _created = await IssuedLicense.objects.aupdate_or_create(
        stripe_subscription_id=subscription.id,
        defaults={
            "stripe_customer_id": subscription.customer,
            "email": email,
            "plan": plan,
            "status": subscription.status,
            "current_period_end": current_period_end,
            "last_stripe_event_id": stripe_event_id,
        },
    )

    if blob is not None:
        await sync_to_async(send_license_email)(email, blob, plan, current_period_end)

    return license_obj

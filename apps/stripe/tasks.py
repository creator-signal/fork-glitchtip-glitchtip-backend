import logging

from asgiref.sync import sync_to_async
from django.tasks import task

from .client import fetch_customer_by_email
from .email import LicenseKeyEmail
from .exceptions import StripeResourceNotFound

logger = logging.getLogger(__name__)


@task
async def send_license_key_email_by_lookup(email: str):
    """Look up a Stripe customer by email; if found, send them their license key.

    Runs out-of-band (queued) so the public endpoint returns immediately and
    response timing doesn't leak whether the email matched a customer.

    Only catches StripeResourceNotFound (a definitive "not in Stripe" signal)
    so that transient errors (5xx, network) propagate to the task runner,
    which retries with backoff. Without retry, a Stripe blip = silent drop.
    """
    try:
        customer = await fetch_customer_by_email(email)
    except StripeResourceNotFound:
        logger.info("Stripe returned not-found for license-key email lookup")
        return

    if customer is None:
        logger.info("No Stripe customer matched license-key email lookup")
        return

    if not customer.email:
        # Stripe has a customer matching this query but no email on file.
        # Sending to the caller-supplied address would leak match status to
        # anyone controlling that inbox without verifying they own the
        # billing email. Skip silently.
        logger.warning(
            "Matched Stripe customer %s has no email on file; skipping send",
            customer.id,
        )
        return

    # Send to the Stripe-verified address, NOT the caller-supplied one.
    # Stripe's email-lookup is case-insensitive, so the two may differ in
    # case; the Stripe one is authoritative.
    await sync_to_async(LicenseKeyEmail(license_key=customer.id).send_email)(
        customer.email
    )

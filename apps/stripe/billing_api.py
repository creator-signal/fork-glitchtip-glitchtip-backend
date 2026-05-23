import logging
import re
from hmac import compare_digest

from django.core.cache import cache
from django.http import HttpRequest, HttpResponseRedirect, JsonResponse
from ipware import get_client_ip
from ninja import Router
from ninja.errors import Throttled

from .client import fetch_latest_invoice_for_customer, fetch_subscription_with_customer
from .exceptions import StripeResourceNotFound
from .validators import SUBSCRIPTION_ID_PATTERN

logger = logging.getLogger(__name__)

router = Router()

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

LICENSE_INVOICE_LIMIT = 10
LICENSE_INVOICE_WINDOW_SECONDS = 60


async def _throttle(cache_key: str, limit: int, window_seconds: int):
    """Per-key fixed-window throttle (atomic aadd/aincr). Raises Throttled when limit hit."""
    if await cache.aadd(cache_key, 1, window_seconds):
        return
    try:
        count = await cache.aincr(cache_key)
    except ValueError:
        # Key expired between aadd and aincr; treat as a fresh window.
        await cache.aadd(cache_key, 1, window_seconds)
        return
    if count > limit:
        raise Throttled(limit)


def _uniform_error() -> JsonResponse:
    """Same status + body for every failure mode (malformed input, unknown
    subscription, wrong email, no invoice) so a caller cannot distinguish
    them via response shape."""
    return JsonResponse({"detail": "Could not verify license"}, status=404)


@router.get("license-invoice/", auth=None)
async def license_invoice(
    request: HttpRequest,
    license_key: str | None = None,
    email: str | None = None,
):
    """Redirect to the customer's most recent Stripe-hosted invoice page,
    gated on a matching (license_key, billing email) pair."""
    client_ip, _ = get_client_ip(request)
    await _throttle(
        f"license_invoice_throttle_{client_ip or 'unknown'}",
        LICENSE_INVOICE_LIMIT,
        LICENSE_INVOICE_WINDOW_SECONDS,
    )

    if (
        not license_key
        or not email
        or not SUBSCRIPTION_ID_PATTERN.match(license_key)
        or not EMAIL_PATTERN.match(email)
    ):
        return _uniform_error()

    try:
        subscription = await fetch_subscription_with_customer(license_key)
    except StripeResourceNotFound:
        return _uniform_error()
    except Exception:
        logger.exception("Stripe subscription lookup failed")
        return JsonResponse({"detail": "Unable to look up subscription"}, status=500)

    customer_email = subscription.customer.email or ""
    if not compare_digest(email.lower().encode(), customer_email.lower().encode()):
        return _uniform_error()

    try:
        invoice = await fetch_latest_invoice_for_customer(subscription.customer.id)
    except Exception:
        logger.exception("Stripe invoice lookup failed")
        return JsonResponse({"detail": "Unable to look up invoice"}, status=500)

    if invoice is None or not invoice.hosted_invoice_url:
        return _uniform_error()

    return HttpResponseRedirect(invoice.hosted_invoice_url)
